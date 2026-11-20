"""Cerry Hermes voice (v3): voice and typed Hermes tasks start by themselves AND appear in a task list where you can end them.

Self-contained: uses core/cerry_hermes_core.py (the engine that talks to the Hermes gateway), not the Hermes plugin.
Tasks start immediately with your default models (the model selected in the main window first, then the other cloud
models, local models last). Several tasks can run at the same time (default 3, "max_parallel").

The task list: button "Hermes tasks..." (next to Agent...) or the command /hermestasks
  - one row per task: number, task, status (Starting / Running / Waiting for you / Completed / Failed / Stopped),
    model, start time, source (conversation, record button, push-to-talk, typed)
  - End task, End all running, Show activity and result of the selected task, Run again, Clear finished,
    Open results folder. The list is saved, so finished tasks are still there after a restart.
  - "Stop all agents" (the existing button) also ends these tasks.
  - Voice: "Cerry, stop Hermes" / "stop the agents" ends all running tasks.

How to give a command
 1. Conversation mode (Converse button): "Cerry, do <task>" / "Cerry, execute <task>" / "Cerry, run <task>" /
    "Cerry, ask Hermes to <task>" / "Hermes, <task>". Speech recognition often drops the name, so a sentence starting with
    "execute ..." or "do <action> ..." (for example "do create a folder called BB on my desktop") is accepted without
    "Cerry". "Do you ..." and "do we ..." stay chat. German: "Cerry, mach ...", "Cerry, erledige ...".
 2. Record button: if the recording is a command, Hermes starts at once.
 3. "Voice -> Hermes" button: click, speak the whole command, click again.
 4. Typed: /hermesnow <task>, or "Cerry, do <task>" / "Hermes, <task>" (typed commands always need the name).
    (/hermes <task> still opens the review window of the Hermes plugin; those tasks stay in its own list.)
Safety: Hermes asks YOU before risky commands (window, 280 s, then it denies); commands with destructive words (delete,
remove, erase, format, uninstall, shut down, overwrite, lösche, entferne ...) ask for confirmation first; the recognised
command is shown in the chat before it runs.
Diagnostics: ai_workspace_data/hermes_voice.log. Settings: ai_workspace_data/hermes_voice.json.
Task history: ai_workspace_data/hermes/voice_tasks.json. Results: ai_workspace_data/hermes/runs.
Enable: "cerry_hermes_voice" in plugins.json (needs cerry_hermes_core).
"""
import importlib
import json
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext

ORDER = 170
DEFAULTS = {
    'enabled': True,
    'wake_names': ['cerry', 'cherry', 'kerry', 'sherry', 'cerri', 'serry', 'sary', 'cheri', 'sheri', 'kerri', 'terry',
                   'jerry', 'carrie', 'kari'],
    'hermes_names': ['hermes', 'hermis', 'hermez', 'hermès', 'ermes'],
    'extra_verbs': [],
    'folder': '',
    'confirm_destructive': True,
    'record_button_commands': True,
    'lenient_without_name': True,
    'speak_ack': True,
    'speak_result': True,
    'max_parallel': 3,
    'history_size': 100,
}
BASE_VERBS = ['carry out', 'execute', 'perform', 'handle', 'run', 'do', 'mach', 'mache', 'erledige', 'führe aus',
              'fuehre aus']
GUARD = {'you', 'i', 'we', 'they', 'he', 'she', 'it', 'there', 'these', 'those', 'not', 'your', 'my', 'me', 'us',
         'anyone', 'anybody', 'people', 'that', 'this', 'the', 'a', 'an', 'what', 'how', 'why', 'when', 'where', 'who',
         'which', 'is', 'are', 'was', 'were', 'can', 'could', 'would', 'should', 'will', 'have', 'has', 'had', 'some',
         'any', 'all', 'something', 'anything', 'nothing', 'everything', 'well', 'our', 'their', 'his', 'her', 'its',
         'one', 'du', 'ich', 'wir', 'sie', 'er', 'es', 'nicht', 'das', 'die', 'der', 'ein', 'eine'}
