"""Cerry writing style: CVs, cover letters and other texts in YOUR voice, learned from a training folder.

Set the "writing training folder" once (button "Writing style..." or /style): put your own cover letters, motivation
letters, CVs and other texts there (.txt .md .docx .pdf .tex).  Cerry studies them, writes a short style guide that you can
read and edit, and then
  * Job search (cerry_jobs): every tailored CV and cover letter gets that guide plus 1-3 of your own letters as
    examples (the ones most similar to the job, in the language of the job).  Facts still come only from your CV.
  * Chat: with "auto" the guide is added when you ask Cerry to write something (letter, e-mail, text); "always" adds it to
    every answer; "off" never.
The guide is rebuilt when the folder changes (not if you edited it - then press "Study folder now").
Commands: /style   /style off | auto | always
Settings: ai_workspace_data/style.json     Guide: ai_workspace_data/style/profile.json
Switch on: "cerry_style" in plugins.json (needs style_engine.py).  Best together with cerry_tuning (temperature for writing).
"""
import importlib
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

import style_engine as se

DEFAULTS = {"folder": "", "use_in_jobs": True, "use_in_chat": "auto", "examples": 2, "model": ""}
TABS = (("letter_en", "Letters (English)"), ("letter_de", "Briefe (German)"), ("cv", "CV wording"),
        ("phrases", "Your phrases"), ("avoid", "Never do"))


