"""Cerry token control. Enable cerry_tokens in plugins.json; restart Cerry.

Caps every chat.completions.create call that uses Cerry's get_client (including Jobs,
style learning, memory and document study). Hermes' external gateway is NOT covered.
The cap is PER ATTEMPT, not a daily budget or a currency limit. Fallback attempts are
separate requests and can each cost tokens. Existing smaller limits are preserved.

Optional text-input guard: reject requests above a chosen number of text characters.
It never truncates prompts or files silently. Images/audio have extra token costs that
this character count does not measure. Rough token estimate = text characters / 4;
this is not a tokenizer and must not be treated as a billing count.

If a provider rejects the cap parameter, the call fails normally. No uncapped retry.
Choose max_tokens or max_completion_tokens per provider under Settings.
Settings: ai_workspace_data/tokens.json. No network calls occur when opening settings.
"""
import json
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

ORDER = 85
DEFAULTS = {'enabled': True, 'output_limit': 4096, 'input_char_limit': 0,
            'provider_parameters': {}, 'default_parameter': 'max_tokens'}
PARAMETERS = ('max_tokens', 'max_completion_tokens')


def text_characters(messages):
    total = 0
    for message in messages or []:
        content = message.get('content')
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') in ('text', 'input_text'):
                    total += len(str(part.get('text', '')))
        # Count text in tool/function metadata, but never count base64 image payloads as text.
        for key in ('tool_calls', 'function_call'):
            if message.get(key):
                total += len(json.dumps(message[key], ensure_ascii=False))
    return total


def capped_arguments(kwargs, provider, cfg):
    if not cfg.get('enabled', True):
        return dict(kwargs), None
    limit = max(1, int(cfg['output_limit']))
    result = dict(kwargs)
    chars = text_characters(result.get('messages'))
    guard = int(cfg.get('input_char_limit', 0))
    if guard and chars > guard:
        raise ValueError(f'Token control blocked this request: {chars:,} text characters exceed '
                         f'the {guard:,}-character guard. Increase the guard or reduce the input. '
                         'No prompt was truncated.')
    present = [p for p in PARAMETERS if result.get(p) is not None]
    for p in present:
        limit = min(limit, int(result[p]))
    # Preserve a caller's chosen parameter when one already exists.
    parameter = present[0] if present else cfg.get('provider_parameters', {}).get(
        provider, cfg.get('default_parameter', 'max_tokens'))
    if parameter not in PARAMETERS:
        raise ValueError('Token control: invalid cap parameter for ' + provider)
    for p in PARAMETERS:
        result.pop(p, None)
    result[parameter] = limit
    return result, {'characters': chars, 'limit': limit, 'parameter': parameter}


class LimitedStream:
    def __init__(self, stream, notify):
        self._stream, self._notify = stream, notify
        self._iterator = iter(stream)

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._iterator)
        for choice in getattr(chunk, 'choices', None) or []:
            if getattr(choice, 'finish_reason', None) == 'length':
                self._notify('Output token limit reached; the answer may be incomplete. '
                             'Increase Tokens and regenerate if needed.')
        return chunk

    def close(self):
        return self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class Completions:
    def __init__(self, real, provider, cfg, lock, notify):
        self.real, self.provider, self.cfg, self.lock, self.notify = real, provider, cfg, lock, notify

    def create(self, *args, **kwargs):
        with self.lock:
            snapshot = json.loads(json.dumps(self.cfg))
        params, info = capped_arguments(kwargs, self.provider, snapshot)
        if info:
            self.notify(f"Token cap: {info['limit']:,}; text input: {info['characters']:,} characters "
                        f"(~{info['characters'] // 4:,} tokens, rough estimate; excludes images).")
        # Deliberately no retry without a cap when the provider rejects the parameter.
        response = self.real.create(*args, **params)
        if kwargs.get('stream'):
            return LimitedStream(response, self.notify)
        for choice in getattr(response, 'choices', None) or []:
            if getattr(choice, 'finish_reason', None) == 'length':
                self.notify('Output token limit reached; the answer may be incomplete.')
        return response

    def __getattr__(self, name):
        return getattr(self.real, name)


