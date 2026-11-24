"""Cerry files: Cerry hands you real files, edits your PDF / Word files, and shows code in boxes with a Copy button.

1. CODE BOXES   Code, commands and complete texts (a letter, an e-mail) that the model writes inside ``` fences are shown as a
                box with the language name and a "Copy" button at its top - one click puts exactly that text on the clipboard.
                Other plugins can add more buttons to those boxes (cerry_run adds "Run" to Python, R and SQL boxes).
2. NEW FILES    Ask for a file ("make me a Python script that ...", "put this in a Word document", "export it as a PDF",
                "an Excel sheet with ...").  Cerry creates it in  workspaces/<name>/outputs/  and shows it in the Files window:
                Python, text, Markdown, CSV, JSON, HTML, LaTeX, YAML, SQL ... as plain files; Word and PDF from Markdown; Excel from CSV.
3. EDITING      "Analyse report.pdf and change x to y", "remove page 3 from Contract.pdf", "put DRAFT on every page",
                "replace Dr. Smith by Prof. Jones in Letter.docx": Cerry gives the model the text of the file, the model lists
                precise operations (a fixed list - no free-form code is run), and Cerry applies them to a COPY.
                The original is never changed.  The result opens from the Files window.
   PDF   replace/highlight text (needs  pip install pymupdf ), delete / keep / rotate / reorder pages, add text, watermark, merge
   Word  replace text, insert after a paragraph, delete a paragraph, append a paragraph   (python-docx)
   Name the file in your message (the name or the name without its ending) or attach it, so Cerry knows which one you mean.
Commands: /files opens the Files window.     Switch on: "cerry_files" in plugins.json (needs file_engine.py).
"""
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

import file_engine as fe

ORDER = 60
MAX_ACTIONS = 3
CODE_RULE = ("When you write code, a command, or a complete text the user will copy (letter, e-mail, CV), put it in a fenced block "
             "with a language tag (```python, ```text, ...). Keep explanations outside the block.")


def open_path(ns, path):
    opener = ns.get("open_folder")
    if opener and Path(path).is_dir():
        opener(path)
    elif hasattr(os, "startfile"):
        os.startfile(str(path))