class Style:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.dir = Path(ns["DATA_DIR"]) / "style"
        self.dir.mkdir(exist_ok=True)
        self.cfg_path, self.prof_path = Path(ns["DATA_DIR"]) / "style.json", self.dir / "profile.json"
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self._samples = (None, [])
        self.window = None
        self.busy = False

    # ------------------------------------------------------------ storage
    def save_cfg(self):
        try:
            self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def profile(self):
        try:
            return json.loads(self.prof_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_profile(self, prof):
        self.prof_path.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")

    def samples(self):
        folder = self.cfg.get("folder", "")
        fp = se.fingerprint(folder) if folder else None
        if self._samples[0] != fp or fp is None:
            self._samples = (fp, se.scan_folder(folder) if folder else [])
        return self._samples[1]

    def status_text(self):
        folder = self.cfg.get("folder", "")
        if not folder:
            return "No writing folder chosen yet."
        if not Path(folder).is_dir():
            return "The folder does not exist."
        prof = self.profile()
        n = len(se.candidate_files(folder))
        if not prof:
            return f"{n} file(s) found. Not studied yet - press 'Study folder now'."
        c = prof.get("counts", {})
        fresh = prof.get("edited") or prof.get("fingerprint") == se.fingerprint(folder)
        return (f"Guide built {prof.get('built', '?')} from {len(prof.get('files', []))} file(s) "
                f"({c.get('letters', 0)} letters, {c.get('cvs', 0)} CVs)." + (" Edited by you." if prof.get("edited") else "")
                + ("" if fresh else "  The folder has changed - press 'Study folder now'."))

    # ------------------------------------------------------------ studying
    def ensure_profile(self, ask, log=None):
        """The guide for the folder; built (with `ask`) if missing or outdated and not edited by hand."""
        folder = self.cfg.get("folder", "")
        prof = self.profile()
        if prof and (prof.get("edited") or prof.get("fingerprint") == se.fingerprint(folder)):
            return prof
        if log:
            log("Studying your writing folder ...")
        prof = se.build_profile(self.samples(), ask, folder)
        self.save_profile(prof)
        return prof

    def window_ask(self):
        ns, app = self.ns, self.app
        label = self.cfg.get("model") or ""
        model = next((m for m in ns["MODELS"] if m["label"] == label), None) or app.selected_model()

        def ask(system, user):
            streaming = ns["streaming"]
            order, _ = ns["routing"].build_order(ns["MODELS"], model, app.policy)
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            return streaming.run_chat(order, msgs, ns["get_client"], streaming.CancelToken(), app.policy, None, None)["answer"]
        return ask

    def study_now(self):
        if self.busy:
            return
        if not Path(self.cfg.get("folder", "")).is_dir():
            messagebox.showinfo("Writing style", "Choose the writing folder first.", parent=self.window or self.app)
            return
        prof = self.profile()
        if prof and prof.get("edited") and not messagebox.askyesno(
                "Writing style", "You edited the guide by hand. Studying again replaces your edits. Continue?", parent=self.window):
            return
        ask, self.busy = self.window_ask(), True
        self.set_status("Studying the folder ... (one model call, about a minute)")
        self._samples = (None, [])
        result = {}

        def job():
            try:
                result["profile"] = se.build_profile(self.samples(), ask, self.cfg["folder"])
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        t = threading.Thread(target=job, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                self.app.after(400, poll)
                return
            self.busy = False
            if "profile" in result:
                self.save_profile(result["profile"])
                self.set_status("Done. " + self.status_text())
                self.fill()
            else:
                self.set_status("Failed: " + result.get("error", "unknown error"))
        self.app.after(400, poll)

    # ------------------------------------------------------------ what goes into the prompts
    def blocks_for(self, lang_name, query, ask, log=None):
        """-> (cv_block, letter_block) for a job in `lang_name`."""
        lang = "de" if lang_name.lower().startswith(("german", "deutsch")) else "en"
        prof = self.ensure_profile(ask, log)
        n = int(self.cfg.get("examples", 2))
        samples = self.samples()
        cv = se.prompt_block(prof, "cv", lang, se.pick_examples(samples, "cv", lang, query, min(1, n), 1500))
        letter = se.prompt_block(prof, "letter", lang, se.pick_examples(samples, "letter", lang, query, n, 1800))
        return cv, letter

    def chat_block(self, user_text):
        mode = self.cfg.get("use_in_chat", "auto")
        prof = self.profile()
        if mode == "off" or not prof or not self.cfg.get("folder"):
            return ""
        if mode == "auto" and not se.WRITING_REQUEST.search(user_text or ""):
            return ""
        lang = se.detect_language(user_text or "")
        ex = se.pick_examples(self.samples(), "letter", lang, user_text, 1, 1200)
        return se.prompt_block(prof, "letter", lang, ex)

    # ------------------------------------------------------------ the window
    def set_status(self, text):
        if self.window is not None and getattr(self, "info", None) is not None:
            try:
                self.info.set(text)
            except tk.TclError:
                pass

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
        win.title("Cerry - Writing style")
        win.geometry("900x700")
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Writing training folder").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=self.cfg.get("folder", ""))
        ttk.Entry(top, textvariable=self.folder_var).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="Browse...", command=self.browse).grid(row=0, column=2)
        ttk.Button(top, text="Open folder", command=self.open_folder).grid(row=0, column=3, padx=4)
        opt = ttk.Frame(win, padding=(8, 0))
        opt.pack(fill="x")
        self.jobs_var = tk.BooleanVar(value=bool(self.cfg["use_in_jobs"]))
        ttk.Checkbutton(opt, text="use for CVs and cover letters (Jobs)", variable=self.jobs_var, command=self.options_changed).pack(side="left")
        ttk.Label(opt, text="Chat:").pack(side="left", padx=(16, 3))
        self.chat_var = tk.StringVar(value=self.cfg["use_in_chat"])
        box = ttk.Combobox(opt, textvariable=self.chat_var, values=["off", "auto", "always"], state="readonly", width=8)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.options_changed())
        ttk.Label(opt, text="Examples per letter:").pack(side="left", padx=(16, 3))
        self.ex_var = tk.StringVar(value=str(self.cfg["examples"]))
        ex = ttk.Combobox(opt, textvariable=self.ex_var, values=["0", "1", "2", "3"], state="readonly", width=3)
        ex.pack(side="left")
        ex.bind("<<ComboboxSelected>>", lambda e: self.options_changed())
        self.info = tk.StringVar(value=self.status_text())
        ttk.Label(win, textvariable=self.info, wraplength=860, padding=8).pack(fill="x")
        bar = ttk.Frame(win, padding=(8, 0))
        bar.pack(fill="x")
        for text, fn in (("Study folder now", self.study_now), ("Save my edits", self.save_edits), ("Show files studied", self.show_files)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.boxes = {}
        for key, title in TABS:
            frame = ttk.Frame(nb)
            nb.add(frame, text=title)
            t = scrolledtext.ScrolledText(frame, wrap="word", height=14)
            t.pack(fill="both", expand=True)
            self.boxes[key] = t
        ttk.Label(win, padding=8, wraplength=860, text=(
            "The guides above are what Cerry follows. You can change them: edit the text and press 'Save my edits'. "
            "Your own letters are used as examples for the voice only - facts always come from your CV.")).pack(fill="x")
        self.fill()

    def fill(self):
        if not self.alive():
            return
        prof = self.profile() or {}
        for key, _ in TABS:
            box = self.boxes[key]
            box.delete("1.0", "end")
            box.insert("1.0", prof.get(key, ""))
        self.info.set(self.status_text())

    def browse(self):
        p = filedialog.askdirectory(parent=self.window, title="Folder with your own letters, CVs and texts")
        if p:
            self.folder_var.set(p)
            self.cfg["folder"] = p
            self.save_cfg()
            self.info.set(self.status_text())

    def open_folder(self):
        opener = self.ns.get("open_folder")
        if opener and self.cfg.get("folder"):
            opener(self.cfg["folder"])

    def options_changed(self):
        self.cfg["folder"] = self.folder_var.get().strip()
        self.cfg["use_in_jobs"] = bool(self.jobs_var.get())
        self.cfg["use_in_chat"] = self.chat_var.get()
        self.cfg["examples"] = int(self.ex_var.get() or 2)
        self.save_cfg()
        self.info.set(self.status_text())

    def save_edits(self):
        prof = self.profile() or {k: "" for k in se.KEYS}
        for key, _ in TABS:
            prof[key] = self.boxes[key].get("1.0", "end").strip()
        prof["edited"] = True
        prof.setdefault("files", [])
        self.save_profile(prof)
        self.info.set("Saved. " + self.status_text())

    def show_files(self):
        files = (self.profile() or {}).get("files", [])
        messagebox.showinfo("Files studied", "\n".join(files) or "None yet.", parent=self.window)


def jobs_module():
    for name in ("cerry_jobs_1", "cerry_jobs"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_style_plugin_installed"):
        return
    App._style_plugin_installed = True
    original_init, original_send = App.__init__, App.send
    original_build = getattr(App, "build_messages", None)

    def init(app, *a, **k):
        original_init(app, *a, **k)
        app._style = Style(app, ns)
        row = app.sidebar.tool_row("Response settings")
        ttk.Button(row, text="Writing style...", command=app._style.open).pack()

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        low = text.lower()
        if low == "/style" or low in ("/style off", "/style auto", "/style always"):
            app.inp.delete("1.0", "end")
            if low == "/style":
                app._style.open()
            else:
                app._style.cfg["use_in_chat"] = low.split()[1]
                app._style.save_cfg()
                app.show(f"Writing style in chat: {low.split()[1]}.")
            return
        return original_send(app)

    App.__init__, App.send = init, send
    if original_build is not None:
        def build_messages(app, user_text, base):
            msgs, ev = original_build(app, user_text, base)
            try:
                block = app._style.chat_block(user_text)
                if block:
                    msgs[0] = dict(msgs[0], content=msgs[0]["content"] + "\n\n" + block)
            except Exception:
                pass
            return msgs, ev
        App.build_messages = build_messages

    # ---- CV and cover letter of the job search: add the guide and examples to its instructions
    mod = jobs_module()
    if mod is not None and hasattr(mod, "Hunter"):
        original_tailor = mod.Hunter.tailor

        def tailor(self, rec, desc, a, profile, cv_text, folder, lang, letter_only=False):
            style = getattr(self.app, "_style", None)
            if style is None or not style.cfg.get("use_in_jobs") or not style.cfg.get("folder"):
                return original_tailor(self, rec, desc, a, profile, cv_text, folder, lang, letter_only=letter_only)
            try:
                cv_block, letter_block = style.blocks_for(lang, desc, lambda s, u: self.llm(s, u), self.log)
            except Exception as e:                       # a problem with the folder must never stop the job search
                self.log(f"  writing style skipped: {str(e)[:120]}")
                return original_tailor(self, rec, desc, a, profile, cv_text, folder, lang, letter_only=letter_only)
            self.log("  writing the documents in your own style ...")
            saved = (mod.CV_SYSTEM, mod.LETTER_SYSTEM)
            tuner = ns.get("_tuner")
            mod.CV_SYSTEM, mod.LETTER_SYSTEM = saved[0] + "\n\n" + cv_block, saved[1] + "\n\n" + letter_block
            if tuner is not None:
                tuner.context.kind = "writing"
            try:
                return original_tailor(self, rec, desc, a, profile, cv_text, folder, lang, letter_only=letter_only)
            finally:
                mod.CV_SYSTEM, mod.LETTER_SYSTEM = saved
                if tuner is not None:
                    tuner.context.kind = None
        mod.Hunter.tailor = tailor
