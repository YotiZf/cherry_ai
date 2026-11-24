"""Cerry restart: start Cerry again with one click, so updated files are picked up without closing and re-opening it.

  * Restart is at the top right. Re-scan is in the workspace tools.
  * Typing /restart or /rescan in the chat box runs the matching action.
How it works: Cerry closes in the normal way (agents, speech and the audio queue are stopped, your chat is saved) and,
as the very last step, starts a new copy of itself with the same Python and the same script.  If something is still
running (an answer, an agent task, a job search, an audio transcription, a voice conversation) Cerry asks first.
Switch on:  "cerry_restart" in ai_workspace_data/plugins.json
"""
import atexit
import os
import subprocess
import sys
from tkinter import ttk, messagebox

DETACHED = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (Windows)


def relaunch_command():
    """The command that starts this very program again (same Python, same script, same arguments)."""
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None
    return [sys.executable] + ([script] if script else []) + list(sys.argv[1:])


def spawn(cmd, cwd):
    """Start the new Cerry, independent of this process."""
    kwargs = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def things_running(app):
    busy = []
    if getattr(app, "busy", False):
        busy.append("an answer is being written")
    if getattr(app, "convo_active", False):
        busy.append("a voice conversation")
    hub = getattr(app, "hermes", None)
    if hub is not None and getattr(hub, "running", False):
        busy.append("an agent task")
    jobs = getattr(app, "jobhunter", None)
    if jobs is not None and getattr(jobs, "running", False):
        busy.append("a job search")
    audio = getattr(app, "_aud_queue", None)
    if audio is not None and audio.busy():
        busy.append("an audio transcription")
    return busy


def restart(app):
    busy = things_running(app)
    if busy and not messagebox.askyesno(
            "Restart Cerry", "Still running: " + ", ".join(busy) + ".\n\nThese will be stopped. Restart anyway?",
            parent=app):
        return
    atexit.register(spawn, relaunch_command(), os.getcwd())      # runs after Cerry has closed completely
    try:
        app.status.set("Restarting Cerry ...")
    except Exception:
        pass
    app.after(150, app.on_close)


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_restart_plugin_installed"):
        return
    App._restart_plugin_installed = True
    original_init, original_send = App.__init__, App.send

    def init(app, *a, **k):
        original_init(app, *a, **k)
        ttk.Button(app.header_actions, text="Restart", command=lambda: restart(app)).pack(side="right")

    def send(app):
        command = app.inp.get("1.0", "end").strip().lower()
        if command == "/restart":
            app.inp.delete("1.0", "end")
            restart(app)
            return
        if command == "/rescan":
            app.inp.delete("1.0", "end")
            app.reindex_async()
            return
        return original_send(app)

    App.__init__, App.send = init, send
