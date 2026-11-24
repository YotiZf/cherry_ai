"""Cerry audio: transcribe audio/video files in the background, with a window that shows what is happening.

Needs: pip install faster-whisper        (speaker labels also: pip install pyannote.audio, plus HF_TOKEN in key.env)
Use:   "Attach files" accepts audio and video. The files go into the Audio window (button "Audio tasks..." or
       type /audio), which shows for every file: the stage (decoding, loading the model, transcribing, speakers),
       a progress bar, how much audio is done, speed, the time left, and the transcript as it appears.
       Files are processed one after the other; you can stop one file or all of them at any time.
       When a file is done, its transcript is attached to your next chat message and saved in the workspace.
Settings: ai_workspace_data/audio.json (model, language, speakers, auto_open)
The speech and speaker code is in audio_engine.py.
"""
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk, filedialog, scrolledtext

import audio_engine as eng

# kept so that older code importing these names from cerry_audio keeps working
AUDIO_EXT, MODEL_CHOICES, fmt_time = eng.AUDIO_EXT, eng.MODEL_CHOICES, eng.fmt_time
FINISHED = ("Done", "Failed", "Stopped")


@dataclass
class Job:
    id: int
    path: Path
    model: str
    lang: str
    label: bool
    nspk: str
    ws: object
    status: str = "Queued"            # Queued | Working | Done | Failed | Stopped
    stage: str = "Waiting"
    duration: float = 0.0
    done_s: float = 0.0
    queued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    trans_at: float = 0.0             # when the transcription itself began (for speed and time left)
    finished_at: float = 0.0
    lines: list = field(default_factory=list)
    speakers: int = 0
    language: str = ""
    error: str = ""
    warning: str = ""
    out: object = None
    text: str = ""
    stop: threading.Event = field(default_factory=threading.Event)
    keep_transcript: bool = False

    @property
    def name(self):
        return self.path.name

    def elapsed(self, now=None):
        if not self.started_at:
            return 0.0
        return (self.finished_at or now or time.time()) - self.started_at

    def numbers(self, now=None):
        """-> (fraction, speed in audio seconds per second, seconds left or None)."""
        now = now or time.time()
        if self.status == "Done":
            return 1.0, 0.0, 0.0
        if not self.trans_at or self.stage != "Transcribing":
            return (self.done_s / self.duration if self.duration else 0.0), 0.0, None
        return eng.estimate(self.done_s, self.duration, now - self.trans_at)