DETERMINERS = {'the', 'a', 'an', 'this', 'that', 'these', 'those', 'my', 'your', 'our', 'their', 'his', 'her', 'its', 'one',
               'some', 'any', 'all', 'something', 'anything', 'nothing', 'everything', 'well', 'das', 'die', 'der', 'ein',
               'eine'}
QUESTION_GUARD = GUARD - DETERMINERS
VAGUE = {'this', 'that', 'it', 'this one', 'that one', 'das', 'es', 'dies'}
STATEMENT_STARTS = {'is', 'are', 'was', 'were', 'has', 'had', 'did', 'does', 'ist', 'war', 'sind'}
DESTRUCTIVE = re.compile(
    r'\b(delete|remove|erase|wipe|format|uninstall|shut\s*down|shutdown|kill|overwrite|purge|drop|'
    r'l(?:ö|oe)sch\w*|entfern\w*|formatier\w*|deinstallier\w*|herunterfahr\w*|(?:ü|ue)berschreib\w*)\b',
    re.I | re.U)
ACTIVE = ('Starting', 'Running', 'Waiting for you')
STATE = {'hv': None}


def alternatives(words):
    cleaned = sorted({w.strip() for w in words if w and w.strip()}, key=len, reverse=True)
    return '|'.join(re.escape(w).replace(r'\ ', r'\s+') for w in cleaned)


def make_parser(cfg):
    wake = alternatives(cfg['wake_names'])
    herm = alternatives(cfg['hermes_names'])
    verbs = alternatives(BASE_VERBS + list(cfg.get('extra_verbs', [])))
    lead = r'^\W*(?:(?:hey|hi|ok|okay|hallo|hej)\W+)?'
    polite = r'(?:(?:please|bitte)\W+)?'
    stop_re = re.compile(
        lead + r'(?:(?:%s)\W+)?%s(?:stop|cancel|abort|end|stoppe|beende|brich\s+ab)\W+(?:all\W+)?(?:%s|the\s+agents?|agents?|'
               r'the\s+tasks?|tasks?|der\s+agent|die\s+aufgabe|alle\s+agenten)\W*$' % (wake, polite, herm), re.I | re.U)
    hermes_re = re.compile(
        lead + r'(?P<w>(?:%s)\W+)?%s(?P<v>(?:ask|tell|have|let|get|call|use|frag|sag|lass)\W+)?(?:%s)\W+'
               r'(?:to\W+|zu\W+)?(?P<task>.+?)\W*$' % (wake, polite, herm), re.I | re.S | re.U)
    generic_re = re.compile(lead + r'(?:%s)\W+%s(?:%s)\W+(?P<task>.+?)\W*$' % (wake, polite, verbs), re.I | re.S | re.U)
    bare_re = re.compile(lead + polite + r'(?P<verb>execute|do)\W+(?P<task>.+?)\W*$', re.I | re.S | re.U)
    strip_re = re.compile(lead + r'(?:(?:%s|%s)\W+)*' % (wake, herm), re.I | re.U)

    def first_word(task):
        match = re.match(r'\W*(\w+)', task, re.U)
        return match.group(1).lower() if match else ''

    def ok(task):
        task = re.sub(r'\s+', ' ', task).strip()
        return task if len(task) >= 4 else None

    def parse(text, mode='strict'):
        """mode: strict (needs Cerry or Hermes), lenient (speech: the name may be missing), forced (push-to-talk)."""
        text = (text or '').strip()
        if not text:
            return None
        if stop_re.match(text):
            return ('stop', None)
        match = hermes_re.match(text)
        if match:
            task = ok(match.group('task'))
            bare = not match.group('w') and not match.group('v')
            if task and not (bare and first_word(task) in STATEMENT_STARTS):
                return ('run', task)
        match = generic_re.match(text)
        if match:
            task = ok(match.group('task'))
            if task and first_word(task) not in QUESTION_GUARD and task.lower() not in VAGUE:
                return ('run', task)
        if mode in ('lenient', 'forced') and cfg.get('lenient_without_name', True):
            match = bare_re.match(text)
            if match:
                task = ok(match.group('task'))
                is_do = match.group('verb').lower() == 'do'
                if task and not (is_do and first_word(task) in GUARD):
                    return ('run', task)
        if mode == 'forced':
            task = ok(strip_re.sub('', text, count=1))
            if task:
                return ('run', task)
        return None
    return parse


