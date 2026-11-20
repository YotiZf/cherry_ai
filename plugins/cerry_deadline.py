"""Cerry response deadline: if a cloud model gives no answer within X seconds, Cerry moves on to the next model.

Switch on:  add "cerry_deadline" to ai_workspace_data/plugins.json
Use:        a strip above the input box:  [x] Switch to the next model if there is no answer within [30] seconds
            Or type   /wait 45   (seconds, 5-300)   /wait off   /wait on   /wait local on|off
Settings:   ai_workspace_data/deadline.json   (created on first start)

How it works: every model request made through Cerry's get_client is watched. The clock starts when the request is
sent and stops at the first real sign of life (answer text or provider-streamed reasoning). If nothing arrives in time,
the attempt is cancelled and Cerry's normal fallback chain tries the next model. Once a model has started answering,
Cerry never switches in the middle of the answer (a stall of 120 s after that is reported as an error instead).
Local models (localhost) are not timed by default, because they may need a long time to load; tick "also for local
models" to change that.
"""
import json
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import ttk

INSTALL_FLAG = "_dl_installed"
DEFAULTS = {"enabled": True, "seconds": 30.0, "apply_local": False}
MIN_S, MAX_S, IDLE_S = 5.0, 300.0, 120.0


class DeadlineTimeout(TimeoutError):
    """No answer from the model in time (message contains 'timed out' so routing treats it as a timeout)."""


def has_life(chunk):
    """True when a streamed chunk carries real output (text, reasoning or a tool call), not just an empty header."""
    try:
        for ch in getattr(chunk, "choices", None) or []:
            d = getattr(ch, "delta", None)
            if d is None:
                continue
            if getattr(d, "content", None) or getattr(d, "tool_calls", None):
                return True
            for name in ("reasoning_content", "reasoning"):
                if getattr(d, name, None):
                    return True
    except Exception:
        return True
    return False


class GuardedStream:
    """Wraps an OpenAI-style stream; raises DeadlineTimeout if no real output arrives before the deadline."""

    def __init__(self, stream, started, deadline, label, notify):
        self._s, self._t0, self._deadline, self._label, self._notify = stream, started, deadline, label, notify
        self._q = queue.Queue()
        self._life = False
        self._closed = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for chunk in self._s:
                if self._closed.is_set():
                    break
                self._q.put(("chunk", chunk))
            self._q.put(("end", None))
        except BaseException as e:   # noqa: closing the stream raises inside this helper thread
            self._q.put(("error", e))

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._life:
                wait = IDLE_S
            else:
                wait = max(0.05, self._t0 + self._deadline - time.monotonic())
            try:
                kind, val = self._q.get(timeout=wait)
            except queue.Empty:
                self.close()
                if not self._life:
                    msg = (f"timed out: no answer from {self._label} within {self._deadline:g} s "
                           "- trying the next model")
                    self._notify(msg)
                    raise DeadlineTimeout(msg)
                raise TimeoutError(f"timed out: {self._label} stopped sending data for {IDLE_S:g} s")
            if kind == "chunk":
                if not self._life and has_life(val):
                    self._life = True
                return val
            if kind == "end":
                raise StopIteration
            raise val

    def close(self):
        self._closed.set()
        try:
            self._s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __getattr__(self, name):
        return getattr(self._s, name)


class _Completions:
    def __init__(self, real, owner):
        self._real, self._owner = real, owner

    def create(self, *args, **kwargs):
        d = self._owner.deadline()
        if not d:
            return self._real.create(*args, **kwargs)
        label = str(kwargs.get("model", "the model"))
        t0 = time.monotonic()
        box = {}

        def call():
            try:
                box["r"] = self._real.create(*args, **kwargs)
            except BaseException as e:   # noqa
                box["e"] = e
        t = threading.Thread(target=call, daemon=True)
        t.start()
        t.join(d)
        if t.is_alive():
            def tidy():
                t.join()
                r = box.get("r")
                try:
                    r.close()
                except Exception:
                    pass
            threading.Thread(target=tidy, daemon=True).start()
            msg = f"timed out: no answer from {label} within {d:g} s - trying the next model"
            self._owner.notify(msg)
            raise DeadlineTimeout(msg)
        if "e" in box:
            raise box["e"]
        result = box["r"]
        if kwargs.get("stream"):
            return GuardedStream(result, t0, d, label, self._owner.notify)
        return result


class _Chat:
    def __init__(self, real, owner):
        self.completions = _Completions(real.completions, owner)
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


