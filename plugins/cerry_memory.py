"""Cerry memory: Cerry remembers things for you - globally and per workspace - and you can edit all of it.

Two kinds of memory (plain Markdown files, one memory per "- " line, so you can also edit them in any editor):
    Global     ai_workspace_data/global_memory.md      true in every conversation
    Workspace  workspaces/<name>/notes.md              true only in this workspace
They are the same files Cerry always put into the prompt, so /remember, /note and the old Edit buttons still work.

How memories are made
  * You say it:   "remember that I write my cover letters in English"   (or "merk dir ...")
                  scope: words like "always/everywhere" -> global, "this project" -> workspace, otherwise your default
                  (/remember <text> = global and /note <text> = workspace work as before)
  * Cerry notices: after an answer, a model looks at the last exchange and suggests lasting facts.
                  Auto memory: Off | Ask me (suggestions wait in the Memory window - default) | Save automatically
  * You edit:     "Memory..." window or /memory: add, edit, delete, pin, move between global and workspace.
                  /forget <words> deletes the best matching memory (after asking).
Long memories do not flood the prompt: above the budget only pinned memories and those that match your question are sent.
Never stored: lines that look like passwords, API keys, tokens or ID numbers.
Settings: ai_workspace_data/memory.json   Pending suggestions: ai_workspace_data/memory_pending.json
Switch on: "cerry_memory" in ai_workspace_data/plugins.json (needs memory_engine.py next to it)
"""
import json
import queue
import re
import threading
import time
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, simpledialog

import memory_engine as me
from turn_context import TurnContext

DEFAULTS = {"mode": "ask", "budget": 2500, "model": "", "prefilter": True, "default_scope": "global",
            "cooldown_s": 20, "remember_phrase": True}
MODE_LABELS = {"Off": "off", "Ask me": "ask", "Save automatically": "auto"}
MODE_NAMES = {v: k for k, v in MODE_LABELS.items()}


