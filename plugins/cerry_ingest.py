"""Cerry ingest: Re-scan now also reads pictures, scanned PDF pages and audio files.

What happens when you press Re-scan (or the Re-scan button in the Documents window):
  1. Text documents are indexed as before.
  2. Cerry looks for files it cannot read as text and that it has not processed yet:
       pictures (png, jpg, ...)          OCR, and - when OCR finds little - a description by a model that can see pictures
       PDF pages without text            the page is drawn as a picture and handled the same way (scanned PDFs)
       audio and video files             transcript (faster-whisper, same models as the Audio window)
  3. They are processed one after the other in the background; the window "Processing..." (or /ingest) shows every
     file, its stage, progress and time, and lets you stop.
  4. The text found is stored once (ai_workspace_data/ingest.sqlite) and then searched like any other document:
     the answer cites the original file name (and the PDF page).  Unchanged files are never processed again.
Picture models: the first model in your list that can read pictures (or the one chosen in the window), then - if it
fails - the next picture-capable model of your fallback chain.  Online models only get pictures after you agreed
(a question appears once per run).  Local-only mode never sends anything online; OCR then does the work.
Opening a workspace only COUNTS what is waiting (status line); nothing is processed until you press Re-scan or Process.
Settings: ai_workspace_data/ingest.json     Switch on: "cerry_ingest" in plugins.json (needs ingest_engine.py)
Packages: pip install pillow pypdfium2 rapidocr-onnxruntime   (Tesseract + pytesseract is better for German)
          pip install faster-whisper   (audio)
"""
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, scrolledtext

import ingest_engine as ie

ORDER = 6          # right after cerry_docs (5): this plugin wraps the function cerry_docs already wrapped

VISION_SYSTEM = ("You help to make documents searchable. Describe pictures precisely and factually. "
                 "Reply with the description only. Do not use any <cerry_file> or <cerry_run> tags.")
VISION_PROMPT = ("Describe this picture so that someone can find it later by searching. 1) Copy all readable text "
                 "exactly (keep numbers, names, dates). 2) Describe tables, charts (axes, series, key values), diagrams, "
                 "handwriting, photos, logos and layout in plain sentences. 3) If it is a form or document, say what kind. "
                 "At most about 250 words. Use the language of the text in the picture.")