class GuardedClient:
    """Looks like the normal client; only chat.completions.create is watched."""

    def __init__(self, real, settings, notify):
        self._real, self._settings, self.notify = real, settings, notify
        self.chat = _Chat(real.chat, self)

    def deadline(self):
        s = self._settings
        if not s.get("enabled"):
            return None
        try:
            host = urlparse(str(getattr(self._real, "base_url", ""))).hostname or ""
        except Exception:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1") and not s.get("apply_local"):
            return None
        return float(s.get("seconds", 30.0))

    def with_options(self, *a, **k):
        return GuardedClient(self._real.with_options(*a, **k), self._settings, self.notify)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install(ns):
    App = ns["App"]
    if App.__dict__.get(INSTALL_FLAG):
        return
    setattr(App, INSTALL_FLAG, True)
    path = Path(ns["DATA_DIR"]) / "deadline.json"
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved:
                    settings[k] = saved[k]
    except (OSError, ValueError):
        pass
    try:
        settings["seconds"] = min(MAX_S, max(MIN_S, float(settings["seconds"])))
    except (TypeError, ValueError):
        settings["seconds"] = DEFAULTS["seconds"]

    def save():
        try:
            path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except OSError:
            pass
    save()
    holder = {}

    def notify(msg):
        app = holder.get("app")
        if app is None:
            return
        try:
            app.q.put(("status", msg))
        except Exception:
            pass
        try:
            app._ext_events.put(msg)
        except Exception:
            pass

    original_get_client = ns["get_client"]

    def get_client(provider):
        return GuardedClient(original_get_client(provider), settings, notify)
    ns["get_client"] = get_client

    routing = ns.get("routing")
    original_classify = getattr(routing, "classify_error", None)
    if original_classify is not None:
        def classify_error(exc, *a, **k):
            if isinstance(exc, DeadlineTimeout):
                return "timeout"
            return original_classify(exc, *a, **k)
        routing.classify_error = classify_error

    original_init, original_send = App.__init__, App.send

    def init(app, *a, **k):
        holder["app"] = app
        original_init(app, *a, **k)
        on = tk.BooleanVar(value=bool(settings["enabled"]))
        loc = tk.BooleanVar(value=bool(settings["apply_local"]))
        secs = tk.StringVar(value=f"{settings['seconds']:g}")
        info = tk.StringVar(value="")
        app._dl_vars = (on, loc, secs)

        def apply(*_):
            settings["enabled"], settings["apply_local"] = bool(on.get()), bool(loc.get())
            try:
                v = float(secs.get())
                if not MIN_S <= v <= MAX_S:
                    raise ValueError
                settings["seconds"] = v
                info.set(f"Applies to the next request: {v:g} s" if settings["enabled"] else "Deadline is off.")
            except ValueError:
                info.set(f"Seconds must be between {MIN_S:g} and {MAX_S:g}; keeping {settings['seconds']:g}.")
            save()
        bar = ttk.Frame(app, padding=(6, 0))
        bar.pack(fill="x", before=app.inp.master)
        ttk.Checkbutton(bar, text="Switch to the next model if there is no answer within", variable=on,
                        command=apply).pack(side="left")
        ttk.Spinbox(bar, from_=MIN_S, to=MAX_S, increment=5, width=5, textvariable=secs, command=apply
                    ).pack(side="left", padx=4)
        ttk.Label(bar, text="seconds").pack(side="left")
        ttk.Checkbutton(bar, text="also for local models", variable=loc, command=apply).pack(side="left", padx=12)
        ttk.Label(bar, textvariable=info, style="Muted.TLabel").pack(side="left", padx=8)
        secs.trace_add("write", apply)

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        low = text.lower()
        if low == "/wait" or low.startswith("/wait "):
            app.inp.delete("1.0", "end")
            arg = low[5:].strip()
            on, loc, secs = app._dl_vars
            if arg in ("off", "on"):
                on.set(arg == "on")
            elif arg.startswith("local"):
                loc.set(arg.endswith("on"))
            elif arg:
                try:
                    v = float(arg.replace("s", "").strip())
                    if not MIN_S <= v <= MAX_S:
                        raise ValueError
                    secs.set(f"{v:g}")
                    on.set(True)
                except ValueError:
                    app.show(f"Use /wait <seconds> with {MIN_S:g}-{MAX_S:g}, /wait off, /wait on, /wait local on|off.")
                    return
            settings["enabled"], settings["apply_local"] = bool(on.get()), bool(loc.get())
            save()
            state = f"{settings['seconds']:g} s" if settings["enabled"] else "off"
            app.show(f"Response deadline: {state}; local models {'timed too' if settings['apply_local'] else 'not timed'}.")
            return
        return original_send(app)

    App.__init__, App.send = init, send
