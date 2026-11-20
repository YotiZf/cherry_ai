"""Cerry run: run Python, R and SQL code from the chat - after you have looked at it and pressed Run.

Every code box of a Python, R or SQL answer gets a "Run" button next to "Copy".  It opens the Run window with the code in an
editor (change it if you like).  NOTHING runs until you press Run in that window.
  Python / R   run in a separate process in the workspace's outputs folder, with a time limit and a Stop button.
               Files the code creates (tables, plots, ...) are listed.  Your API keys are not visible to the code.
               NOT a sandbox: the code has your user permissions - read it first.
  SQL          SQLite only: a temporary database (the workspace's CSV files can be loaded as tables), or a SQLite file you
               choose - opened read-only unless you allow changes.  Results are shown as tables and can be saved as CSV.
"Send result to chat" puts the output into your message box, so the model can check it and fix the code.
Commands: /run opens the window.     Settings: ai_workspace_data/run.json
Needs: run_engine.py.  R needs R installed (Rscript).  Switch on: "cerry_run" in plugins.json, AFTER "cerry_files".
"""
import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

import run_engine as re_

ORDER = 70
LABELS = {"Python": "py", "R": "r", "SQL (SQLite)": "sql"}
NAMES = {v: k for k, v in LABELS.items()}
BLOCK_LANGS = {"py", "python", "python3", "r", "rscript", "sql", "sqlite", "sqlite3"}