class Client:
    def __init__(self, real, provider, cfg, lock, notify):
        self._real, self._provider, self._cfg, self._lock, self._notify = real, provider, cfg, lock, notify
        self.chat = Chat(real.chat, provider, cfg, lock, notify)

    def with_options(self, *args, **kwargs):
        return Client(self._real.with_options(*args, **kwargs), self._provider,
                      self._cfg, self._lock, self._notify)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Chat:
    def __init__(self, real, provider, cfg, lock, notify):
        self._real = real
        self.completions = Completions(real.completions, provider, cfg, lock, notify)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install(ns):
    App = ns['App']
    if App.__dict__.get('_token_control_plugin_installed'):
        return
    path = Path(ns['DATA_DIR']) / 'tokens.json'
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            cfg.update(data)
    lock, holder = threading.RLock(), {}
    original_client = ns['get_client']

    def notify(text):
        app = holder.get('app')
        if app is not None:
            # Queue only: model requests often run on background threads.
            app.q.put(('status', text))
    ns['get_client'] = lambda provider: Client(original_client(provider), provider, cfg, lock, notify)
    original_init, original_send = App.__init__, App.send

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        holder['app'] = app
        enabled = tk.BooleanVar(value=bool(cfg['enabled']))
        limit = tk.StringVar(value=str(cfg['output_limit']))
        row = app.sidebar.tool_row("Token control")

        def save():
            with lock:
                tmp = path.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                tmp.replace(path)

        def apply(_event=None):
            try:
                value = int(limit.get())
                if not 128 <= value <= 131072:
                    raise ValueError()
            except ValueError:
                app.status.set('Tokens: choose an output cap between 128 and 131072.')
                return
            with lock:
                cfg.update(enabled=bool(enabled.get()), output_limit=value)
                save()
            app.status.set(f'Token control: {value} output tokens per model attempt.' if enabled.get() else 'Token control is off.')

        def settings():
            win = tk.Toplevel(app)
            win.title('Token control - settings')
            frame = ttk.Frame(win, padding=12)
            frame.pack(fill='both', expand=True)
            ttk.Label(frame, text='Text-input guard in characters (0 = off):').pack(anchor='w')
            guard = tk.StringVar(value=str(cfg['input_char_limit']))
            ttk.Entry(frame, textvariable=guard).pack(fill='x')
            ttk.Label(frame, wraplength=580, text='This blocks oversized text requests, not image/audio token costs. '
                'Nothing is truncated. Lower output caps can break long code, JSON, CVs or letters. '
                'For Gemini, use a lower Thinking setting too; a cap may be spent on reasoning.').pack(pady=8)
            variables = {}
            for provider in ns['PROVIDERS']:
                sub = ttk.Frame(frame)
                sub.pack(fill='x', pady=2)
                ttk.Label(sub, text=provider, width=18).pack(side='left')
                var = tk.StringVar(value=cfg['provider_parameters'].get(provider, cfg['default_parameter']))
                variables[provider] = var
                ttk.Combobox(sub, textvariable=var, values=PARAMETERS, state='readonly', width=24).pack(side='left')

            def store():
                try:
                    value = int(guard.get())
                    if value < 0:
                        raise ValueError()
                except ValueError:
                    messagebox.showerror('Tokens', 'The text-input guard must be zero or a positive integer.', parent=win)
                    return
                with lock:
                    cfg['input_char_limit'] = value
                    cfg['provider_parameters'] = {p: v.get() for p, v in variables.items()}
                    save()
                win.destroy()
            ttk.Button(frame, text='Save', command=store).pack(anchor='e')
        ttk.Checkbutton(row, text='Token control', variable=enabled, command=apply).pack(side='left')
        limits = ttk.Frame(row)
        limits.pack(fill='x')
        box = ttk.Combobox(limits, textvariable=limit, width=7, values=['1024', '2048', '4096', '8192', '16384'])
        box.pack(side='right', padx=2)
        ttk.Label(limits, text='Output limit').pack(side='left')
        for event in ('<<ComboboxSelected>>', '<Return>', '<FocusOut>'):
            box.bind(event, apply)
        ttk.Button(row, text='Token settings...', command=settings).pack(side='left', padx=8)
        app._token_settings = settings

    def send(app):
        if app.inp.get('1.0', 'end').strip().lower() == '/tokens':
            app.inp.delete('1.0', 'end')
            app._token_settings()
            return
        return original_send(app)
    App.__init__, App.send = init, send
    App._token_control_plugin_installed = True
