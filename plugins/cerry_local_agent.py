"""Local Cherry coding agent: inspect one chosen folder and propose reviewed edits without Hermes or shell execution."""
from copy import deepcopy
import threading
import os
import shutil
import subprocess
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import local_agent_engine as engine
import streaming

ORDER = 96
SOURCES = ("Cherry models", "Codex (ChatGPT sign-in)", "Claude Code (Claude sign-in)")


class LocalAgent:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.window = None
        self.busy = False
        self.root_var = tk.StringVar(value=str(app.ws.dir))
        self.task_var = tk.StringVar()
        self.provider_var = tk.StringVar(value="Cherry models")
        self.status = tk.StringVar(value="Choose a project folder and describe the changes you want.")

    def open(self, task=""):
        if self.window is None or not self.window.winfo_exists():
            self.build()
        self.window.deiconify()
        self.window.lift()
        if task:
            self.task_var.set(task)

    def build(self):
        win = self.window = tk.Toplevel(self.app)
        win.title("Cherry - Local coding agent")
        win.geometry("900x680")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Project directory").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.root_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Choose...", command=self.choose).grid(row=0, column=2)
        ttk.Label(frm, text="Task for Cherry").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        ttk.Entry(frm, textvariable=self.task_var).grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(10, 0))
        ttk.Label(frm, text="Model source").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.source_box = ttk.Combobox(frm, textvariable=self.provider_var, state="readonly",
                                       postcommand=self.refresh_sources)
        self.source_box.grid(row=2, column=1, sticky="w", padx=6, pady=(10, 0))
        self.window.bind("<FocusIn>", lambda _event: self.refresh_sources())
        self.refresh_sources()
        ttk.Label(frm, text="Cherry reads up to 80 text files (500 KB total), excluding secrets and common generated folders. "
                          "CLI agents run in read-only/plan mode and cannot apply edits here. Changes require your approval.",
                  wraplength=850).grid(row=3, column=0, columnspan=3, sticky="w", pady=10)
        self.run_btn = ttk.Button(frm, text="Review task", command=self.start)
        self.run_btn.grid(row=4, column=2, sticky="e")
        ttk.Label(frm, textvariable=self.status, wraplength=850).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        frm.columnconfigure(1, weight=1)

    def refresh_sources(self):
        sources = SOURCES[:1] if self.app.policy.local_only else SOURCES
        self.source_box.configure(values=sources)
        if self.provider_var.get() not in sources:
            self.provider_var.set(sources[0])

    def choose(self):
        path = filedialog.askdirectory(parent=self.window, title="Choose the folder Cherry may edit")
        if path:
            self.root_var.set(path)

    def start(self):
        if self.busy:
            return
        task = self.task_var.get().strip()
        if not task:
            return self.status.set("Describe the change first.")
        source = self.provider_var.get()
        if self.app.policy.local_only and source != SOURCES[0]:
            self.refresh_sources()
            return self.status.set("Local-only mode requires Cherry models. Review the task again to continue.")
        model = self.app.selected_model() if source == SOURCES[0] else None
        policy = deepcopy(self.app.policy)
        try:
            root, files, skipped = engine.gather(self.root_var.get())
        except Exception as exc:
            return self.status.set("Could not inspect folder: " + str(exc)[:180])
        if not files:
            return self.status.set("No readable project text files found.")
        self._current_files = files
        summary = f"Reviewed {len(files)} file(s) in {root}."
        if skipped:
            summary += f" Skipped {len(skipped)} file(s) (binary, protected, or size limit)."
        self.status.set(summary + " Asking the selected model...")
        self.busy = True
        self.run_btn.configure(state="disabled")

        def worker():
            error, proposal = None, None
            try:
                if source == "Cherry models":
                    order, _ = self.ns["routing"].build_order(
                        self.ns["MODELS"], model, policy)
                    system, prompt = engine.make_prompt(task, files)
                    result = streaming.run_chat(order, [{"role": "system", "content": system},
                        {"role": "user", "content": prompt}], self.ns["get_client"],
                        streaming.CancelToken(), policy)
                    answer = result["answer"]
                else:
                    answer = self.run_cli(source, root, files, task)
                proposal = engine.parse_proposal(answer, root, files)
            except Exception as exc:
                error = exc
            self.app.after(0, lambda: self.finish(root, files, proposal, error))
        threading.Thread(target=worker, daemon=True).start()

    def run_cli(self, source, root, files, task):
        prompt_system, prompt_user = engine.make_prompt(task, files)
        prompt = prompt_system + "\n\n" + prompt_user
        with tempfile.TemporaryDirectory(prefix="cherry-agent-review-") as temp:
            sandbox = Path(temp) / "project"
            sandbox.mkdir()
            for item in files:
                target = sandbox.joinpath(*Path(item["path"]).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item["text"], encoding="utf-8")
            prompt += "\n\nThe selected project is named " + root.name + ". The current directory is a temporary, read-only review copy."
            if source.startswith("Codex"):
                executable = shutil.which("codex")
                if not executable:
                    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "codex.exe"
                    executable = str(local) if local.exists() else None
                if not executable:
                    raise RuntimeError("Codex CLI was not found. Install it or add codex.exe to PATH.")
                result_file = Path(temp) / "answer.txt"
                cmd = [executable, "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                       "--cd", str(sandbox), "--output-last-message", str(result_file), prompt]
                result = subprocess.run(cmd, cwd=str(sandbox), capture_output=True, text=True, timeout=300,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout or "Codex failed")[-1200:])
                if not result_file.exists():
                    raise RuntimeError("Codex finished without a final response.")
                return result_file.read_text(encoding="utf-8")
            executable = shutil.which("claude")
            if not executable:
                raise RuntimeError("Claude Code CLI was not found. Install Claude Code, sign in, and retry.")
            result = subprocess.run([executable, "-p", "--permission-mode", "plan", "--output-format", "text",
                                     "--max-turns", "2", prompt], cwd=str(sandbox), capture_output=True, text=True,
                                    timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "Claude Code failed")[-1200:])
            return result.stdout

    def finish(self, root, files, proposal, error):
        self.busy = False
        self.run_btn.configure(state="normal")
        if error:
            self.status.set("Cherry could not prepare changes: " + str(error)[:220])
            return
        if not proposal["changes"]:
            self.status.set("Cherry found no file changes to propose. " + proposal["summary"][:160])
            return
        diffs = []
        originals = {f["path"]: f for f in files}
        for change in proposal["changes"]:
            old = originals.get(change["path"])
            if old:
                change["original"] = old
            diffs.append(engine.diff_for(change) or f"[{change['action'].upper()} {change['path']}]")
        self.review(root, proposal, diffs)

    def review(self, root, proposal, diffs):
        win = tk.Toplevel(self.window)
        win.title("Review Cherry's local changes")
        win.geometry("900x680")
        ttk.Label(win, text=f"{proposal['summary']}\n{len(proposal['changes'])} proposed change(s). "
                            "Nothing has been changed yet.", wraplength=850, padding=10).pack(fill="x")
        text = scrolledtext.ScrolledText(win, wrap="none", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=10)
        text.insert("1.0", "\n\n".join(diffs))
        text.configure(state="disabled")
        row = ttk.Frame(win, padding=10)
        row.pack(fill="x")
        ttk.Button(row, text="Reject", command=win.destroy).pack(side="right", padx=4)

        def approve():
            if not messagebox.askyesno("Apply local changes?", "Cherry will update or remove the listed files in:\n\n"
                    f"{root}\n\nOriginal files will be backed up under ai_workspace_data/local_agent_backups.", parent=win):
                return
            try:
                backup = engine.apply_changes(root, proposal["changes"],
                    Path(self.ns["DATA_DIR"]) / "local_agent_backups")
            except Exception as exc:
                messagebox.showerror("Cherry", "Could not safely apply changes: " + str(exc), parent=win)
                return
            win.destroy()
            self.status.set(f"Applied {len(proposal['changes'])} change(s). Backup: {backup}")
            messagebox.showinfo("Cherry", "Approved changes were applied.\nBackup: " + str(backup), parent=self.window)
        ttk.Button(row, text="Approve and apply", command=approve).pack(side="right", padx=4)


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_local_agent_installed"):
        return
    App._local_agent_installed = True
    original_init, original_send = App.__init__, App.send

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        app.local_agent = LocalAgent(app, ns)
        ttk.Button(app.sidebar.tool_row("Agents"), text="Code agent...", command=app.local_agent.open).pack(anchor="w", padx=12, pady=(0, 4))

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        if text.lower().startswith("/work"):
            task = text[5:].strip()
            app.inp.delete("1.0", "end")
            app.local_agent.open(task)
            return
        return original_send(app)

    App.__init__, App.send = init, send
