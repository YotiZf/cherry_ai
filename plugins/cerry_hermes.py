"""Cerry + Hermes Agent: tasks ("missions") carried out by Hermes, with Cerry's models and a safe fallback chain.

Switch on:  add "cerry_hermes" to ai_workspace_data/plugins.json   (needs cerry_hermes_core.py next to this file)
Use:        "Agent..." button above the input box, or type  /cherry <what Cherry should do>
Settings:   ai_workspace_data/hermes.json   (created on first start)
Tasks:      ai_workspace_data/hermes/tasks.json     Results: ai_workspace_data/hermes/runs/
"""
import datetime as dt
import json
import os
import queue
import re
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

import cerry_hermes_core as core

INSTALL_FLAG = "_hm_installed"


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Agent:
    def __init__(self, app, ns, cfg):
        self.app, self.ns, self.cfg = app, ns, cfg
        self.base = ns["DATA_DIR"] / "hermes"
        (self.base / "runs").mkdir(parents=True, exist_ok=True)
        self.tasks_file = self.base / "tasks.json"
        hub = ns['_hermes_hub']
        if hub.tasks is None:
            try:
                hub.tasks = json.loads(self.tasks_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                hub.tasks = []
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.running = False
        self.current = None
        self.gateway = None              # process we started ourselves (stopped when Cerry closes)
        self.window = None
        self.in_dialog = False
        self.log_lines = []
        self.stop_buttons = []
        app.after(150, self.tick)

    @property
    def tasks(self):
        return self.ns['_hermes_hub'].tasks

    @tasks.setter
    def tasks(self, value):
        self.ns['_hermes_hub'].tasks = value

    def task_running(self, task):
        return any(a.running and a.current and a.current['id'] == task['id']
                   for a in self.ns['_hermes_hub'].agents)

    # ------------------------------------------------------------ helpers
    def routes(self):
        return core.build_routes(self.ns["MODELS"], self.cfg)

    def save(self):
        atomic_json(self.tasks_file, self.tasks)
        for agent in self.ns['_hermes_hub'].agents:
            if agent is not self:
                agent.refresh()

    def mine(self):
        return [t for t in self.tasks if t.get("workspace") == self.app.ws.name]

    def set_buttons(self):
        for b in self.stop_buttons:
            try:
                b.config(state="normal" if self.running else "disabled")
            except tk.TclError:
                pass

    def parent(self):
        w = self.window
        try:
            return w if w is not None and w.winfo_exists() else self.app
        except tk.TclError:
            return self.app

    def say(self, text):
        """Thread-safe: add a line to the activity log."""
        text = re.sub(r"(?i)\bHermes\b", "Cherry", str(text))
        self.q.put(("log", text))

    # ------------------------------------------------------------ window
    def panel(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.refresh()
            return
        win = tk.Toplevel(self.app)
        self.window = win
        win.protocol('WM_DELETE_WINDOW', win.withdraw)
        win.title("Cherry agent tasks")
        win.geometry("1000x700")
        ttk.Label(win, padding=8, wraplength=960, text=(
            "Cherry carries out each task with the models in its list, in order. If a cloud model shows no sign of "
            "life within the time limit, Cerry stops that attempt and tries the next one; local models come last. "
            "Once Cherry has started using tools, Cerry never switches model by itself.")).pack(fill="x")
        cols = ("name", "status", "model", "last run")
        self.table = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse", height=6)
        for c, w in zip(cols, (330, 200, 250, 180)):
            self.table.heading(c, text=c.title())
            self.table.column(c, width=w)
        self.table.pack(fill="x", padx=8)
        row = ttk.Frame(win, padding=6)
        row.pack(fill="x")
        stop = ttk.Button(row, text="Stop", command=self.request_stop, state="disabled")
        self.stop_buttons.append(stop)
        for text, fn in (("New", lambda: self.editor()), ("Edit", self.edit_selected), ("Run now", self.run_selected)):
            ttk.Button(row, text=text, command=fn).pack(side="left", padx=3)
        stop.pack(side="left", padx=3)
        for text, fn in (("Delete", self.delete_selected), ("Check connection", self.check_connection),
                         ("Results folder", self.open_results), ("Settings file", self.open_settings)):
            ttk.Button(row, text=text, command=fn).pack(side="left", padx=3)
        ttk.Label(win, text="Activity").pack(anchor="w", padx=8)
        self.activity = scrolledtext.ScrolledText(win, height=14, wrap="word", state="disabled")
        self.activity.pack(fill="both", expand=True, padx=8)
        ttk.Label(win, text="Result").pack(anchor="w", padx=8, pady=(6, 0))
        self.result = scrolledtext.ScrolledText(win, height=10, wrap="word", state="disabled")
        self.result.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.set_text(self.activity, "\n".join(self.log_lines))
        self.set_buttons()
        self.refresh()

    @staticmethod
    def set_text(widget, text):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.config(state="disabled")
        widget.see("end")

    def refresh(self):
        if self.window is None or not self.window.winfo_exists():
            return
        keep = self.table.selection()
        self.table.delete(*self.table.get_children())
        for t in self.mine():
            self.table.insert("", "end", iid=t["id"], values=(t["name"], t.get("last_status", "-"),
                                                              t.get("last_model", "-"), t.get("last_run", "-")))
        if keep and self.table.exists(keep[0]):
            self.table.selection_set(keep[0])

    def selected(self):
        if self.window is None or not self.window.winfo_exists():
            return None
        sel = self.table.selection()
        return next((t for t in self.tasks if sel and t["id"] == sel[0]), None)

    # ------------------------------------------------------------ editor
    def editor(self, draft=None):
        if self.in_dialog:
            return
        routes = self.routes()
        if not routes:
            messagebox.showerror("Cherry", "No usable models: add Gemini, Groq or OpenRouter models to Cerry first.",
                                 parent=self.app)
            return
        d = dict(draft or {})
        try:
            selected = self.app.selected_model()["label"]
        except Exception:
            selected = None
        labels = d.get("chain") or core.default_chain(routes, selected)
        local_names = {r["label"] for r in routes if r["local"]}
        show = lambda l: l + ("  [local]" if l in local_names else "")
        self.in_dialog = True
        win = tk.Toplevel(self.window if self.window is not None and self.window.winfo_exists() else self.app)
        win.title("Cherry task - review")
        win.transient(self.app)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        name = tk.StringVar(value=d.get("name", "New task"))
        folder = tk.StringVar(value=d.get("folder", ""))
        ttk.Label(frm, text="Name").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=name, width=70).grid(row=0, column=1, columnspan=2, sticky="ew", padx=6)
        ttk.Label(frm, text="Folder Cherry works in").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=folder, width=60).grid(row=1, column=1, sticky="ew", padx=6)

        def browse():
            p = filedialog.askdirectory(parent=win, title="Folder for this task")
            if p:
                folder.set(p)
        ttk.Button(frm, text="Browse...", command=browse).grid(row=1, column=2)
        ttk.Label(frm, text="What should Cherry do?").grid(row=2, column=0, sticky="nw", pady=3)
        text = scrolledtext.ScrolledText(frm, width=70, height=8, wrap="word")
        text.grid(row=2, column=1, columnspan=2, sticky="ew", padx=6)
        text.insert("1.0", d.get("instruction", ""))
        ttk.Label(frm, text="Models, in order\n(local models always last)").grid(row=3, column=0, sticky="nw", pady=6)
        box = tk.Listbox(frm, height=7, exportselection=False)
        box.grid(row=3, column=1, sticky="nsew", padx=6, pady=6)
        for l in core.order_chain(labels, routes):
            box.insert("end", show(l["label"]))
        side = ttk.Frame(frm)
        side.grid(row=3, column=2, sticky="n")

        def move(delta):
            sel = box.curselection()
            if not sel:
                return
            i, j = sel[0], sel[0] + delta
            if 0 <= j < box.size():
                item = box.get(i)
                box.delete(i)
                box.insert(j, item)
                box.selection_set(j)

        def remove():
            sel = box.curselection()
            if sel and box.size() > 1:
                box.delete(sel[0])
        ttk.Button(side, text="Up", command=lambda: move(-1)).pack(fill="x", pady=1)
        ttk.Button(side, text="Down", command=lambda: move(1)).pack(fill="x", pady=1)
        ttk.Button(side, text="Remove", command=remove).pack(fill="x", pady=1)
        add_var = tk.StringVar()
        add = ttk.Combobox(frm, textvariable=add_var, state="readonly", values=[show(r["label"]) for r in routes], width=40)
        add.grid(row=4, column=1, sticky="w", padx=6)

        def add_model():
            v = add_var.get()
            if v and v not in box.get(0, "end"):
                box.insert("end", v)
        ttk.Button(frm, text="Add model", command=add_model).grid(row=4, column=2)
        ttk.Label(frm, foreground="#888", wraplength=700, text=(
            "Cherry asks you before risky commands (approval settings are in hermes.json). The folder is a clear instruction "
            "to Cherry, not a lock; to limit where it may write, list folders under safe_roots in hermes.json."
        )).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        bar = ttk.Frame(frm)
        bar.grid(row=6, column=0, columnspan=3, sticky="e")

        def close():
            self.in_dialog = False
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

        def approve():
            try:
                shown = list(box.get(0, "end"))
                chosen = [s.replace("  [local]", "") for s in shown]
                task = {"id": d.get("id") or uuid.uuid4().hex, "name": name.get().strip(),
                        "instruction": text.get("1.0", "end").strip(), "folder": folder.get().strip(),
                        "chain": [r["label"] for r in core.order_chain(chosen, routes)],
                        "workspace": d.get("workspace", self.app.ws.name),
                        "last_status": d.get("last_status", "Saved"), "last_model": d.get("last_model", "-"),
                        "last_run": d.get("last_run", "-")}
                if not task["name"] or not task["instruction"]:
                    raise ValueError("Give the task a name and tell Cherry what to do.")
                if task["folder"] and not Path(task["folder"]).is_dir():
                    raise ValueError("The folder does not exist.")
                if not task["chain"]:
                    raise ValueError("Choose at least one model.")
                if not messagebox.askyesno("Save this task?", json.dumps(task, ensure_ascii=False, indent=2), parent=win):
                    return
                self.tasks = [t for t in self.tasks if t["id"] != task["id"]] + [task]
                self.save()
                close()
                self.refresh()
                self.app.show(f"Cherry task saved: {task['name']}. Open Agent... and press Run now.")
            except Exception as e:
                messagebox.showerror("Task not saved", str(e), parent=win)
        ttk.Button(bar, text="Review and save", command=approve).pack(side="right")
        ttk.Button(bar, text="Cancel", command=close).pack(side="right", padx=4)

    def edit_selected(self):
        t = self.selected()
        if t and not self.running and not self.task_running(t):
            self.editor(t)

    def delete_selected(self):
        t = self.selected()
        if t and not self.running and not self.task_running(t) and messagebox.askyesno("Delete task", t["name"], parent=self.parent()):
            self.tasks.remove(t)
            self.save()
            self.refresh()

    def open_results(self):
        opener = self.ns.get("open_folder")
        target = self.base / "runs"
        opener(target) if opener else os.startfile(str(target))

    def open_settings(self):
        path = self.ns["DATA_DIR"] / "hermes.json"
        try:
            os.startfile(str(path))
        except Exception as e:
            messagebox.showinfo("Settings file", f"{path}\n\n{e}", parent=self.parent())

    # ------------------------------------------------------------ running
    def run_selected(self):
        t = self.selected()
        if t:
            self.start(t)

    def start(self, task):
        if self.task_running(task):
            messagebox.showinfo('Cherry', 'This saved task is already running in an agent window.', parent=self.parent())
            return
        if self.running:
            messagebox.showinfo("Cherry", "A task is already running. Press Stop first.", parent=self.parent())
            return
        if getattr(self.app, "busy", False):
            messagebox.showinfo("Cherry", "Wait for the current chat answer to finish.", parent=self.parent())
            return
        chain = core.order_chain(task["chain"], self.routes())
        if not chain:
            messagebox.showerror("Cherry", "None of this task's models exist any more. Edit the task.", parent=self.parent())
            return
        summary = (f"Task: {task['name']}\nFolder: {task.get('folder') or '(none)'}\n"
                   f"Models in order: {', '.join(r['label'] for r in chain)}\n\n{task['instruction']}\n\n"
                   "Cherry may create files and run commands on this computer. Start now?")
        if not messagebox.askyesno("Run this task with Cherry?", summary, parent=self.parent()):
            return
        self.running, self.current = True, task
        self.stop_flag.clear()
        self.log_lines = []
        if self.window is not None and self.window.winfo_exists():
            self.set_text(self.activity, "")
            self.set_text(self.result, "")
        task["last_status"], task["last_model"] = "Running", "-"
        self.save()
        self.refresh()
        self.set_buttons()
        self.say(f"Cherry is working on '{task['name']}'.")
        threading.Thread(target=self.worker, args=(task, chain), daemon=True).start()

    def worker(self, task, chain):
        def ask(ctl, is_clarify):
            reply = queue.Queue()
            self.q.put(("ask", ctl, is_clarify, reply))
            waited = 0.0
            while waited < self.cfg["approval_wait_s"] and not self.stop_flag.is_set():
                try:
                    return reply.get(timeout=0.5)
                except queue.Empty:
                    waited += 0.5
            return {"choice": "deny", "answer": ""}
        ctx = core.Ctx(self.say, ask, self.stop_flag.is_set)
        try:
            client, proc = self.ns['_hermes_hub'].connect(self.say, self.base)
            if proc is not None:
                self.gateway = proc
            if self.stop_flag.is_set():
                result = {'status': 'cancelled', 'model': '-', 'text': '', 'error': '', 'attempts': [], 'tools': 0}
            else:
                result = core.run_chain(client, task["instruction"], task.get("folder", ""), chain, self.cfg, ctx)
        except core.HermesError as e:
            result = {"status": "failed", "model": "-", "text": "", "error": str(e), "attempts": [], "tools": 0}
        except Exception as e:
            result = {"status": "failed", "model": "-", "text": "", "error": f"{type(e).__name__}: {e}",
                      "attempts": [], "tools": 0}
        self.q.put(("done", task, result))

    def request_stop(self):
        if self.running:
            self.stop_flag.set()
            self.say("Cherry is stopping at the next safe point ...")

    def check_connection(self):
        if self.running:
            return

        def job():
            try:
                client, proc = self.ns['_hermes_hub'].connect(self.say, self.base)
                if proc is not None:
                    self.gateway = proc
                feats = client.capabilities().get("features", {})
                self.say("Cherry agent connection is ready. Stop: %s, live events: %s." %
                         (feats.get("run_stop"), feats.get("run_events_sse")))
            except Exception as e:
                self.say(f"Connection check failed: {e}")
        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------ main-thread loop
    def tick(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    stamp = dt.datetime.now().strftime("%H:%M:%S")
                    self.log_lines.append(f"{stamp}  {item[1]}")
                    if self.window is not None and self.window.winfo_exists():
                        self.set_text(self.activity, "\n".join(self.log_lines))
                    self.app.status.set(item[1][:150])
                elif item[0] == "ask":
                    self.answer(*item[1:])
                elif item[0] == "done":
                    self.finish(item[1], item[2])
        except queue.Empty:
            pass
        except Exception as e:
            self.log_lines.append(f"Internal error: {e}")
        self.app.after(150, self.tick)

    def answer(self, ctl, is_clarify, reply):
        parent = self.parent()
        if is_clarify:
            question = str(ctl.get("question") or ctl.get("prompt") or json.dumps(ctl, ensure_ascii=False)[:600])
            text = simpledialog.askstring("Cherry asks a question", question, parent=parent)
            reply.put({"answer": text or ""})
        else:
            details = json.dumps(ctl, ensure_ascii=False, indent=2)[:1500]
            ok = messagebox.askyesno("Cherry asks permission", details + "\n\nAllow this once?", parent=parent)
            reply.put({"choice": "once" if ok else "deny"})

    def finish(self, task, result):
        self.running = False
        self.set_buttons()
        status = result.get("status", "failed")
        label = {"completed": "Completed", "cancelled": "Stopped"}.get(status, "Failed")
        task["last_status"] = label
        task["last_model"] = result.get("model", "-")
        task["last_run"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.save()
        body = result.get("text") or ""
        if status != "completed":
            body = (result.get("error") or "") + (("\n\n" + body) if body else "")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^\w\-]+", "_", task["name"])[:40] or "task"
        report = self.base / "runs" / f"{stamp}_{slug}_{uuid.uuid4().hex[:8]}.md"
        try:
            report.write_text(f"# {task['name']}\n\nStatus: {label} - model: {task['last_model']}\n\n"
                              f"Folder: {task.get('folder') or '-'}\n\n## Task\n{task['instruction']}\n\n"
                              f"## Result\n{body}\n\n## Activity\n" + "\n".join(self.log_lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        if self.window is not None and self.window.winfo_exists():
            self.set_text(self.result, body or "(no text)")
        self.refresh()
        icon = {"Completed": "OK", "Stopped": "Stopped"}.get(label, "FAILED")
        display_body = re.sub(r"(?i)\bHermes\b", "Cherry", body)
        self.app.show(f"Cherry task '{task['name']}' - {icon} ({task['last_model']}).\n\n"
                      f"{display_body[:1500]}\n\nReport saved: {report.name}")
        try:
            self.app.bell()
        except tk.TclError:
            pass

    def close(self):
        self.request_stop()


class AgentHub:
    """Independent original Agent panels sharing saved tasks and a single gateway."""
    def __init__(self, app, ns, cfg):
        self.app, self.ns, self.cfg = app, ns, cfg
        self.tasks = None
        self.agents = []
        self.connection = None
        self.connection_lock = threading.Lock()

    @property
    def running(self):
        return any(agent.running for agent in self.agents)

    def panel(self):
        agent = Agent(self.app, self.ns, self.cfg)
        self.agents.append(agent)
        agent.panel()
        agent.window.title(f'Cherry agent tasks - window {len(self.agents)}')
        return agent

    def editor(self, draft=None):
        self.panel().editor(draft)

    def request_stop(self):
        for agent in self.agents:
            agent.request_stop()

    def close(self):
        self.request_stop()
        # Do not terminate the host gateway: other sessions may be using it.

    def connect(self, log, base):
        with self.connection_lock:
            if self.connection is not None:
                url, key = self.connection
                client = core.Client(url, key)
                if client.health():
                    return client, None
            client, process = core.ensure_gateway(self.cfg, log, base)
            self.connection = (client.url, client.key)
            return client, None


def install(ns):
    App = ns["App"]
    if App.__dict__.get(INSTALL_FLAG):
        return
    setattr(App, INSTALL_FLAG, True)
    cfg = core.load_config(ns["DATA_DIR"] / "hermes.json")
    orig_init, orig_send = App.__init__, App.send
    orig_stop = App.stop
    orig_close = getattr(App, "on_close", None)

    def init(app, *a, **k):
        orig_init(app, *a, **k)
        app.hermes = AgentHub(app, ns, cfg)
        ns['_hermes_hub'] = app.hermes
        strip = ttk.Frame(app, padding=(6, 0))
        strip.pack(fill="x", before=app.inp.master)
        ttk.Button(strip, text="Agent...", command=app.hermes.panel).pack(side="left", padx=2)
        def stop_all():
            if app.hermes.running and messagebox.askyesno('Stop all agents',
                    'Request stopping every running Cherry job?', parent=app):
                app.hermes.request_stop()
        ttk.Button(strip, text='Stop all agents', command=stop_all).pack(side='left', padx=2)
        ttk.Label(strip, text="Cherry agent - type /cherry <what to do>", style="Muted.TLabel").pack(side="left", padx=8)

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        if re.match(r"(?is)^/cherry(?:\s|$)", text):
            app.inp.delete("1.0", "end")
            rest = text[len("/cherry"):].strip()
            if rest:
                app.hermes.editor({"name": rest[:50], "instruction": rest})
            else:
                app.hermes.panel()
            return
        return orig_send(app)

    def stop(app):
        if hasattr(app, "hermes") and app.hermes.running:
            app.hermes.request_stop()
        return orig_stop(app)

    def on_close(app):
        def finish_close():
            if orig_close:
                orig_close(app)
            else:
                app.destroy()
        if hasattr(app, 'hermes') and app.hermes.running:
            if not messagebox.askyesno('Running agents', 'Stop all agent jobs before closing Cerry?', parent=app):
                return
            app.hermes.request_stop()
            def wait():
                if app.hermes.running:
                    app.after(250, wait)
                else:
                    finish_close()
            app.after(250, wait)
        else:
            finish_close()

    App.__init__, App.send, App.stop, App.on_close = init, send, stop, on_close