class Files:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.window = None
        self.recent = []                 # [{'path', 'what', 'note'}]

    def out_dir(self):
        d = Path(self.app.ws.dir) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def folders(self):
        return [self.app.ws.docs, self.out_dir()] + ([self.app.ws.cfg["folder"]] if self.app.ws.cfg.get("folder") else [])

    # ------------------------------------------------------------ doing what the answer asked for
    def run_actions(self, acts):
        """-> (notes_for_the_message, created_paths).  Never raises: every problem becomes a note."""
        notes, created = [], []
        for act in acts[:MAX_ACTIONS]:
            try:
                if act["kind"] == "make":
                    path = fe.make_file(self.out_dir(), act["name"], act["content"])
                    notes.append(f"\u2705 Created **{path.name}**")
                    self.recent.append({"path": path, "what": "created", "note": ""})
                else:
                    log = []
                    path, changes, lines = fe.run_edit(act, self.folders(), self.out_dir(), log.append)
                    notes.append(f"\u2705 Edited a copy of **{act['source']}** \u2192 **{path.name}** ({changes} change(s))")
                    self.recent.append({"path": path, "what": "edited copy", "note": "; ".join(s.strip() for s in lines)})
                created.append(path)
            except Exception as e:
                label = act.get("name") or act.get("source", "file")
                notes.append(f"\u26a0 Could not make **{label}**: {str(e)[:220]}")
        if len(acts) > MAX_ACTIONS:
            notes.append(f"\u26a0 Only the first {MAX_ACTIONS} files were made; ask again for the rest.")
        return notes, created

    def prompt_additions(self, user_text):
        """Extra instructions for the model: the code-box rule always, the file rules when a file is being asked for."""
        extra = [fe.FILE_RULES if fe.REQUEST_RE.search(user_text or "") else CODE_RULE]
        try:
            for p in fe.mentioned_files(user_text, self.folders()):
                import doc_manager
                pages, status, _ = doc_manager.extract_pages(p)
                if status == "ok" and pages:
                    extra.append(f"[FULL TEXT OF {p.name}] (copy 'find' strings exactly from here)\n{fe.page_text(pages)}")
        except Exception:
            pass
        return "\n\n".join(extra)

    # ------------------------------------------------------------ Files window
    def alive(self):
        try:
            return self.window is not None and self.window.winfo_exists()
        except tk.TclError:
            return False

    def open(self):
        if self.alive():
            self.window.deiconify()
            self.window.lift()
            self.fill()
            return
        win = self.window = tk.Toplevel(self.app)
        win.title("Cerry - Files")
        win.geometry("860x420")
        ttk.Label(win, padding=8, wraplength=820, text=(
            "Files Cerry made in this session. Edited files are always copies - your original is untouched. "
            f"Folder: {self.out_dir()}")).pack(fill="x")
        self.tree = ttk.Treeview(win, columns=("name", "what", "folder", "note"), show="headings", selectmode="browse", height=10)
        for c, w in (("name", 220), ("what", 90), ("folder", 260), ("note", 260)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=8)
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")
        for text, fn in (("Open file", self.open_file), ("Open folder", self.open_dir), ("Save a copy as...", self.save_as),
                         ("Copy path", self.copy_path), ("Clear list", self.clear)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        self.fill()

    def fill(self):
        if not self.alive():
            return
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.recent):
            p = Path(r["path"])
            self.tree.insert("", "end", iid=str(i), values=(p.name, r["what"], str(p.parent), r["note"][:90]))
        if self.recent:
            self.tree.selection_set(str(len(self.recent) - 1))

    def selected(self):
        sel = self.tree.selection() if self.alive() else ()
        return self.recent[int(sel[0])] if sel else None

    def open_file(self):
        r = self.selected()
        if r:
            open_path(self.ns, r["path"])

    def open_dir(self):
        r = self.selected()
        open_path(self.ns, Path(r["path"]).parent if r else self.out_dir())

    def save_as(self):
        r = self.selected()
        if not r:
            return
        p = Path(r["path"])
        target = filedialog.asksaveasfilename(parent=self.window, initialfile=p.name, defaultextension=p.suffix)
        if target:
            Path(target).write_bytes(p.read_bytes())
            self.app.status.set(f"Saved a copy: {target}")

    def copy_path(self):
        r = self.selected()
        if r:
            self.app.clipboard_clear()
            self.app.clipboard_append(str(r["path"]))
            self.app.status.set("Path copied.")

    def clear(self):
        self.recent.clear()
        self.fill()


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_files_plugin_installed"):
        return
    App._files_plugin_installed = True
    original_init, original_send = App.__init__, App.send
    original_finish = getattr(App, "finish_generation", None)
    original_build = getattr(App, "build_messages", None)
    original_render = App.render_chat

    # ------------------------------------------------------------ prompt
    def build_messages(app, user_text, base):
        msgs, ev = original_build(app, user_text, base)
        try:
            msgs[0] = dict(msgs[0], content=msgs[0]["content"] + "\n\n" + app._files.prompt_additions(user_text))
        except Exception:
            pass
        return msgs, ev

    # ------------------------------------------------------------ the finished answer
    def finish_generation(app, cid, target, version):
        try:
            if version and version.get("content"):
                acts = fe.parse_actions(version["content"])
                if acts:
                    app.status.set("Creating files ...")
                    notes, created = app._files.run_actions(acts)
                    version = dict(version)
                    version["content"] = fe.display_text(version["content"], acts).rstrip() + "\n\n" + "\n".join(notes)
                    version["files"] = [str(p) for p in created]
                    if created:
                        app.after(100, app._files.open)
        except Exception as e:
            app.status.set(f"Files: {str(e)[:120]}")
        return original_finish(app, cid, target, version)

    # ------------------------------------------------------------ code boxes with a Copy button
    def tags_ready(app):
        w = app.chat_w
        if getattr(app, "_files_tags", False):
            return
        app._files_tags = True
        try:
            bg = str(w.cget("bg"))
        except tk.TclError:
            bg = ""
        try:
            fg = str(w.cget("fg"))
        except tk.TclError:
            fg = ""
        dark = False
        m = fe.re.fullmatch(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", bg)
        if m:
            r, g, b = (int(x, 16) for x in m.groups())
            dark = (0.299 * r + 0.587 * g + 0.114 * b) < 128
        box = fe.shade(bg, dark)
        w.tag_configure("cc_code", font=("Consolas", 10), background=box, lmargin1=14, lmargin2=14, rmargin=8)
        w.tag_configure("cc_head", font=("Segoe UI", 8, "bold"), background=box, lmargin1=14, foreground=fg or None)
        w.tag_configure("cc_copy", font=("Segoe UI", 8, "bold"), background=box, underline=True)
        w.tag_raise("sel")

    def copy_block(app, n):
        w = app.chat_w
        r = w.tag_ranges(f"cc_blk{n}")
        if not r:
            return
        text = w.get(r[0], r[1]).rstrip("\n")
        app.clipboard_clear()
        app.clipboard_append(text)
        app.status.set(f"Copied {len(text.splitlines())} line(s) to the clipboard.")
        w.tag_configure(f"cc_cp{n}", foreground="#2e7d32")
        app.after(1200, lambda: w.tag_configure(f"cc_cp{n}", foreground=""))

    def run_action(app, n, act, lang):
        r = app.chat_w.tag_ranges(f"cc_blk{n}")
        if r:
            act["callback"](app, lang, app.chat_w.get(r[0], r[1]).rstrip("\n"))

    def decorate(app):
        w = app.chat_w
        try:
            if w.tag_ranges("sel"):
                return                                           # do not move text the user is selecting
            text = w.get("1.0", "end-1c")
            if "```" not in text:
                return
            tags_ready(app)
            w.config(state="normal")
            try:
                # an answer that BEGINS with a code block has its fence behind "Cherry [model]: " - move it to its own line
                for i, line in sorted(((i, l) for i, l in enumerate(text.split("\n"), 1) if "]: ```" in l), reverse=True):
                    m = fe.re.match(r"^(.{0,140}?\]: )```", line)
                    if m:
                        w.insert(f"{i}.{len(m.group(1))}", "\n")
                text = w.get("1.0", "end-1c")
                blocks = fe.find_blocks(text)
                if not blocks:
                    return
                for n in range(len(blocks) - 1, -1, -1):         # from the end, so earlier line numbers stay valid
                    a, b, lang = blocks[n]
                    w.delete(f"{b}.0", f"{b + 1}.0")             # closing fence line
                    w.tag_add("cc_code", f"{a + 1}.0", f"{b}.0")
                    w.tag_add(f"cc_blk{n}", f"{a + 1}.0", f"{b}.0")
                    w.delete(f"{a}.0", f"{a}.end")               # opening fence line -> header with the Copy link
                    w.insert(f"{a}.0", f" {lang or 'code'}   ", ("cc_head",))
                    w.insert(f"{a}.end", "\u29c9 Copy", ("cc_copy", f"cc_cp{n}", "link"))
                    for k, act in enumerate(x for x in ns.get("_block_actions", []) if lang in x["langs"]):
                        tag = f"cc_act{n}_{k}"
                        w.insert(f"{a}.end", "  ", ("cc_head",))
                        w.insert(f"{a}.end", act["label"], ("cc_copy", tag, "link"))
                        if tag not in app._files_bound:             # other plugins (cerry_run) add buttons to code boxes
                            app._files_bound.add(tag)
                            w.tag_bind(tag, "<Button-1>", lambda e, n=n, act=act, lang=lang: run_action(app, n, act, lang))
                            w.tag_bind(tag, "<Enter>", lambda e: w.config(cursor="hand2"))
                            w.tag_bind(tag, "<Leave>", lambda e: w.config(cursor=""))
                    w.insert(f"{a}.end", "   ", ("cc_head",))
                    w.tag_add("cc_code", f"{a}.0", f"{a}.end")
                    if n not in app._files_bound:
                        app._files_bound.add(n)
                        w.tag_bind(f"cc_cp{n}", "<Button-1>", lambda e, n=n: copy_block(app, n))
                        w.tag_bind(f"cc_cp{n}", "<Enter>", lambda e: w.config(cursor="hand2"))
                        w.tag_bind(f"cc_cp{n}", "<Leave>", lambda e: w.config(cursor=""))
            finally:
                w.config(state="disabled")
        except tk.TclError:
            pass

    def render_chat(app, *a, **k):
        result = original_render(app, *a, **k)
        if getattr(app, "live", None) is None:
            decorate(app)
        return result

    # ------------------------------------------------------------ window, command
    def init(app, *a, **k):
        app._files_bound = set()
        original_init(app, *a, **k)
        app._files = Files(app, ns)
        strip = app.sidebar.tool_row("Files and images")
        ttk.Button(strip, text="Files...", command=app._files.open).pack(side="left", padx=2)

    def send(app):
        if app.inp.get("1.0", "end").strip().lower() == "/files":
            app.inp.delete("1.0", "end")
            app._files.open()
            return
        return original_send(app)

    App.__init__, App.send, App.render_chat = init, send, render_chat
    if original_build is not None:
        App.build_messages = build_messages
    if original_finish is not None:
        App.finish_generation = finish_generation