def find_button(widget, text):
    for child in widget.winfo_children():
        try:
            if child.winfo_class() == 'TButton' and str(child.cget('text')) == text:
                return child
        except Exception:
            pass
        found = find_button(child, text)
        if found is not None:
            return found
    return None


def find_combobox(app):
    variable = str(getattr(app, 'model_var', ''))
    stack = [app]
    while variable and stack:
        widget = stack.pop()
        for child in widget.winfo_children():
            try:
                if child.winfo_class() == 'TCombobox' and str(child.cget('textvariable')) == variable:
                    return child
            except Exception:
                pass
            stack.append(child)
    return None


def live_models(app, ns):
    """The models of the main window's Model box (the same list the user sees)."""
    fallback = list(ns.get('MODELS') or [])
    box = find_combobox(app)
    if box is None:
        return fallback
    labels = [str(x) for x in app.tk.splitlist(box.cget('values'))]
    known = {m.get('label'): m for m in fallback}
    models, original = [], app.model_var.get()
    for label in labels:
        model = known.get(label)
        if model is None:
            try:
                app.model_var.set(label)
                candidate = app.selected_model()
                if candidate and candidate.get('label') == label:
                    model = candidate
            except Exception:
                model = None
            finally:
                app.model_var.set(original)
        if model is not None:
            models.append(model)
    return models or fallback