class AudioQueue:
    """Runs the jobs one after the other in a background thread. GUI-free."""

    def __init__(self, events, token_fn=None):
        self.jobs, self.events = [], events
        self.token_fn = token_fn or (lambda: os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN"))
        self._next, self._lock, self._running = 1, threading.RLock(), False

    def add(self, paths, model, lang, label, nspk, ws):
        added = []
        with self._lock:
            for p in paths:
                job = Job(self._next, Path(p), model, lang, label, nspk, ws)
                self._next += 1
                self.jobs.append(job)
                added.append(job)
            if added and not self._running:
                self._running = True
                threading.Thread(target=self._loop, daemon=True).start()
        return added

    def busy(self):
        return any(j.status in ("Queued", "Working") for j in self.jobs)

    def stop(self, job):
        if job.status == "Queued":
            job.status, job.stage, job.finished_at = "Stopped", "Stopped before it started", time.time()
        elif job.status == "Working":
            job.stop.set()
            job.stage = "Stopping after the current step ..."

    def stop_all(self):
        for j in list(self.jobs):
            self.stop(j)

    def remove_finished(self):
        with self._lock:
            self.jobs[:] = [j for j in self.jobs if j.status not in FINISHED]

    def _loop(self):
        while True:
            with self._lock:
                job = next((j for j in self.jobs if j.status == "Queued"), None)
                if job is None:
                    self._running = False
                    return
            self._run(job)

    def _run(self, job):
        job.status, job.stage, job.started_at = "Working", "Starting", time.time()

        def on_status(text):
            if text.startswith("Retrying transcription"):
                job.lines = []
                job.done_s, job.trans_at = 0, 0
            job.stage = "Transcribing" if text.startswith("Transcribing") else text

        def on_progress(done, total):
            job.done_s, job.duration = done, total
            if not job.trans_at:
                job.trans_at = time.time()
            job.stage = "Transcribing"

        def on_segment(seg):
            job.lines.append(f"[{eng.fmt_time(seg['start'])}] {seg['text']}")

        def on_step(name, completed, total):
            job.stage = f"Speakers: {name}" + (f" {completed}/{total}" if completed is not None and total else "")

        draft = None

        def checkpoint(result):
            nonlocal draft
            draft = dict(result, lines=eng.make_lines(result)[0], warning="", speakers=0)
            job.out, job.text = eng.save_transcript(draft, job.path, job.ws.docs, job.ws.dir / "audio")

        try:
            try:
                res = eng.analyze(job.path, job.model, None if job.lang == "auto" else job.lang, job.label,
                              None if job.nspk == "auto" else job.nspk, self.token_fn(), on_progress, on_status,
                              on_segment, on_step, job.stop.is_set, on_transcribed=checkpoint)
            except (eng.Stopped, TimeoutError) as exc:
                if isinstance(exc, eng.Stopped) and not job.keep_transcript:
                    raise
                if draft:
                    res = dict(draft, warning="Speaker identification ended. The transcript is complete.")
                elif job.lines:
                    res = dict(lines=list(job.lines), language=job.lang, duration=job.done_s, model=job.model,
                               speakers=0, probability=0, partial=True, warning="Partial transcript through the last recognized phrase.")
                else:
                    raise
            job.language, job.speakers, job.warning = res["language"], res["speakers"], res["warning"]
            job.duration = res["duration"]
            if not res["lines"]:
                job.status, job.stage, job.error = "Done", "No speech found", "No speech detected."
            else:
                job.lines = list(res["lines"])
                job.stage = "Saving"
                if job.out:
                    from workspace import write_atomic
                    job.text = eng.build_text(res, job.path.name)
                    write_atomic(job.out, job.text)
                else:
                    job.out, job.text = eng.save_transcript(res, job.path, job.ws.docs, job.ws.dir / "audio")
                job.status, job.stage, job.done_s = "Done", "Finished", job.duration
                self.events.put(("done", job, res))
        except eng.Stopped:
            job.status = "Stopped"
            job.stage = "Ended before any speech was recognized" if job.keep_transcript else "Stopped"
        except ImportError as e:
            job.status, job.stage, job.error = "Failed", "Package missing", f"A package is missing ({e}). Run: pip install faster-whisper"
        except Exception as e:
            logging.getLogger(__name__).warning("Audio job %s failed during %s: %s", job.id, job.stage, e)
            job.status, job.stage, job.error = "Failed", "Failed", f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            job.finished_at = time.time()


class AudioPanel:
    """The window: job list, progress, live transcript, activity."""

    COLS = (("file", 250), ("status", 80), ("stage", 190), ("progress", 70), ("length", 80), ("elapsed", 80),
            ("left", 100), ("speed", 70))

    def __init__(self, app, queue_, options, on_attach):
        self.app, self.q, self.options, self.on_attach = app, queue_, options, on_attach
        self.win = None
        self.shown = (None, 0)
        self.last_stage = {}

    def alive(self):
        try:
            return self.win is not None and self.win.winfo_exists()
        except tk.TclError:
            return False

    def open(self):
        if self.alive():
            self.win.deiconify()
            self.win.lift()
            self.refresh()
            return
        win = self.win = tk.Toplevel(self.app)
        win.title("Cerry - Audio transcription")
        win.geometry("1060x720")
        ttk.Label(win, padding=8, wraplength=1020, text=(
            "Audio and video files are transcribed one after the other with the model chosen in the Audio bar. "
            "End & keep transcript saves the phrases recognized so far. You can open the full text "
            "as soon as transcription finishes, even while speakers are being identified.")).pack(fill="x")
        cols = [c for c, _ in self.COLS]
        self.table = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse", height=7)
        for c, w in self.COLS:
            self.table.heading(c, text=c.title())
            self.table.column(c, width=w, anchor="w")
        self.table.pack(fill="x", padx=8)
        self.table.tag_configure("bad", foreground="#b00020")
        self.table.tag_configure("done", foreground="#2e7d32")
        self.table.bind("<<TreeviewSelect>>", lambda e: self.refresh())
        row = ttk.Frame(win, padding=6)
        row.pack(fill="x")
        for text, fn in (("Add audio files...", self.add_dialog), ("End & keep transcript", self.finish_selected),
                         ("Stop all", self.stop_all), ("Remove finished", self.remove_finished),
                         ("Open transcript", self.open_transcript), ("Open folder", self.open_folder),
                         ("Attach to chat", self.attach_selected)):
            ttk.Button(row, text=text, command=fn).pack(side="left", padx=3)
        box = ttk.Frame(win, padding=(8, 2))
        box.pack(fill="x")
        self.bar = ttk.Progressbar(box, maximum=100, mode="determinate")
        self.bar.pack(fill="x")
        self.detail = tk.StringVar(value="Add a file to begin.")
        ttk.Label(box, textvariable=self.detail, wraplength=1020).pack(anchor="w", pady=(4, 0))
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        t1, t2 = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(t1, text="Transcript (live)")
        nb.add(t2, text="Activity")
        self.live = scrolledtext.ScrolledText(t1, wrap="word", state="disabled", height=14)
        self.live.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(t2, wrap="word", state="disabled", height=14)
        self.log.pack(fill="both", expand=True)
        self.shown = (None, 0)
        self.refresh()

    # ---- helpers
    def selected(self):
        if not self.alive():
            return None
        sel = self.table.selection()
        return next((j for j in self.q.jobs if str(j.id) == sel[0]), None) if sel else None

    @staticmethod
    def _append(widget, text):
        widget.config(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.config(state="disabled")

    def note(self, text):
        if self.alive():
            self._append(self.log, f"{time.strftime('%H:%M:%S')}  {text}\n")

    def add_dialog(self):
        paths = filedialog.askopenfilenames(parent=self.win, title="Choose audio or video files",
                                            filetypes=[("Audio and video", " ".join("*" + e for e in sorted(eng.AUDIO_EXT))),
                                                       ("All files", "*.*")])
        if paths:
            self.options["add"](list(paths))

    def finish_selected(self):
        j = self.selected() or next((job for job in self.q.jobs if job.status == "Working"), None)
        if j:
            j.keep_transcript = True
            self.q.stop(j)
            self.note(f"Ending {j.name}. Recognized speech will be kept.")

    def stop_all(self):
        self.q.stop_all()
        self.note("Stop requested for all files.")

    def remove_finished(self):
        self.q.remove_finished()
        self.refresh()

    def open_transcript(self):
        j = self.selected() or (self.q.jobs[-1] if self.q.jobs else None)
        if j and j.out and hasattr(os, "startfile"):
            os.startfile(str(j.out))

    def open_folder(self):
        j = self.selected()
        if j and j.out and hasattr(os, "startfile"):
            os.startfile(str(Path(j.out).parent))

    def attach_selected(self):
        j = self.selected()
        if j and j.status == "Done" and j.text:
            self.on_attach(j)

    # ---- the live view
    def refresh(self):
        if not self.alive():
            return
        now = time.time()
        remaining = set(self.table.get_children())
        for j in self.q.jobs:
            frac, speed, left = j.numbers(now)
            tag = "bad" if j.status == "Failed" else ("done" if j.status == "Done" else "")
            values = (
                j.name, j.status, j.stage[:40], f"{frac:.0%}" if j.duration else "-",
                eng.fmt_time(j.duration) if j.duration else "-", eng.fmt_short(j.elapsed(now)) if j.started_at else "-",
                ("done" if j.status == "Done" else eng.fmt_short(left) if left is not None else "-"),
                f"{speed:.1f}x" if speed else "-")
            iid = str(j.id)
            if iid in remaining:
                remaining.remove(iid)
                self.table.item(iid, tags=(tag,), values=values)
            else:
                self.table.insert("", "end", iid=iid, tags=(tag,), values=values)
            last = self.last_stage.get(j.id)
            if last != (j.status, j.stage) and (j.stage != "Transcribing" or not last or last[0] != j.status):
                self.note(f"{j.name}: {j.status} - {j.stage}" + (f" ({j.error})" if j.error and j.status == "Failed" else ""))
            self.last_stage[j.id] = (j.status, j.stage)
        if remaining:
            self.table.delete(*remaining)
        job = self.selected() or (self.q.jobs[-1] if self.q.jobs else None)
        if job is None:
            return
        frac, speed, left = job.numbers(now)
        self.bar["value"] = frac * 100
        parts = [job.name, f"{job.status}: {job.stage}"]
        if job.duration:
            parts.append(f"{eng.fmt_time(job.done_s)} of {eng.fmt_time(job.duration)} ({frac:.0%})")
        if speed:
            parts.append(f"{speed:.1f}x real time")
        if left is not None and job.status == "Working":
            parts.append(f"about {eng.fmt_short(left)} left")
        parts.append(f"model {job.model}, language {job.lang}" + (", speaker labels" if job.label else ""))
        if job.error:
            parts.append(job.error)
        if job.warning:
            parts.append(job.warning)
        self.detail.set("  |  ".join(parts))
        lines = job.lines
        previous, n = self.shown
        if previous is not lines:
            self.live.config(state="normal")
            self.live.delete("1.0", "end")
            self.live.config(state="disabled")
            n = 0
        if len(lines) < n:
            n = 0
        added = lines[n:]
        if added:
            self._append(self.live, "\n".join(added) + "\n")
        self.shown = (lines, n + len(added))


def install(ns):
    """Pass the same namespace you give to cerri_extensions.install()."""
    App = ns["App"]
    if getattr(App, "_cerry_audio", False):
        return
    App._cerry_audio = True
    cfg_path = ns["DATA_DIR"] / "audio.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    original_init, original_attach, original_send = App.__init__, App.attach, App.send
    original_close = getattr(App, "on_close", None)

    def save(app):
        cfg.update(model=app.aud_model.get(), lang=app.aud_lang.get(), speakers=app.aud_spk.get(), nspk=app.aud_nspk.get())
        try:
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def deliver(app, job):
        """Main thread: a finished transcript goes to the chat of its workspace."""
        res_note = (f'Transcribed {job.name} ({eng.fmt_time(job.duration)}, language {job.language}'
                    + (f', {job.speakers} speakers).' if job.speakers else ').'))
        if job.warning:
            res_note += " " + job.warning
        text = job.text[:eng.ATTACH_LIMIT]
        if len(job.text) > eng.ATTACH_LIMIT:
            text += "\n[Transcript continues. The full text is saved in the workspace documents and is searchable.]"
            res_note += " Long transcript: the first part is attached; the rest is searchable."
        if app.ws is job.ws:
            app.pending_files.append((f"{job.path.stem} (transcript)", text))
            app.show(res_note + f" Saved as {job.out.name}. Ask your question now.")
            app.reindex_async()
        else:
            app.show(res_note + " (Saved in the other workspace.)")

    def poll(app):
        try:
            while True:
                kind, job, res = app._aud_events.get_nowait()
                if kind == "done":
                    deliver(app, job)
        except queue.Empty:
            pass
        except Exception as e:
            app.status.set(f"Audio error: {e}"[:150])
        busy = app._aud_queue.busy()
        if busy:
            work = next((j for j in app._aud_queue.jobs if j.status == "Working"), None)
            if work:
                frac, _, left = work.numbers()
                app.status.set(f"Audio: {work.name} - {work.stage}" + (f" {frac:.0%}" if work.duration else "")
                               + (f", about {eng.fmt_short(left)} left" if left else ""))
        elif app._aud_was_busy:
            last = app._aud_queue.jobs[-1] if app._aud_queue.jobs else None
            app.status.set(f"Audio failed: {last.error}" if last and last.status == "Failed" else "Audio queue finished.")
        app._aud_was_busy = busy
        app._aud_panel.refresh()
        app.after(500 if (busy or app._aud_panel.alive()) else 1500, lambda: poll(app))

    def add_files(app, paths):
        if not paths:
            return
        jobs = app._aud_queue.add(paths, app.aud_model.get(), app.aud_lang.get(), app.aud_spk.get(), app.aud_nspk.get(), app.ws)
        app.status.set(f"{len(jobs)} audio file(s) added to the Audio window.")
        if cfg.get("auto_open", True):
            app._aud_panel.open()
        app._aud_panel.note(f"Added: {', '.join(j.name for j in jobs)}")

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        app._aud_events = queue.Queue()
        app._aud_queue = AudioQueue(app._aud_events)
        app._aud_was_busy = False
        app.aud_model = tk.StringVar(value=cfg.get("model", "small"))
        app.aud_lang = tk.StringVar(value=cfg.get("lang", "auto"))
        app.aud_spk = tk.BooleanVar(value=cfg.get("speakers", False))
        app.aud_nspk = tk.StringVar(value=cfg.get("nspk", "auto"))
        app._aud_panel = AudioPanel(app, app._aud_queue, {"add": lambda paths: add_files(app, paths)},
                                    lambda job: deliver(app, job))
        bar = app.sidebar.tool_row("Audio and voice")
        ttk.Label(bar, text="Audio model:").pack(side="left")
        m = ttk.Combobox(bar, textvariable=app.aud_model, state="readonly", width=14, values=eng.MODEL_CHOICES)
        m.pack(side="left", padx=4)
        ttk.Label(bar, text="Language:").pack(side="left", padx=(10, 3))
        lg = ttk.Combobox(bar, textvariable=app.aud_lang, state="readonly", width=6, values=["auto", "en", "de"])
        lg.pack(side="left")
        ttk.Checkbutton(bar, text="Speaker labels", variable=app.aud_spk, command=lambda: save(app)).pack(side="left", padx=(12, 3))
        ttk.Label(bar, text="Speakers:").pack(side="left", padx=(6, 3))
        nb = ttk.Combobox(bar, textvariable=app.aud_nspk, state="readonly", width=5, values=["auto", "2", "3", "4", "5", "6"])
        nb.pack(side="left")
        for box in (m, lg, nb):
            box.bind("<<ComboboxSelected>>", lambda e: save(app))
        ttk.Button(bar, text="Audio tasks...", command=app._aud_panel.open).pack(side="left", padx=(12, 3))
        ttk.Label(bar, text="Attach files also accepts audio/video; knowing the speaker count helps accuracy.",
                  style="Muted.TLabel").pack(side="left", padx=8)
        app.after(500, lambda: poll(app))

    def attach(app):
        picked = filedialog.askopenfilenames(title="Upload documents or audio")
        if not picked:
            return
        audio = [p for p in picked if Path(p).suffix.lower() in eng.AUDIO_EXT]
        others = [p for p in picked if p not in audio]
        add_files(app, audio)
        if others:
            real = filedialog.askopenfilenames
            filedialog.askopenfilenames = lambda *a, **k: tuple(others)
            try:
                original_attach(app)
            finally:
                filedialog.askopenfilenames = real

    def send(app):
        if app.inp.get("1.0", "end").strip().lower() == "/audio":
            app.inp.delete("1.0", "end")
            app._aud_panel.open()
            return
        return original_send(app)

    def on_close(app):
        try:
            app._aud_queue.stop_all()
        except Exception:
            pass
        if original_close:
            return original_close(app)
        app.destroy()

    App.__init__, App.attach, App.send, App.on_close = init, attach, send, on_close