class Runner:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.path = Path(ns["DATA_DIR"]) / "run.json"
        self.cfg = dict(re_.DEFAULTS)
        try:
            self.cfg.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.window = None
        self.runs = []
        self.running = False
        self.stop_flag = threading.Event()
        self.q = queue.Queue()

    # ------------------------------------------------------------ helpers
    def save_cfg(self):
        try:
            self.path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def out_dir(self):
        d = Path(self.app.ws.dir) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def csv_files(self):
        folders = [self.app.ws.docs] + ([self.app.ws.cfg["folder"]] if self.app.ws.cfg.get("folder") else [])
        out = []
        for f in folders:
            f = Path(f)
            if f.is_dir():
                out += [p for p in sorted(f.rglob("*.csv")) if not any(x.startswith(".") for x in p.relative_to(f).parts)]
        return out

    def alive(self):
        try:
            return self.window is not None and self.window.winfo_exists()
        except tk.TclError:
            return False

    # ------------------------------------------------------------ window
    def open(self, lang=None, code=None):
        if not self.alive():
            self.build()
        self.window.deiconify()
        self.window.lift()
        if lang:
            self.lang_var.set(NAMES.get(re_.language_of(lang) or "py", "Python"))
            self.options_changed()
        if code is not None:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", code)
            self.set_info("Read the code, then press Run. Nothing has been run yet.")
        self.editor.focus_set()

    def build(self):
        win = self.window = tk.Toplevel(self.app)
        win.title("Cerry - Run code")
        win.geometry("1000x760")
        self.info = tk.StringVar(value="")
        ttk.Label(win, padding=(8, 6), wraplength=960, text=(
            "Python and R run as separate programs with your user permissions (not a sandbox, but your API keys are hidden "
            "from them). SQL runs in SQLite. Nothing runs until you press Run.")).pack(fill="x")
        top = ttk.Frame(win, padding=(8, 0))
        top.pack(fill="x")
        self.lang_var = tk.StringVar(value="Python")
        box = ttk.Combobox(top, textvariable=self.lang_var, values=list(LABELS), state="readonly", width=14)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.options_changed())
        ttk.Label(top, text="Time limit (s):").pack(side="left", padx=(14, 3))
        self.timeout_var = tk.StringVar(value=str(self.cfg["timeout"]))
        sp = ttk.Spinbox(top, from_=5, to=3600, width=6, textvariable=self.timeout_var, command=self.options_changed)
        sp.pack(side="left")
        sp.bind("<FocusOut>", lambda e: self.options_changed())
        self.run_btn = ttk.Button(top, text="\u25b6 Run", command=self.start)
        self.run_btn.pack(side="left", padx=(16, 4))
        self.stop_btn = ttk.Button(top, text="\u25a0 Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Label(top, textvariable=self.info, style="Muted.TLabel").pack(side="left", padx=12)
        self.sql_frame = ttk.LabelFrame(win, text="SQL database", padding=6)
        self.sql_frame.pack(fill="x", padx=8, pady=(6, 0))
        self.db_var = tk.StringVar(value=self.cfg.get("sql_db", ""))
        ttk.Label(self.sql_frame, text="SQLite file (empty = temporary database):").pack(side="left")
        ttk.Entry(self.sql_frame, textvariable=self.db_var, width=48).pack(side="left", padx=6)
        ttk.Button(self.sql_frame, text="Browse...", command=self.browse_db).pack(side="left")
        ttk.Button(self.sql_frame, text="Temporary", command=lambda: (self.db_var.set(""), self.options_changed())).pack(side="left", padx=4)
        self.write_var = tk.BooleanVar(value=bool(self.cfg.get("sql_write")))
        ttk.Checkbutton(self.sql_frame, text="allow changes to the file", variable=self.write_var, command=self.options_changed).pack(side="left", padx=8)
        self.csv_var = tk.BooleanVar(value=bool(self.cfg.get("sql_csv", True)))
        ttk.Checkbutton(self.sql_frame, text="load the workspace's CSV files as tables", variable=self.csv_var, command=self.options_changed).pack(side="left")
        ttk.Label(win, text="Code").pack(anchor="w", padx=8, pady=(6, 0))
        self.editor = scrolledtext.ScrolledText(win, wrap="none", height=14, font=("Consolas", 10), undo=True)
        self.editor.pack(fill="both", expand=True, padx=8)
        mid = ttk.Frame(win, padding=(8, 4))
        mid.pack(fill="x")
        for text, fn in (("Send result to chat", self.send_back), ("Save result as CSV...", self.save_csv),
                         ("Open outputs folder", self.open_outputs), ("Open script", self.open_script), ("Clear history", self.clear)):
            ttk.Button(mid, text=text, command=fn).pack(side="left", padx=3)
        self.hist = ttk.Treeview(win, columns=("when", "lang", "status", "time"), show="headings", selectmode="browse", height=4)
        for c, w in (("when", 120), ("lang", 120), ("status", 380), ("time", 80)):
            self.hist.heading(c, text=c.title())
            self.hist.column(c, width=w)
        self.hist.pack(fill="x", padx=8)
        self.hist.bind("<<TreeviewSelect>>", lambda e: self.show_selected())
        ttk.Label(win, text="Result").pack(anchor="w", padx=8)
        self.result = scrolledtext.ScrolledText(win, wrap="none", height=12, font=("Consolas", 10), state="disabled")
        self.result.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.refresh_history()
        self.options_changed(save=False)

    def set_info(self, text):
        if self.alive():
            self.info.set(text)

    def set_result(self, text):
        self.result.config(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.config(state="disabled")

    def options_changed(self, save=True):
        if not self.alive():
            return
        try:
            self.cfg["timeout"] = max(5, min(3600, int(float(self.timeout_var.get()))))
        except ValueError:
            pass
        self.cfg["sql_db"] = self.db_var.get().strip()
        self.cfg["sql_write"] = bool(self.write_var.get())
        self.cfg["sql_csv"] = bool(self.csv_var.get())
        is_sql = LABELS.get(self.lang_var.get()) == "sql"
        for child in self.sql_frame.winfo_children():
            try:
                child.configure(state="normal" if is_sql else "disabled")
            except tk.TclError:
                pass
        if save:
            self.save_cfg()

    def browse_db(self):
        p = filedialog.askopenfilename(parent=self.window, title="SQLite database",
                                       filetypes=[("SQLite", "*.db *.sqlite *.sqlite3 *.db3"), ("All files", "*.*")])
        if p:
            self.db_var.set(p)
            self.options_changed()

    # ------------------------------------------------------------ running
    def start(self):
        if self.running:
            return
        code = self.editor.get("1.0", "end").strip("\n")
        if not code.strip():
            self.set_info("There is no code to run.")
            return
        self.options_changed()
        lang = LABELS.get(self.lang_var.get(), "py")
        cfg = dict(self.cfg)
        csvs = self.csv_files() if lang == "sql" and cfg.get("sql_csv") and not cfg.get("sql_db") else []
        if lang == "sql" and cfg.get("sql_db") and cfg.get("sql_write") and not messagebox.askyesno(
                "Change the database?", f"This code may CHANGE the database file:\n{cfg['sql_db']}\n\nA backup is not made. Continue?",
                parent=self.window):
            return
        self.running = True
        self.stop_flag.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.set_info("Running ...")
        started = self._t0 = time.time()
        out = self.out_dir()

        def job():
            try:
                if lang == "sql":
                    res = re_.run_sql(code, cfg, csvs, self.stop_flag.is_set)
                else:
                    res = re_.run_process(lang, code, out, cfg, self.stop_flag.is_set)
            except Exception as e:
                res = {"lang": lang, "error": f"{type(e).__name__}: {e}"}
            self.q.put((lang, code, res, started))
        threading.Thread(target=job, daemon=True).start()
        self.app.after(300, self.poll)

    def poll(self):
        try:
            lang, code, res, started = self.q.get_nowait()
        except queue.Empty:
            if self.running:
                self.set_info(f"Running ... {time.time() - self.t0():.0f}s")
                self.app.after(300, self.poll)
            return
        self.running = False
        text = re_.format_result(res, int(self.cfg.get("max_output", 20000)))
        if res.get("error") and "returncode" not in res and not res.get("sets"):
            status = "Error"
        elif res.get("timed_out"):
            status = "Time limit reached"
        elif res.get("stopped"):
            status = "Stopped"
        elif lang != "sql" and res.get("returncode") not in (0, None):
            status = f"Failed (exit code {res['returncode']})"
        elif res.get("error"):
            status = "SQL error"
        else:
            status = "OK" + (f" - {len(res['new_files'])} new file(s)" if res.get("new_files") else "")
        self.runs.append({"when": time.strftime("%H:%M:%S"), "lang": lang, "code": code, "res": res, "text": text,
                          "status": status, "seconds": time.time() - started})
        if self.alive():
            self.run_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.refresh_history()
            self.set_result(text)
            self.set_info(status)
        try:
            self.app.status.set(f"{re_.LABEL[lang]}: {status}")
        except Exception:
            pass

    def t0(self):
        return getattr(self, "_t0", time.time())

    def stop(self):
        if self.running:
            self.stop_flag.set()
            self.set_info("Stopping ...")

    # ------------------------------------------------------------ results
    def refresh_history(self):
        if not self.alive():
            return
        self.hist.delete(*self.hist.get_children())
        for i, r in enumerate(self.runs):
            self.hist.insert("", "end", iid=str(i), values=(r["when"], re_.LABEL[r["lang"]], r["status"], f"{r['seconds']:.1f}s"))
        if self.runs:
            self.hist.selection_set(str(len(self.runs) - 1))

    def selected(self):
        sel = self.hist.selection() if self.alive() else ()
        if sel:
            return self.runs[int(sel[0])]
        return self.runs[-1] if self.runs else None

    def show_selected(self):
        r = self.selected()
        if r:
            self.set_result(r["text"])

    def send_back(self):
        r = self.selected()
        if not r:
            return
        name = re_.LABEL[r["lang"]]
        self.app.inp.delete("1.0", "end")
        self.app.inp.insert("1.0", f"Result of the {name} code I ran:\n```\n{r['text'][:6000]}\n```\nPlease check it and continue.")
        self.app.inp.focus_set()
        self.app.status.set("Result placed in the message box. Press Enter to send it to the model.")

    def save_csv(self):
        r = self.selected()
        sets = (r or {}).get("res", {}).get("sets") or []
        if not sets:
            messagebox.showinfo("Save as CSV", "The selected run has no SQL result table.", parent=self.window)
            return
        target = filedialog.asksaveasfilename(parent=self.window, defaultextension=".csv", initialfile="result.csv",
                                              filetypes=[("CSV", "*.csv")])
        if target:
            Path(target).write_text(re_.rows_to_csv(sets[-1]["columns"], sets[-1]["rows"]), encoding="utf-8-sig", newline="")
            self.app.status.set(f"Saved {target}")

    def open_outputs(self):
        opener = self.ns.get("open_folder")
        if opener:
            opener(self.out_dir())
        elif hasattr(os, "startfile"):
            os.startfile(str(self.out_dir()))

    def open_script(self):
        r = self.selected()
        script = (r or {}).get("res", {}).get("script")
        if script and hasattr(os, "startfile"):
            os.startfile(script)

    def clear(self):
        self.runs.clear()
        self.refresh_history()
        self.set_result("")


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_run_plugin_installed"):
        return
    App._run_plugin_installed = True
    actions = ns.setdefault("_block_actions", [])
    actions.append({"label": "\u25b6 Run", "langs": set(BLOCK_LANGS),
                    "callback": lambda app, lang, code: app._run.open(lang, code)})
    original_init, original_send, original_close = App.__init__, App.send, getattr(App, "on_close", None)

    def init(app, *a, **k):
        original_init(app, *a, **k)
        app._run = Runner(app, ns)
        strip = ttk.Frame(app, padding=(6, 0))
        strip.pack(fill="x", before=app.inp.master)
        ttk.Button(strip, text="Run code...", command=app._run.open).pack(side="left", padx=2)
        ttk.Label(strip, text="Python / R / SQL - press Run on a code box in the chat, or type /run", style="Muted.TLabel").pack(side="left", padx=8)

    def send(app):
        if app.inp.get("1.0", "end").strip().lower() == "/run":
            app.inp.delete("1.0", "end")
            app._run.open()
            return
        return original_send(app)

    def on_close(app):
        try:
            app._run.stop_flag.set()
        except Exception:
            pass
        if original_close:
            return original_close(app)
        app.destroy()

    App.__init__, App.send, App.on_close = init, send, on_close