class Ingest:
    def __init__(self, app, ns):
        self.app, self.ns = app, ns
        self.path = Path(ns["DATA_DIR"]) / "ingest.json"
        self.cfg = dict(ie.DEFAULTS, auto_open=True)
        try:
            self.cfg.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.store = ns["_ingest_store"]
        self.items, self.counts = [], {}
        self.running = self.discovering = False
        self.stop_flag = threading.Event()
        self.token = None
        self.q = queue.Queue()
        self.log_lines = []
        self.panel = None
        self.reindex_plain = None

    # ------------------------------------------------------------ helpers
    def save_cfg(self):
        try:
            self.path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def say(self, text):
        self.q.put(("log", text))

    def status(self, text):
        try:
            self.app.status.set(text)
        except Exception:
            pass

    def sources(self):
        ws = self.app.ws
        out = [("documents", ws.docs)]
        if ws.cfg.get("folder"):
            out.append(("linked", ws.cfg["folder"]))
        return out

    def audio_ext(self):
        try:
            import audio_engine
            return set(audio_engine.AUDIO_EXT)
        except Exception:
            return {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mkv", ".webm", ".mov"}

    def vision_ids(self):
        try:
            data = json.loads((Path(self.ns["DATA_DIR"]) / "vision.json").read_text(encoding="utf-8"))
            return set(data.get("vision_ids", [])) | {m.get("id") for m in data.get("extra_models", []) if isinstance(m, dict)}
        except (OSError, ValueError):
            return set()

    def summary(self):
        c = self.counts
        parts = []
        if c.get("image"):
            parts.append(f"{c['image']} picture(s)")
        if c.get("pdf"):
            pages = sum(len(i.pages) for i in self.items if i.kind == "pdf")
            parts.append(f"{c['pdf']} scanned PDF(s) ({pages} page(s))")
        if c.get("audio"):
            parts.append(f"{c['audio']} audio file(s)")
        return ", ".join(parts)

    # ------------------------------------------------------------ discovery
    def discover(self, start=False, deep=True):
        """deep=False (automatic, when a workspace opens) does not read PDFs that were never checked before."""
        if self.running or self.discovering:
            return
        self.discovering = True
        self.stop_flag.clear()
        cfg, sources = dict(self.cfg), self.sources()
        ws_name = self.app.ws.name

        def job():
            try:
                items, counts = ie.find_work(sources, self.store, cfg, self.audio_ext(),
                                             lambda t: self.q.put(("progress", t)), self.stop_flag.is_set, deep)
                self.q.put(("found", ws_name, items, counts, start))
            except ie.Stopped:
                self.q.put(("found", ws_name, [], {}, False))
            except Exception as e:
                self.q.put(("log", f"Looking for new files failed: {type(e).__name__}: {str(e)[:150]}"))
                self.q.put(("found", ws_name, [], {}, False))
        threading.Thread(target=job, daemon=True).start()
        self.app.after(400, self.tick)

    def on_found(self, ws_name, items, counts, start):
        self.discovering = False
        if ws_name != self.app.ws.name:
            return
        finished = [i for i in self.items if i.status in ("Done", "Failed", "Stopped")]
        self.items, self.counts = finished + items, counts
        text = self.summary()
        if text:
            self.status(f"Waiting to be read: {text}. " + ("Starting ..." if start else "Press Re-scan (or open Processing...)."))
            self.note(f"Found: {text}.")
        elif start:
            self.status("Documents scanned. Nothing new to read.")
        if start and items:
            self.begin()
        if self.panel is not None:
            self.panel.refresh()

    # ------------------------------------------------------------ running
    def begin(self):
        todo = [i for i in self.items if i.status == "Queued"][: int(self.cfg.get("max_per_run", 30))]
        if not todo or self.running:
            return
        allow_vision = bool(self.cfg.get("allow_vision", True)) and self.cfg.get("image_mode") != "ocr"
        wants_vision = allow_vision and any(i.kind in ("image", "pdf") for i in todo)
        if wants_vision and self.cfg.get("ask_before_cloud", True):
            model = self.first_vision_model()
            local = bool(model) and self.ns["routing"].is_local(model)
            if model and not local and not self.app.policy.local_only:
                n = sum(1 if i.kind == "image" else len(i.pages) for i in todo if i.kind in ("image", "pdf"))
                allow_vision = messagebox.askyesno(
                    "Send pictures online?",
                    f"Up to {n} picture(s)/page(s) may be sent to an online model ({model['label']}, and the next "
                    f"picture-capable model if it fails) so that it can describe them.\n\nYes = allow.  No = local OCR only.",
                    parent=self.app)
        self.running = True
        self.stop_flag.clear()
        self.token = self.ns["streaming"].CancelToken()
        selected = self.first_vision_model()
        for i in todo:
            i.status, i.stage, i.frac, i.note = "Queued", "Waiting", 0.0, ""
        threading.Thread(target=self.worker, args=(todo, allow_vision, selected, dict(self.cfg)), daemon=True).start()
        if self.cfg.get("auto_open", True):
            self.open()
        self.app.after(400, self.tick)

    def first_vision_model(self):
        ids = self.vision_ids()
        models = [m for m in self.ns["MODELS"] if ie.is_vision(m, ids)]
        label = self.cfg.get("vision_model") or ""
        chosen = next((m for m in models if m["label"] == label), None)
        if chosen:
            return chosen
        try:
            current = self.app.selected_model()
            if ie.is_vision(current, ids):
                return current
        except Exception:
            pass
        return models[0] if models else None

    def make_vision(self, first, token, status_cb):
        ns = self.ns
        routing, streaming = ns["routing"], ns["streaming"]
        ids = self.vision_ids()

        def vision(img, hint):
            if first is None:
                raise ie.VisionUnavailable("no model in your list can read pictures - add one under Manage models")
            try:
                order, _ = routing.build_order(ns["MODELS"], first, self.app.policy)
            except RuntimeError as e:
                raise ie.VisionUnavailable(str(e))
            order = [m for m in order if ie.is_vision(m, ids)]
            if not order:
                raise ie.VisionUnavailable("your routing settings leave no model that can read pictures")
            prompt = VISION_PROMPT + (f"\n\nText that OCR already read (may contain mistakes):\n{hint[:2000]}" if hint.strip() else "")
            msgs = [{"role": "system", "content": VISION_SYSTEM},
                    {"role": "user", "content": [{"type": "text", "text": prompt},
                                                 {"type": "image_url", "image_url": {"url": ie.image_to_data_url(img)}}]}]
            try:
                res = streaming.run_chat(order, msgs, ns["get_client"], token, self.app.policy, status_cb, None)
            except streaming.Cancelled:
                raise ie.Stopped()
            except streaming.AllFailed as e:
                raise ie.VisionUnavailable("every picture-capable model failed: " + str(e).replace("\n", " ")[:160])
            return res["answer"].strip(), res["label"]
        return vision

    def make_audio(self, cfg):
        try:
            acfg = json.loads((Path(self.ns["DATA_DIR"]) / "audio.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            acfg = {}
        model, lang = acfg.get("model", cfg.get("audio_model", "small")), acfg.get("lang", cfg.get("audio_lang", "auto"))

        def audio(path, progress, should_stop):
            import audio_engine
            state = {"stage": "Starting"}

            def on_status(t):
                state["stage"] = "Transcribing" if t.startswith("Transcribing") else t[:60]
                progress(state["stage"], 0.0, "")

            def on_progress(done, total):
                progress("Transcribing", done / total if total else 0.0,
                         f"{audio_engine.fmt_time(done)} of {audio_engine.fmt_time(total)}")
            res = audio_engine.analyze(path, model, None if lang == "auto" else lang, False, None, None,
                                       on_progress, on_status, None, None, should_stop)
            return audio_engine.build_text(res, Path(path).name), res
        return audio

    def worker(self, todo, allow_vision, first, cfg):
        ocr = None
        if any(i.kind in ("image", "pdf") for i in todo) and cfg.get("image_mode") != "vision":
            ocr = ie.OCR(cfg.get("ocr_engine", "auto"), cfg.get("ocr_langs", "eng+deu"))
            if ocr.setup():
                self.say(f"OCR engine: {ocr.name}")
            else:
                self.say(f"No OCR engine ({ocr.problem}). Pictures will only be described by a model, if allowed.")
                ocr = None
        cfg = dict(cfg, allow_vision=allow_vision)
        vision = self.make_vision(first, self.token, lambda t: None) if allow_vision else None
        proc = ie.Processor(cfg, self.store, ocr, vision, self.make_audio(cfg), self.say)
        done = failed = 0
        for item in todo:
            if self.stop_flag.is_set():
                item.status, item.stage = "Stopped", "Stopped before it started"
                continue
            item.status, item.stage, item.started = "Working", "Starting", time.time()

            def progress(stage, frac, note, it=item):
                it.stage, it.frac, it.note = stage, frac, note
            try:
                method = proc.run(item, progress, lambda it=item: self.stop_flag.is_set() or it.stop.is_set())
                item.status, item.stage, item.frac, item.note = "Done", "Finished", 1.0, method
                done += 1
            except ie.Stopped:
                item.status, item.stage = "Stopped", "Stopped"
            except Exception as e:
                item.status, item.stage, item.note = "Failed", "Failed", f"{type(e).__name__}: {str(e)[:200]}"
                failed += 1
            item.finished = time.time()
        self.q.put(("finished", done, failed))

    def stop(self):
        self.stop_flag.set()
        if self.token is not None:
            try:
                self.token.cancel()
            except Exception:
                pass
        for i in self.items:
            if i.status == "Working":
                i.stage = "Stopping ..."

    # ------------------------------------------------------------ main-thread pump
    def note(self, text):
        self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {text}")
        self.log_lines = self.log_lines[-1000:]
        if self.panel is not None:
            self.panel.append_log(self.log_lines[-1])

    def tick(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.note(msg[1])
                elif kind == "progress":
                    self.status(msg[1])
                elif kind == "found":
                    self.on_found(*msg[1:])
                elif kind == "finished":
                    self.running = False
                    self.note(f"Finished: {msg[1]} done, {msg[2]} failed.")
                    if self.reindex_plain is not None:
                        self.reindex_plain()                       # index the new text
                    self.status(f"Documents processed: {msg[1]} done" + (f", {msg[2]} failed" if msg[2] else "") + ".")
                    left = [i for i in self.items if i.status == "Queued"]
                    if left:
                        self.note(f"{len(left)} more file(s) are waiting - press Re-scan or Process for the next batch.")
        except queue.Empty:
            pass
        if self.panel is not None:
            self.panel.refresh()
        if self.running or self.discovering or (self.panel is not None and self.panel.alive()):
            self.app.after(500, self.tick)

    def open(self):
        if self.panel is None:
            self.panel = Panel(self)
        self.panel.open()


class Panel:
    COLS = (("file", 280), ("type", 150), ("status", 80), ("stage", 190), ("progress", 70), ("note", 220))

    def __init__(self, ing):
        self.ing, self.app, self.win = ing, ing.app, None

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
        win.title("Cerry - Documents processing")
        win.geometry("1100x720")
        self.summary = tk.StringVar(value="")
        ttk.Label(win, textvariable=self.summary, padding=8, wraplength=1060).pack(fill="x")
        self.build_options(win)
        self.table = ttk.Treeview(win, columns=[c for c, _ in self.COLS], show="headings", selectmode="browse", height=9)
        for c, w in self.COLS:
            self.table.heading(c, text=c.title())
            self.table.column(c, width=w)
        self.table.pack(fill="x", padx=8)
        self.table.tag_configure("bad", foreground="#b00020")
        self.table.tag_configure("done", foreground="#2e7d32")
        bar = ttk.Frame(win, padding=6)
        bar.pack(fill="x")
        for text, fn in (("Check folders", lambda: self.ing.discover(False, True)), ("Process next batch", self.process),
                         ("Stop", self.ing.stop), ("Show text", self.show_text), ("Clear finished", self.clear),
                         ("Reprocess selected", self.reprocess)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        self.progress = ttk.Progressbar(win, maximum=100)
        self.progress.pack(fill="x", padx=8)
        self.log = scrolledtext.ScrolledText(win, wrap="word", state="disabled", height=12)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        for line in self.ing.log_lines:
            self.append_log(line)
        self.refresh()

    def build_options(self, win):
        c = self.ing.cfg
        box = ttk.LabelFrame(win, text="What to read", padding=6)
        box.pack(fill="x", padx=8)
        self.vars = {}
        row = ttk.Frame(box)
        row.pack(fill="x")
        for key, text in (("images", "pictures"), ("pdf_pages", "PDF pages without text (scans)"), ("audio", "audio / video"),
                          ("allow_vision", "let a picture model describe them"), ("ask_before_cloud", "ask before sending online")):
            v = tk.BooleanVar(value=bool(c.get(key, True)))
            self.vars[key] = v
            ttk.Checkbutton(row, text=text, variable=v, command=lambda k=key: self.set(k)).pack(side="left", padx=6)
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Pictures:").pack(side="left")
        for key, values, width in (("image_mode", ["auto", "ocr", "vision", "both"], 8), ("ocr_engine", ["auto", "tesseract", "rapidocr"], 10)):
            v = tk.StringVar(value=str(c.get(key)))
            self.vars[key] = v
            if key == "ocr_engine":
                ttk.Label(row2, text="OCR engine:").pack(side="left", padx=(12, 3))
            cb = ttk.Combobox(row2, textvariable=v, values=values, state="readonly", width=width)
            cb.pack(side="left", padx=3)
            cb.bind("<<ComboboxSelected>>", lambda e, k=key: self.set(k))
        ttk.Label(row2, text="OCR languages:").pack(side="left", padx=(12, 3))
        langs = tk.StringVar(value=str(c.get("ocr_langs")))
        self.vars["ocr_langs"] = langs
        ent = ttk.Entry(row2, textvariable=langs, width=10)
        ent.pack(side="left")
        ent.bind("<FocusOut>", lambda e: self.set("ocr_langs"))
        ttk.Label(row2, text="Picture model:").pack(side="left", padx=(12, 3))
        labels = ["(automatic)"] + [m["label"] for m in self.ing.ns["MODELS"]]
        vm = tk.StringVar(value=c.get("vision_model") or labels[0])
        self.vars["vision_model"] = vm
        cb = ttk.Combobox(row2, textvariable=vm, values=labels, state="readonly", width=30)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.set("vision_model"))
        ttk.Label(row2, text="Files per run:").pack(side="left", padx=(12, 3))
        per = tk.StringVar(value=str(c.get("max_per_run", 30)))
        self.vars["max_per_run"] = per
        ent2 = ttk.Entry(row2, textvariable=per, width=5)
        ent2.pack(side="left")
        ent2.bind("<FocusOut>", lambda e: self.set("max_per_run"))

    def set(self, key):
        v = self.vars[key].get()
        if key == "vision_model":
            v = "" if v.startswith("(") else v
        elif key == "max_per_run":
            try:
                v = max(1, min(500, int(v)))
            except ValueError:
                return
        self.ing.cfg[key] = v
        self.ing.save_cfg()

    def process(self):
        if self.ing.running:
            return
        if not any(i.status == "Queued" for i in self.ing.items):
            self.ing.discover(start=True)
        else:
            self.ing.begin()

    def selected(self):
        sel = self.table.selection()
        return next((i for i in self.ing.items if sel and i.id == sel[0]), None)

    def show_text(self):
        item = self.selected()
        rec = self.ing.store.get(item.key) if item else None
        if not rec:
            return
        text = rec["text"] or "\n\n".join(f"--- page {n} ---\n{t}" for n, t in sorted(rec["pages"].items(), key=lambda x: int(x[0])) if t)
        win = tk.Toplevel(self.win)
        win.title(f"Text found in {item.name}")
        box = scrolledtext.ScrolledText(win, wrap="word", width=100, height=30)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text or "(nothing)")
        box.config(state="disabled")

    def clear(self):
        self.ing.items = [i for i in self.ing.items if i.status not in ("Done", "Failed", "Stopped")]
        self.refresh()

    def reprocess(self):
        item = self.selected()
        if item and not self.ing.running:
            self.ing.store.delete(item.key)
            item.status, item.stage, item.frac, item.note = "Queued", "Waiting", 0.0, ""
            self.ing.begin()

    def append_log(self, line):
        if self.alive():
            self.log.config(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

    def refresh(self):
        if not self.alive():
            return
        keep = self.table.selection()
        self.table.delete(*self.table.get_children())
        working = None
        for i in self.ing.items:
            tag = "bad" if i.status == "Failed" else ("done" if i.status == "Done" else "")
            self.table.insert("", "end", iid=i.id, tags=(tag,), values=(
                i.name, i.label(), i.status, i.stage[:40], f"{i.frac:.0%}" if i.status in ("Working", "Done") else "-", i.note[:60]))
            if i.status == "Working":
                working = i
        if keep and self.table.exists(keep[0]):
            self.table.selection_set(keep[0])
        self.progress["value"] = (working.frac * 100) if working else 0
        text = self.ing.summary()
        state = "Working ..." if self.ing.running else ("Looking for new files ..." if self.ing.discovering else "Idle.")
        self.summary.set(f"{state}  " + (f"Waiting: {text}. " if text else "Nothing waiting. ") +
                         "Re-scan (top bar) checks the folders and processes the next batch. Opening a workspace only counts.")


def install(ns):
    import doc_manager
    App = ns["App"]
    if App.__dict__.get("_ingest_plugin_installed"):
        return
    App._ingest_plugin_installed = True
    store = ns["_ingest_store"] = ie.Store(Path(ns["DATA_DIR"]) / "ingest.sqlite")
    try:
        cfg = json.loads((Path(ns["DATA_DIR"]) / "ingest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    try:
        import audio_engine
        audio_ext = set(audio_engine.AUDIO_EXT)
    except Exception:
        audio_ext = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mkv", ".webm", ".mov"}
    doc_manager.MAX_FILE_MB = max(doc_manager.MAX_FILE_MB, int(cfg.get("max_file_mb", ie.DEFAULTS["max_file_mb"])))
    doc_manager.extract_pages = ie.make_extract(doc_manager.extract_pages, store, audio_ext)

    original_init, original_send = App.__init__, App.send
    original_reindex, original_load = App.reindex_async, App.load_ws

    def init(app, *a, **k):
        app._ing_auto = True                       # the first scan (start-up) only counts
        original_init(app, *a, **k)
        app._ing_auto = False
        app._ing = Ingest(app, ns)
        app._ing.reindex_plain = lambda: original_reindex(app)
        parent = app.ws_box.master
        cols = [int(w.grid_info().get("column", 0)) for w in parent.grid_slaves(row=1)]
        ttk.Button(parent, text="Processing...", command=app._ing.open).grid(row=1, column=max(cols, default=0) + 1, padx=2)

    def load_ws(app, name):
        app._ing_auto = True                       # opening a workspace: count only, never start work
        try:
            return original_load(app, name)
        finally:
            app._ing_auto = False

    def reindex_async(app):
        original_reindex(app)
        ing = getattr(app, "_ing", None)
        if ing is not None:
            auto = getattr(app, "_ing_auto", False)
            ing.discover(start=not auto, deep=not auto)

    def send(app):
        if app.inp.get("1.0", "end").strip().lower() == "/ingest":
            app.inp.delete("1.0", "end")
            app._ing.open()
            return
        return original_send(app)

    App.__init__, App.send, App.load_ws, App.reindex_async = init, send, load_ws, reindex_async