class HermesVoice:
    def __init__(self, app, ns, cfg, core, note):
        self.app, self.ns, self.cfg, self.core, self.note = app, ns, cfg, core, note
        self.parse = make_parser(cfg)
        self.q = queue.Queue()
        self.base = Path(ns['DATA_DIR']) / 'hermes'
        self.history_file = self.base / 'voice_tasks.json'
        self.gateway = None
        self.gateway_lock = threading.Lock()
        self.recording = False
        self.button_text = tk.StringVar(value='Voice -> Hermes')
        self.tasks_text = tk.StringVar(value='Hermes tasks...')
        self.tmp = Path(ns['DATA_DIR']) / 'hermes_voice_tmp.wav'
        self.window = None
        self.tasks = self.load_history()
        self.next_id = max([t['id'] for t in self.tasks], default=0) + 1
        app.after(200, self.pump)
        app.after(700, self.tick)

    # ---------------------------------------------------------------- history
    def load_history(self):
        try:
            data = json.loads(self.history_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        tasks = []
        for t in data if isinstance(data, list) else []:
            if not isinstance(t, dict) or 'id' not in t or 'instruction' not in t:
                continue
            t.setdefault('log', [])
            t.setdefault('result', '')
            if t.get('status') in ACTIVE:
                t['status'] = 'Stopped'
                t['result'] = (t.get('result') or '') + 'Cerry was closed while this task was running.'
            tasks.append(t)
        return tasks[-int(self.cfg.get('history_size', 100)):]

    def save_history(self):
        keep = self.tasks[-int(self.cfg.get('history_size', 100)):]
        data = [{k: v for k, v in t.items() if k in ('id', 'instruction', 'folder', 'source', 'status', 'model',
                                                      'started', 'finished', 'result')} | {'log': t['log'][-200:]}
                for t in keep]
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            temporary = self.history_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
            temporary.replace(self.history_file)
        except OSError:
            pass

    def get(self, tid):
        return next((t for t in self.tasks if t['id'] == tid), None)

    def running(self):
        return [t for t in self.tasks if t['status'] in ACTIVE]

    # ---------------------------------------------------------------- feedback
    def say(self, text):
        try:
            self.app.status.set(str(text)[:220])
        except Exception:
            pass

    def speak(self, text):
        try:
            self.app.speaker.speak(str(text)[:300])
        except Exception:
            pass

    # ---------------------------------------------------------------- dispatch
    def command(self, text, source, mode='lenient'):
        """True when the text was a Hermes command and has been dealt with."""
        if not self.cfg.get('enabled', True):
            return False
        parsed = self.parse(text, mode)
        self.note('heard (%s, %s): %r -> %s' % (source, mode, text[:150], parsed))
        if not parsed:
            return False
        if parsed[0] == 'stop':
            self.stop_all()
        else:
            self.run(parsed[1], source)
        return True

    def run(self, task_text, source):
        app = self.app
        if len(self.running()) >= int(self.cfg.get('max_parallel', 3)):
            self.say('%d Hermes tasks are already running. End one in "Hermes tasks..." first.' % len(self.running()))
            if source != 'typed' and self.cfg.get('speak_ack', True):
                self.speak('Too many Hermes tasks are running.')
            return None
        try:
            hcfg = self.core.load_config(Path(self.ns['DATA_DIR']) / 'hermes.json')
            routes = self.core.build_routes(live_models(app, self.ns), hcfg)
        except Exception as error:
            self.say('Hermes model list problem: %s' % error)
            self.note('model list error:\n' + traceback.format_exc())
            return None
        if not routes:
            self.say('No usable models for Hermes. See ai_workspace_data/hermes_models.log.')
            return None
        try:
            selected = app.selected_model()['label']
        except Exception:
            selected = None
        chain = self.core.order_chain(self.core.default_chain(routes, selected), routes)
        folder = self.cfg.get('folder') or ''
        if folder and not Path(folder).is_dir():
            self.say('The folder in hermes_voice.json does not exist; starting without a folder instruction.')
            folder = ''
        if self.cfg.get('confirm_destructive', True) and DESTRUCTIVE.search(task_text):
            names = ', '.join(r['label'] for r in chain)
            if not messagebox.askyesno('Run this task with Hermes?',
                                       'Hermes may create, change or delete files and run commands.\n\nTask: %s\n'
                                       'Models in order: %s\n\nStart now?' % (task_text, names), parent=app):
                self.say('Hermes command cancelled.')
                return None
        task = {'id': self.next_id, 'instruction': task_text, 'folder': folder, 'source': source, 'status': 'Starting',
                'model': '-', 'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'finished': '', 'log': [], 'result': '',
                'speak': source in ('conversation', 'record button', 'push-to-talk'), 'stop': threading.Event()}
        self.next_id += 1
        self.tasks.append(task)
        self.save_history()
        app.show('Hermes task #%d (%s): %s\nModels in order: %s\nEnd it in "Hermes tasks..." or say "Cerry, stop Hermes".' % (
            task['id'], source, task_text, ', '.join(r['label'] for r in chain)), 'sys')
        self.say('Hermes task #%d started: %s' % (task['id'], task_text[:90]))
        self.note('START #%d %s | chain: %s' % (task['id'], task_text, [r['label'] for r in chain]))
        if task['speak'] and self.cfg.get('speak_ack', True):
            self.speak('Okay, asking Hermes.')
        threading.Thread(target=self.worker, args=(task, chain, hcfg), daemon=True).start()
        self.refresh_window()
        return task

    # ---------------------------------------------------------------- ending tasks
    def stop_task(self, tid):
        task = self.get(tid)
        if task is None or task['status'] not in ACTIVE:
            return False
        task['stop'].set()
        task['log'].append('%s End requested by you.' % time.strftime('%H:%M:%S'))
        self.say('Ending Hermes task #%d ...' % tid)
        self.note('STOP requested #%d' % tid)
        self.refresh_window()
        return True

    def stop_all(self, quiet=False):
        active = self.running()
        for task in active:
            self.stop_task(task['id'])
        if not quiet:
            if active:
                self.say('Ending %d Hermes task(s) ...' % len(active))
                if self.cfg.get('speak_ack', True):
                    self.speak('Stopping Hermes.')
            else:
                self.say('No Hermes voice tasks are running.')
        return len(active)

    # ---------------------------------------------------------------- the Hermes run (worker thread)
    def worker(self, task, chain, hcfg):
        tid = task['id']

        def ask(ctl, is_clarify):
            reply = queue.Queue()
            self.q.put(('state', tid, 'Waiting for you'))
            self.q.put(('ask', tid, ctl, is_clarify, reply))
            waited = 0.0
            try:
                while waited < float(hcfg.get('approval_wait_s', 280)) and not task['stop'].is_set():
                    try:
                        return reply.get(timeout=0.5)
                    except queue.Empty:
                        waited += 0.5
                return {'choice': 'deny', 'answer': ''}
            finally:
                self.q.put(('state', tid, 'Running'))
        ctx = self.core.Ctx(lambda text: self.q.put(('log', tid, str(text))), ask, task['stop'].is_set)
        try:
            with self.gateway_lock:
                client, proc = self.core.ensure_gateway(hcfg, lambda text: self.q.put(('log', tid, str(text))),
                                                        Path(self.ns['DATA_DIR']))
                if proc is not None:
                    self.gateway = proc
            self.q.put(('state', tid, 'Running'))
            result = self.core.run_chain(client, task['instruction'], task['folder'], chain, hcfg, ctx)
        except Exception as error:
            self.note('worker error #%d:\n%s' % (tid, traceback.format_exc()))
            result = {'status': 'failed', 'model': '-', 'text': '', 'error': str(error), 'attempts': [], 'tools': 0}
        self.q.put(('done', tid, result))

    # ---------------------------------------------------------------- main-thread pump
    def pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == 'log':
                    task = self.get(item[1])
                    if task is not None:
                        task['log'].append('%s %s' % (time.strftime('%H:%M:%S'), item[2]))
                        del task['log'][:-500]
                    self.say('#%d: %s' % (item[1], item[2]))
                elif kind == 'state':
                    task = self.get(item[1])
                    if task is not None and task['status'] in ACTIVE:
                        task['status'] = item[2]
                elif kind == 'ask':
                    self.answer(item[1], item[2], item[3], item[4])
                elif kind == 'done':
                    self.finish(item[1], item[2])
                elif kind == 'status':
                    self.say(item[1])
                elif kind == 'text':
                    self.on_ptt_text(item[1])
                elif kind == 'record':
                    self.command(item[1], 'record button', 'lenient')
        except queue.Empty:
            pass
        except Exception:
            self.note('pump error:\n' + traceback.format_exc())
        self.app.after(200, self.pump)

    def answer(self, tid, ctl, is_clarify, reply):
        task = self.get(tid)
        title = ('Task #%d: %s' % (tid, (task or {}).get('instruction', '')))[:120]
        if task is not None and task['stop'].is_set():
            reply.put({'choice': 'deny', 'answer': ''})
            return
        if self.cfg.get('speak_ack', True):
            self.speak('Hermes needs your answer.')
        if is_clarify:
            question = str(ctl.get('question') or ctl.get('prompt') or json.dumps(ctl, ensure_ascii=False)[:600])
            text = simpledialog.askstring('Hermes asks a question', title + '\n\n' + question, parent=self.app)
            reply.put({'answer': text or ''})
        else:
            details = json.dumps(ctl, ensure_ascii=False, indent=2)[:1500]
            ok = messagebox.askyesno('Hermes asks permission', title + '\n\n' + details + '\n\nAllow this once?',
                                     parent=self.app)
            reply.put({'choice': 'once' if ok else 'deny'})

    def finish(self, tid, result):
        task = self.get(tid)
        if task is None:
            return
        status = result.get('status', 'failed')
        stopped = task['stop'].is_set() or status == 'cancelled'
        task['status'] = 'Stopped' if stopped else ('Completed' if status == 'completed' else 'Failed')
        task['model'] = result.get('model', '-')
        task['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
        body = (result.get('text') or '').strip()
        if task['status'] != 'Completed':
            body = (result.get('error') or '').strip() or body
        task['result'] = body[:6000]
        self.note('DONE #%d %s | model %s | %s' % (tid, task['status'], task['model'], body[:200].replace('\n', ' ')))
        self.save_history()
        try:
            folder = self.base / 'runs'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / ('%s_task%d.md' % (time.strftime('%Y%m%d-%H%M%S'), tid))).write_text(
                '# Hermes task #%d - %s - model %s\n\nTask: %s\nSource: %s\n\n%s\n\n## Activity\n%s\n' % (
                    tid, task['status'], task['model'], task['instruction'], task['source'], body,
                    '\n'.join(task['log'])), encoding='utf-8')
        except OSError:
            pass
        self.app.show('Hermes task #%d - %s (%s)\n%s' % (tid, task['status'], task['model'], body[:1500]), 'sys')
        self.say('Hermes task #%d: %s' % (tid, task['status']))
        if task.get('speak') and self.cfg.get('speak_result', True):
            if task['status'] == 'Completed':
                short = re.sub(r'[*_`#>]+', '', body)[:240]
                self.speak('Hermes finished. ' + short if short else 'Hermes finished.')
            elif task['status'] == 'Stopped':
                self.speak('Hermes task stopped.')
            else:
                self.speak('Hermes failed. ' + body[:150])
        try:
            self.app.bell()
        except Exception:
            pass
        self.refresh_window()

    def close(self):
        for task in self.running():
            task['stop'].set()
        self.save_history()
        if self.gateway is not None:
            try:
                self.gateway.terminate()
            except Exception:
                pass

    # ---------------------------------------------------------------- task list window
    def tick(self):
        try:
            count = len(self.running())
            self.tasks_text.set('Hermes tasks (%d running)...' % count if count else 'Hermes tasks...')
            if self.window is not None and self.window.winfo_exists():
                self.refresh_window()
        except Exception:
            pass
        self.app.after(700, self.tick)

    def selected_task(self):
        try:
            sel = self.table.selection()
            return self.get(int(sel[0])) if sel else None
        except Exception:
            return None

    def open_window(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.refresh_window()
            return
        win = self.window = tk.Toplevel(self.app)
        win.title('Cerry - Hermes tasks (voice and typed)')
        win.geometry('1050x720')
        ttk.Label(win, padding=8, wraplength=1000, text=(
            'Tasks started by voice or with /hermesnow run here. Select a task and press End task to stop it. '
            'The list is saved. Tasks opened with /hermes stay in the Agent... window.')).pack(fill='x')
        columns = ('id', 'task', 'status', 'model', 'started', 'source')
        self.table = ttk.Treeview(win, columns=columns, show='headings', selectmode='browse', height=9)
        for name, width in (('id', 40), ('task', 430), ('status', 120), ('model', 190), ('started', 140), ('source', 110)):
            self.table.heading(name, text=name.title())
            self.table.column(name, width=width, anchor='w')
        self.table.pack(fill='x', padx=8)
        self.table.bind('<<TreeviewSelect>>', lambda e: self.show_details(force=True))
        bar = ttk.Frame(win, padding=6)
        bar.pack(fill='x')
        for text, command in (('End task', self.end_selected), ('End all running', lambda: self.stop_all()),
                              ('Run again', self.run_again), ('Clear finished', self.clear_finished),
                              ('Open results folder', self.open_results)):
            ttk.Button(bar, text=text, command=command).pack(side='left', padx=3)
        ttk.Label(win, text='Activity and result of the selected task:').pack(anchor='w', padx=8)
        self.details = scrolledtext.ScrolledText(win, wrap='word', state='disabled', height=22)
        self.details.pack(fill='both', expand=True, padx=8, pady=6)
        self.detail_key = None
        self.refresh_window()

    def refresh_window(self):
        try:
            if self.window is None or not self.window.winfo_exists():
                return
            keep = self.table.selection()
            self.table.delete(*self.table.get_children())
            for t in reversed(self.tasks):
                self.table.insert('', 'end', iid=str(t['id']),
                                  values=(t['id'], t['instruction'][:90], t['status'], t.get('model', '-'),
                                          t.get('started', '')[5:16], t.get('source', '')))
            if keep and self.table.exists(keep[0]):
                self.table.selection_set(keep[0])
            self.show_details()
        except tk.TclError:
            pass

    def show_details(self, force=False):
        task = self.selected_task()
        key = None if task is None else (task['id'], task['status'], len(task['log']), len(task['result']))
        if not force and key == getattr(self, 'detail_key', None):
            return
        self.detail_key = key
        text = '' if task is None else (
            'Task #%d  (%s)\n%s\n\nStatus: %s   Model: %s\nStarted: %s   Finished: %s\n\n--- Result ---\n%s\n\n--- Activity ---\n%s' % (
                task['id'], task.get('source', ''), task['instruction'], task['status'], task.get('model', '-'),
                task.get('started', ''), task.get('finished', '') or '-', task.get('result') or '(none yet)',
                '\n'.join(task['log'][-200:])))
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', text)
        self.details.configure(state='disabled')

    def end_selected(self):
        task = self.selected_task()
        if task is None:
            messagebox.showinfo('Hermes tasks', 'Select a task first.', parent=self.window)
        elif not self.stop_task(task['id']):
            messagebox.showinfo('Hermes tasks', 'This task is not running.', parent=self.window)

    def run_again(self):
        task = self.selected_task()
        if task is None:
            messagebox.showinfo('Hermes tasks', 'Select a task first.', parent=self.window)
        elif task['status'] in ACTIVE:
            messagebox.showinfo('Hermes tasks', 'This task is still running.', parent=self.window)
        else:
            self.run(task['instruction'], 'typed')

    def clear_finished(self):
        self.tasks = [t for t in self.tasks if t['status'] in ACTIVE]
        self.save_history()
        self.refresh_window()

    def open_results(self):
        folder = self.base / 'runs'
        folder.mkdir(parents=True, exist_ok=True)
        opener = self.ns.get('open_folder')
        if opener:
            opener(folder)

    # ---------------------------------------------------------------- push to talk
    def toggle_ptt(self):
        app = self.app
        if getattr(app, 'convo_active', False):
            self.say('Conversation mode is on: just say "Cerry, do ..." or end it first.')
            return
        if self.recording:
            self.recording = False
            self.button_text.set('Voice -> Hermes')
            try:
                path = app.recorder.stop(self.tmp)
            except Exception as error:
                self.say('Recording problem: %s' % error)
                return
            if not path:
                self.say('Recording too short - nothing to send to Hermes.')
                return
            self.say('Transcribing your command ...')
            args = (str(path), app.voice_cfg, self.ns['get_client'], app.policy.local_only)
            threading.Thread(target=self.transcribe_job, args=args, daemon=True).start()
            return
        if getattr(app.recorder, 'recording', False):
            self.say('The normal Record button is recording. Finish that first.')
            return
        try:
            app.speaker.stop()
            app.recorder.start()
        except Exception as error:
            self.say('Microphone error: %s' % str(error)[:150])
            return
        self.recording = True
        self.button_text.set('Stop and send to Hermes')
        self.say('Recording your command ... click again when you have finished.')

    def transcribe_job(self, path, voice_cfg, get_client, local_only):
        try:
            voice = importlib.import_module('voice')
            text = voice.transcribe(path, voice_cfg, get_client, local_only,
                                    on_status=lambda t: self.q.put(('status', t)))
            self.q.put(('text', text))
        except Exception as error:
            self.q.put(('status', 'Transcription failed: %s' % str(error)[:150]))
        finally:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def on_ptt_text(self, text):
        if text and text.strip():
            if not self.command(text, 'push-to-talk', 'forced'):
                self.say('Could not find a command in: %s' % text[:100])
        else:
            self.say('No speech detected.')


def install(ns):
    App = ns['App']
    if App.__dict__.get('_hermes_voice_v3'):
        return
    App._hermes_voice_v3 = True
    data_dir = Path(ns['DATA_DIR'])
    log_file = data_dir / 'hermes_voice.log'

    def note(text):
        try:
            if log_file.exists() and log_file.stat().st_size > 300000:
                log_file.write_text('', encoding='utf-8')
            with log_file.open('a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
        except OSError:
            pass

    try:
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg_path = data_dir / 'hermes_voice.json'
        try:
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                cfg.update(saved)
        except (OSError, ValueError):
            try:
                cfg_path.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False), encoding='utf-8')
            except OSError:
                pass
        core = importlib.import_module('cerry_hermes_core')
        original_init, original_send = App.__init__, App.send
        original_handle = getattr(App, 'handle_convo_text', None)
        original_close = getattr(App, 'on_close', None)

        def init(app, *args, **kwargs):
            original_init(app, *args, **kwargs)
            try:
                hv = app._hv = STATE['hv'] = HermesVoice(app, ns, cfg, core, note)
                anchor = find_button(app, 'Agent...')
                host = anchor.master if anchor is not None else None
                placed = False
                if host is not None:
                    try:
                        ttk.Button(host, textvariable=hv.tasks_text, command=hv.open_window).pack(
                            side='left', padx=2, after=anchor)
                        ttk.Button(host, textvariable=hv.button_text, command=hv.toggle_ptt).pack(
                            side='left', padx=2, after=anchor)
                        placed = True
                    except Exception:
                        note('could not add the buttons next to Agent...:\n' + traceback.format_exc())
                if not placed:
                    strip = ttk.Frame(app, padding=(6, 0))
                    strip.pack(fill='x', before=app.inp.master)
                    ttk.Button(strip, textvariable=hv.button_text, command=hv.toggle_ptt).pack(side='left', padx=2)
                    ttk.Button(strip, textvariable=hv.tasks_text, command=hv.open_window).pack(side='left', padx=2)
                    ttk.Label(strip, style='Muted.TLabel',
                              text='Say: "Cerry, do ..." / "Hermes, ..." / "Cerry, stop Hermes".  Typed: /hermesnow ...'
                              ).pack(side='left', padx=8)
                # The existing "Stop all agents" button also ends the voice and typed tasks.
                try:
                    stop_button = find_button(app, 'Stop all agents')
                    if stop_button is not None:
                        old = str(stop_button.cget('command') or '')

                        def stop_everything():
                            if old:
                                try:
                                    app.tk.call(old)
                                except Exception:
                                    note('original Stop all agents command failed:\n' + traceback.format_exc())
                            hv.stop_all()
                        stop_button.configure(command=stop_everything)
                except Exception:
                    note('Stop all agents hook failed:\n' + traceback.format_exc())
                note('ready: buttons %s; %d saved task(s)' % ('next to Agent...' if placed else 'in their own strip', len(hv.tasks)))
                app.status.set('Hermes voice ready: say "Cerry, do <task>" in conversation mode (log: hermes_voice.log)')
            except Exception:
                note('init failed:\n' + traceback.format_exc())

        def send(app):
            try:
                text = app.inp.get('1.0', 'end').strip()
                hv = getattr(app, '_hv', None)
                if hv is not None and text:
                    lowered = text.lower()
                    if lowered == '/hermestasks':
                        app.inp.delete('1.0', 'end')
                        hv.open_window()
                        return
                    if not getattr(app, 'busy', False):
                        if lowered.startswith('/hermesnow'):
                            task = text[len('/hermesnow'):].strip()
                            app.inp.delete('1.0', 'end')
                            if task:
                                hv.run(task, 'typed')
                            else:
                                hv.say('Type /hermesnow followed by what Hermes should do.')
                            return
                        if not text.startswith('/') and hv.command(text, 'typed', 'strict'):
                            app.inp.delete('1.0', 'end')
                            return
            except Exception:
                note('send hook error:\n' + traceback.format_exc())
            return original_send(app)

        App.__init__, App.send = init, send
        hooks = ['buttons', 'typed commands', 'task list']
        if original_handle is not None:
            def handle_convo_text(app, text):
                handled = False
                try:
                    handled = app._hv.command(text, 'conversation', 'lenient')
                except Exception:
                    note('conversation hook error:\n' + traceback.format_exc())
                if handled:
                    if getattr(app, 'convo', None) is not None:
                        app.convo.turn_done()
                    return
                return original_handle(app, text)
            App.handle_convo_text = handle_convo_text
            hooks.append('conversation mode')
        if original_close is not None:
            def on_close(app):
                try:
                    app._hv.close()
                except Exception:
                    pass
                return original_close(app)
            App.on_close = on_close
        try:
            voice = importlib.import_module('voice')
            original_transcribe = voice.transcribe

            def transcribe(*args, **kwargs):
                text = original_transcribe(*args, **kwargs)
                try:
                    hv = STATE['hv']
                    caller = sys._getframe(1).f_code.co_name
                    # The Record button transcribes inside a function called "job"; conversation mode does not.
                    if (caller == 'job' and hv is not None and cfg.get('enabled', True)
                            and cfg.get('record_button_commands', True) and text and hv.parse(text, 'lenient')):
                        hv.q.put(('record', text))
                        return ''
                except Exception:
                    note('transcribe hook error:\n' + traceback.format_exc())
                return text
            voice.transcribe = transcribe
            hooks.append('Record button')
        except Exception:
            note('Record button hook not installed:\n' + traceback.format_exc())
        note('installed: ' + ', '.join(hooks))
    except Exception:
        note('INSTALL FAILED:\n' + traceback.format_exc())