class Memory:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.cfg_path = Path(ns["DATA_DIR"]) / "memory.json"
        self.pending_path = Path(ns["DATA_DIR"]) / "memory_pending.json"
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        try:
            self.pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.pending = []
        self.q = queue.Queue()
        self.working = False
        self.last_run = 0.0
        self.panel = None

    # ------------------------------------------------------------ files
    def file(self, scope, workspace=None):
        if scope == "global":
            return me.MemoryFile(self.ns["GLOBAL_MEMORY"])
        if workspace is None:
            return me.MemoryFile(self.app.ws.notes_path)
        root = Path(self.ns["WS_DIR"]).resolve()
        path = (root / workspace).resolve()
        if path.parent != root or not path.is_dir():
            raise ValueError("The workspace for this suggestion is no longer available.")
        return me.MemoryFile(path / "notes.md")

    def other(self, scope):
        return "workspace" if scope == "global" else "global"

    def label(self, scope):
        return "global memory" if scope == "global" else f"memory of workspace '{self.app.ws.name}'"

    def save_cfg(self):
        try:
            self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def save_pending(self):
        try:
            self.pending_path.write_text(json.dumps(self.pending, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def say(self, text):
        try:
            self.app.status.set(text)
        except Exception:
            pass

    # ------------------------------------------------------------ saving / suggestions
    def remember(self, text, scope, workspace=None):
        added, item = self.file(scope, workspace).add(text)
        return added

    def add_pending(self, text, scope, workspace):
        existing = [i["text"] for i in self.file(scope, workspace).items()]
        existing += [p["text"] for p in self.pending if p["scope"] == scope and
                     (scope == "global" or p.get("workspace") == workspace)]
        if any(me.similar(text, e) for e in existing):
            return False
        self.pending.append({"id": uuid.uuid4().hex[:8], "text": text, "scope": scope, "workspace": workspace,
                             "when": time.strftime("%Y-%m-%d %H:%M")})
        self.save_pending()
        return True

    # ------------------------------------------------------------ automatic suggestions
    def after_reply(self, version, turn, force=False):
        mode = self.cfg.get("mode", "ask")
        if (mode == "off" and not force) or self.working or not version:
            return
        now = time.time()
        if not force and now - self.last_run < float(self.cfg.get("cooldown_s", 20)):
            return
        if not (turn.global_memory or turn.workspace_memory):
            return
        user = turn.user_text
        if not user or user.startswith("/"):
            return
        if not force and self.cfg.get("prefilter", True) and not me.looks_memorable(user):
            return
        self.last_run = now
        label = self.cfg.get("model") or ""
        model = next((m for m in self.ns["MODELS"] if m["label"] == label), None) or turn.model
        ws_name = turn.workspace.name
        g_items = [i["text"] for i in self.file("global").items()] if turn.global_memory else []
        w_items = [i["text"] for i in self.file("workspace", ws_name).items()] if turn.workspace_memory else []
        scopes = {scope for scope, enabled in (("global", turn.global_memory), ("workspace", turn.workspace_memory))
                  if enabled}
        prompt = me.extraction_prompt(ws_name, g_items, w_items, user, version.get("content", ""))
        self.working = True
        threading.Thread(target=self._extract, args=(model, prompt, g_items + w_items, ws_name,
                                                      turn.policy, scopes), daemon=True).start()
        self.app.after(700, self.poll)

    def _extract(self, model, prompt, existing, ws_name, policy, scopes):
        routing, streaming = self.ns["routing"], self.ns["streaming"]
        try:
            order, _ = routing.build_order(self.ns["MODELS"], model, policy)
            msgs = [{"role": "system", "content": me.EXTRACT_SYSTEM}, {"role": "user", "content": prompt}]
            res = streaming.run_chat(order, msgs, self.ns["get_client"], streaming.CancelToken(), policy, None, None)
            items = me.parse_extraction(res["answer"], existing)
            self.q.put(("items", ws_name, [item for item in items if item["scope"] in scopes]))
        except Exception as e:
            self.q.put(("error", ws_name, str(e)[:120]))

    def poll(self):
        try:
            while True:
                kind, ws_name, data = self.q.get_nowait()
                self.working = False
                if kind == "error":
                    self.say(f"Memory check skipped: {data}")
                    continue
                self.deliver(ws_name, data)
        except queue.Empty:
            pass
        if self.working:
            self.app.after(700, self.poll)

    def deliver(self, ws_name, items):
        if not items:
            return
        saved, queued = [], 0
        for it in items:
            if self.cfg.get("mode") == "auto" and (it["scope"] == "global" or ws_name == self.app.ws.name):
                if self.remember(it["text"], it["scope"]):
                    saved.append(it["text"])
            elif self.add_pending(it["text"], it["scope"], ws_name):
                queued += 1
        if saved:
            self.say(f"Remembered {len(saved)} new thing(s): {saved[0][:80]} - see Memory... to edit or delete.")
        elif queued:
            self.say(f"Cerry suggests {queued} memory item(s) - open Memory... to keep or dismiss them.")
        if self.panel is not None:
            self.panel.refresh(force=True)

    # ------------------------------------------------------------ what goes into the prompt
    def shrink_prompt(self, msgs, user_text):
        budget = int(self.cfg.get("budget", 2500))
        if not msgs or msgs[0].get("role") != "system":
            return msgs
        system = msgs[0]["content"]
        for marker in ("[GLOBAL MEMORY]", "[WORKSPACE NOTES]"):
            m = re.search(r"(\n\n" + re.escape(marker) + r"\n)(.*?)(?=\n\n\[[A-Z ]+\]|\Z)", system, re.S)
            if m:
                text, left = me.select_for_prompt(m.group(2), user_text, budget)
                if left:
                    system = system[:m.start(2)] + text + system[m.end(2):]
        msgs[0] = dict(msgs[0], content=system)
        return msgs

    # ------------------------------------------------------------ typed commands
    def handle_text(self, text):
        """Return True when the message was a memory command (and has been dealt with)."""
        low = text.strip().lower()
        if low in ("/memory", "/memories"):
            self.open()
            return True
        if low.startswith("/forget "):
            self.forget(text.strip()[8:].strip())
            return True
        if self.cfg.get("remember_phrase", True):
            cmd = me.remember_command(text)
            if cmd:
                body, scope = cmd
                scope = scope or self.cfg.get("default_scope", "global")
                if not me.is_safe(body):
                    self.app.show("That looks like a password or key, so I did not store it.")
                elif self.remember(body, scope):
                    self.app.show(f"Saved to {self.label(scope)}: {body}\n(Memory... lets you edit, move or delete it.)")
                else:
                    self.app.show("I already remember something very similar.")
                return True
        return False

    def forget(self, words):
        best, best_scope, best_score = None, None, 0
        query = me.words(words)
        for scope in ("global", "workspace"):
            for it in self.file(scope).items():
                score = len(query & me.words(it["text"]))
                if score > best_score:
                    best, best_scope, best_score = it, scope, score
        if not best:
            self.app.show(f"No memory matches '{words}'. Open Memory... to see them all.")
        elif messagebox.askyesno("Forget this?", f"Delete from the {self.label(best_scope)}:\n\n{best['text']}", parent=self.app):
            self.file(best_scope).delete(best)
            self.app.show(f"Forgot: {best['text']}")
            if self.panel is not None:
                self.panel.refresh(force=True)

    def open(self):
        if self.panel is None:
            self.panel = Panel(self)
        self.panel.open()


class Panel:
    def __init__(self, mem):
        self.m, self.app = mem, mem.app
        self.win = None
        self.trees, self.rows, self.signature = {}, {}, None

    def alive(self):
        try:
            return self.win is not None and self.win.winfo_exists()
        except tk.TclError:
            return False

    def open(self):
        if self.alive():
            self.win.deiconify()
            self.win.lift()
            self.refresh(force=True)
            return
        win = self.win = tk.Toplevel(self.app)
        win.title("Cerry - Memory")
        win.geometry("900x620")
        ttk.Label(win, padding=8, wraplength=860, text=(
            "Global memory is true in every conversation; workspace memory only in the current workspace. "
            "A starred line is always sent to the model; long memories are reduced to the starred ones plus those that "
            "match your question. Double-click a line to edit it.")).pack(fill="x")
        self.nb = ttk.Notebook(win)
        self.nb.pack(fill="both", expand=True, padx=8)
        self.frames = {}
        for scope in ("global", "workspace"):
            self.frames[scope] = self.build_scope(scope)
        self.build_suggestions()
        opt = ttk.Frame(win, padding=8)
        opt.pack(fill="x")
        ttk.Label(opt, text="Auto memory:").pack(side="left")
        self.mode_var = tk.StringVar(value=MODE_NAMES.get(self.m.cfg.get("mode", "ask"), "Ask me"))
        box = ttk.Combobox(opt, textvariable=self.mode_var, values=list(MODE_LABELS), state="readonly", width=18)
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self.set_mode(self.mode_var.get()))
        self.all_var = tk.BooleanVar(value=not self.m.cfg.get("prefilter", True))
        ttk.Checkbutton(opt, text="check every message (not only ones that sound personal)", variable=self.all_var,
                        command=self.set_prefilter).pack(side="left", padx=12)
        ttk.Button(opt, text="Suggest from the last reply", command=self.suggest_now).pack(side="right")
        self.refresh(force=True)
        self.tick()

    # ---- building the tabs
    def build_scope(self, scope):
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="Global" if scope == "global" else "Workspace")
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        flt = tk.StringVar()
        ttk.Entry(top, textvariable=flt, width=40).pack(side="left", padx=6)
        flt.trace_add("write", lambda *a: self.refresh(force=True))
        tree = ttk.Treeview(frame, columns=("pin", "text"), show="headings", selectmode="browse", height=14)
        tree.heading("pin", text="\u2605")
        tree.heading("text", text="Memory")
        tree.column("pin", width=36, anchor="center", stretch=False)
        tree.column("text", width=780)
        tree.pack(fill="both", expand=True, pady=6)
        tree.bind("<Double-1>", lambda e, s=scope: self.edit(s))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        other = "workspace" if scope == "global" else "global"
        for text, fn in (("Add...", lambda s=scope: self.add(s)), ("Edit...", lambda s=scope: self.edit(s)),
                         ("Delete", lambda s=scope: self.delete(s)), ("Pin / unpin", lambda s=scope: self.pin(s)),
                         (f"Move to {other}", lambda s=scope: self.move(s)),
                         ("Edit as text...", lambda s=scope: self.as_text(s))):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        self.trees[scope] = (tree, flt)
        return frame

    def build_suggestions(self):
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="Suggestions")
        self.sug_frame = frame
        tree = ttk.Treeview(frame, columns=("scope", "text"), show="headings", selectmode="browse", height=14)
        tree.heading("scope", text="Where")
        tree.heading("text", text="Cerry suggests to remember")
        tree.column("scope", width=130, stretch=False)
        tree.column("text", width=700)
        tree.pack(fill="both", expand=True, pady=6)
        tree.bind("<Double-1>", lambda e: self.sug_edit())
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        for text, fn in (("Keep as suggested", self.sug_keep), ("Keep as global", lambda: self.sug_keep("global")),
                         ("Keep in workspace", lambda: self.sug_keep("workspace")), ("Edit...", self.sug_edit),
                         ("Dismiss", self.sug_dismiss), ("Dismiss all", self.sug_dismiss_all)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        self.sug_tree = tree

    # ---- refreshing
    def files_signature(self):
        sig = [self.app.ws.name, len(self.m.pending)]
        for scope in ("global", "workspace"):
            try:
                sig.append(self.m.file(scope).path.stat().st_mtime_ns)
            except OSError:
                sig.append(0)
        return tuple(sig)

    def refresh(self, force=False):
        if not self.alive():
            return
        sig = self.files_signature()
        if not force and sig == self.signature:
            return
        self.signature = sig
        self.nb.tab(self.frames["workspace"], text=f"Workspace: {self.app.ws.name}")
        for scope in ("global", "workspace"):
            tree, flt = self.trees[scope]
            keep = tree.selection()
            tree.delete(*tree.get_children())
            self.rows[scope] = {}
            q = flt.get().strip().lower()
            for n, it in enumerate(self.m.file(scope).items()):
                if q and q not in it["text"].lower():
                    continue
                iid = f"{scope}{n}"
                self.rows[scope][iid] = it
                tree.insert("", "end", iid=iid, values=("\u2605" if it["pinned"] else "", it["text"]))
            if keep and tree.exists(keep[0]):
                tree.selection_set(keep[0])
        self.sug_tree.delete(*self.sug_tree.get_children())
        for p in self.m.pending:
            where = "Global" if p["scope"] == "global" else f"Workspace: {p.get('workspace', '')}"
            self.sug_tree.insert("", "end", iid=p["id"], values=(where, p["text"]))
        self.nb.tab(self.sug_frame, text=f"Suggestions ({len(self.m.pending)})")

    def tick(self):
        if self.alive():
            self.refresh()
            self.app.after(1500, self.tick)

    # ---- actions on the two lists
    def selected(self, scope):
        tree, _ = self.trees[scope]
        sel = tree.selection()
        return self.rows[scope].get(sel[0]) if sel else None

    def ask(self, title, initial=""):
        text = simpledialog.askstring(title, "One short sentence:", initialvalue=initial, parent=self.win)
        return me.clean_text(text) if text else ""

    def guard(self, fn, *args):
        try:
            fn(*args)
        except (KeyError, ValueError) as e:
            messagebox.showinfo("Memory", str(e), parent=self.win)
        self.refresh(force=True)

    def add(self, scope):
        text = self.ask("Add memory")
        if text:
            if not me.is_safe(text):
                messagebox.showinfo("Memory", "That looks like a password or key - not stored.", parent=self.win)
            elif not self.m.remember(text, scope):
                messagebox.showinfo("Memory", "A very similar memory already exists.", parent=self.win)
            self.refresh(force=True)

    def edit(self, scope):
        it = self.selected(scope)
        if it:
            text = self.ask("Edit memory", it["text"])
            if text and me.is_safe(text):
                self.guard(self.m.file(scope).update, it, text)

    def delete(self, scope):
        it = self.selected(scope)
        if it and messagebox.askyesno("Delete memory", it["text"], parent=self.win):
            self.guard(self.m.file(scope).delete, it)

    def pin(self, scope):
        it = self.selected(scope)
        if it:
            self.guard(self.m.file(scope).set_pinned, it, not it["pinned"])

    def move(self, scope):
        it = self.selected(scope)
        if not it:
            return
        target = self.m.other(scope)

        def go():
            self.m.file(scope).delete(it)
            self.m.file(target).add(it["text"], pinned=it["pinned"], skip_similar=False)
        self.guard(go)

    def as_text(self, scope):
        edit = getattr(self.app, "edit_file", None)
        if edit:
            edit("Global memory" if scope == "global" else f"Workspace notes ({self.app.ws.name})", self.m.file(scope).path)

    # ---- suggestions
    def sug_selected(self):
        sel = self.sug_tree.selection()
        return next((p for p in self.m.pending if sel and p["id"] == sel[0]), None)

    def sug_keep(self, scope=None):
        p = self.sug_selected()
        if p:
            try:
                destination = scope or p["scope"]
                if destination == "workspace" and not p.get("workspace"):
                    raise ValueError("This suggestion has no saved workspace.")
                self.m.remember(p["text"], destination, p.get("workspace"))
            except (ValueError, OSError) as exc:
                messagebox.showinfo("Memory", str(exc), parent=self.win)
                return
            self.m.pending.remove(p)
            self.m.save_pending()
            self.refresh(force=True)

    def sug_edit(self):
        p = self.sug_selected()
        if p:
            text = self.ask("Edit suggestion", p["text"])
            if text:
                p["text"] = text
                self.m.save_pending()
                self.refresh(force=True)

    def sug_dismiss(self):
        p = self.sug_selected()
        if p:
            self.m.pending.remove(p)
            self.m.save_pending()
            self.refresh(force=True)

    def sug_dismiss_all(self):
        if self.m.pending and messagebox.askyesno("Dismiss all", "Remove all suggestions?", parent=self.win):
            self.m.pending.clear()
            self.m.save_pending()
            self.refresh(force=True)

    # ---- settings
    def set_mode(self, label):
        self.m.cfg["mode"] = MODE_LABELS.get(label, "ask")
        self.m.save_cfg()
        sync = getattr(self.app, "_mem_mode_var", None)
        if sync is not None:
            sync.set(label)

    def set_prefilter(self):
        self.m.cfg["prefilter"] = not self.all_var.get()
        self.m.save_cfg()

    def suggest_now(self):
        reply = next((m for m in reversed(self.app.chat["messages"]) if m.get("role") == "assistant"), None)
        if not reply:
            messagebox.showinfo("Memory", "There is no reply to look at yet.", parent=self.win)
            return
        self.m.say("Looking for things worth remembering ...")
        messages = self.app.chat["messages"]
        index = next(i for i, message in enumerate(messages) if message is reply)
        user = next((m["content"] for m in reversed(messages[:index]) if m.get("role") == "user"), "")
        self.m.after_reply(reply, TurnContext.capture(self.app, user), force=True)


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_memory_plugin_installed"):
        return
    App._memory_plugin_installed = True
    original_init, original_send = App.__init__, App.send
    original_finish = getattr(App, "finish_generation", None)
    original_build = getattr(App, "build_messages", None)

    def init(app, *a, **k):
        original_init(app, *a, **k)
        app._mem = Memory(app, ns)
        app._mem_mode_var = tk.StringVar(value=MODE_NAMES.get(app._mem.cfg.get("mode", "ask"), "Ask me"))
        strip = app.sidebar.tool_row("Memory")
        ttk.Button(strip, text="Memory...", command=app._mem.open).pack(side="left", padx=2)
        ttk.Label(strip, text="Auto memory:").pack(side="left", padx=(10, 3))
        box = ttk.Combobox(strip, textvariable=app._mem_mode_var, values=list(MODE_LABELS), state="readonly", width=18)
        box.pack(side="left")

        def changed(_e=None):
            app._mem.cfg["mode"] = MODE_LABELS.get(app._mem_mode_var.get(), "ask")
            app._mem.save_cfg()
            app.status.set(f"Auto memory: {app._mem_mode_var.get()}")
        box.bind("<<ComboboxSelected>>", changed)

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        if text and not app.busy and app._mem.handle_text(text):
            app.inp.delete("1.0", "end")
            return
        return original_send(app)

    App.__init__, App.send = init, send
    if original_finish is not None:
        def finish(app, cid, target, version):
            original_finish(app, cid, target, version)
            try:
                if app.turn.chat_id == cid:
                    app._mem.after_reply(version, app.turn)
            except Exception as e:
                app.status.set(f"Memory check failed: {e}"[:150])
        App.finish_generation = finish
    if original_build is not None:
        def build_messages(app, user_text, base):
            msgs, ev = original_build(app, user_text, base)
            try:
                msgs = app._mem.shrink_prompt(msgs, user_text)
            except Exception:
                pass
            return msgs, ev
        App.build_messages = build_messages
