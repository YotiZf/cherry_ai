"""Cerry ingest engine - no GUI.  Turns pictures, scanned PDF pages and audio files into searchable text.

Pieces
  Store            small SQLite database (ai_workspace_data/ingest.sqlite): the text found in each file, so a file is
                   processed once.  The key is path + modification time + size, so a changed file is processed again.
  find_work        walks the workspace folders and lists what still needs processing:
                     image files          -> OCR and/or a description by a vision model
                     PDF pages w/o text   -> pages are drawn as pictures, then handled like pictures (OCR / vision)
                     audio / video files  -> transcript (faster-whisper, see audio_engine.py)
  Processor        does the work for one item and reports progress
  make_extract     wraps doc_manager.extract_pages, so the document index sees the extracted text as if the
                   file had been a normal text document (same file name in the evidence list, page numbers for PDFs)
Optional packages (only for the parts you use):
  OCR           pip install rapidocr-onnxruntime        (pure pip)   or  Tesseract + pip install pytesseract
  PDF pages     pip install pypdfium2                    (or pymupdf)
  Pictures      pip install pillow
  Audio         pip install faster-whisper
"""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
SKIP_DIRS = {"__pycache__", "node_modules", "venv", ".venv", "extracted"}
MIN_PAGE_CHARS = 25                    # a PDF page with less text than this is treated as a picture
MAX_IMAGE_MB = 40
VISION_HINTS = ("gemini", "-vl", "vl-", "vision", "llava", "pixtral", "llama-4", "gpt-4o", "gpt-4.1", "gemma-3",
                "gemma-4", "qwen-vl", "internvl", "molmo")
DEFAULTS = {
    "images": True, "pdf_pages": True, "audio": True,
    "image_mode": "auto",              # auto | ocr | vision | both
    "ocr_engine": "auto",              # auto | tesseract | rapidocr
    "ocr_langs": "eng+deu",
    "ocr_min_chars": 60,               # auto mode: less text than this -> ask a vision model as well
    "allow_vision": True, "ask_before_cloud": True, "vision_model": "",
    "pdf_dpi_scale": 2.0, "pdf_max_pages": 80, "pdf_figures": False,
    "audio_model": "small", "audio_lang": "auto",
    "max_per_run": 30, "max_file_mb": 40,
}


class Stopped(Exception):
    pass


class VisionUnavailable(Exception):
    pass


def file_key(path):
    p = Path(path)
    st = p.stat()
    return f"{p}|{st.st_mtime_ns}|{st.st_size}"


def is_vision(model, vision_ids=()):
    if "vision" in model:
        return bool(model["vision"])
    mid = str(model.get("id", "")).lower()
    return model.get("id") in vision_ids or any(h in mid for h in VISION_HINTS)


def fmt_time(seconds):
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------------- store
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=OFF")
            self.db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, kind TEXT, method TEXT, model TEXT,"
                            " text TEXT, pages TEXT, seconds REAL, created REAL, error TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS pdfscan (key TEXT PRIMARY KEY, lens TEXT)")

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT kind, method, model, text, pages, seconds, created, error FROM results WHERE key=?",
                                  (key,)).fetchone()
        if not row:
            return None
        return {"kind": row[0], "method": row[1], "model": row[2], "text": row[3] or "",
                "pages": json.loads(row[4]) if row[4] else {}, "seconds": row[5], "created": row[6], "error": row[7] or ""}

    def put(self, key, kind, method, model="", text="", pages=None, seconds=0.0, error=""):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                            (key, kind, method, model, text, json.dumps(pages or {}), seconds, time.time(), error))

    def delete(self, key):
        with self.lock:
            self.db.execute("DELETE FROM results WHERE key=?", (key,))

    def pdf_lens(self, key):
        with self.lock:
            row = self.db.execute("SELECT lens FROM pdfscan WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_pdf_lens(self, key, lens):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pdfscan VALUES (?,?)", (key, json.dumps(lens)))


# ---------------------------------------------------------------------- discovery
class WorkItem:
    def __init__(self, path, kind, key, pages=None):
        self.path, self.kind, self.key, self.pages = Path(path), kind, key, pages or []
        self.id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        self.status, self.stage, self.frac, self.note = "Queued", "Waiting", 0.0, ""
        self.started = self.finished = 0.0
        self.stop = threading.Event()

    @property
    def name(self):
        return self.path.name

    def label(self):
        return {"image": "Picture", "pdf": f"PDF ({len(self.pages)} page(s) without text)", "audio": "Audio"}[self.kind]


def iter_files(sources):
    for label, folder in sources:
        folder = Path(folder)
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(folder)
            except ValueError:
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                continue
            yield path


def pdf_page_lengths(path):
    """Characters of text on every page of a PDF (needs pypdf)."""
    from pypdf import PdfReader
    return [len((pg.extract_text() or "").strip()) for pg in PdfReader(str(path)).pages]


def find_work(sources, store, cfg, audio_ext, progress=None, should_stop=None, scan_new_pdfs=True):
    """-> (items, counts).  Items that are already done are not listed.
    scan_new_pdfs=False skips PDFs that were never checked before (checking reads their text, which is slow)."""
    items, counts = [], {"image": 0, "pdf": 0, "audio": 0, "done": 0}
    pdf_seen = 0
    for path in iter_files(sources):
        if should_stop and should_stop():
            raise Stopped()
        ext = path.suffix.lower()
        kind = "image" if ext in IMAGE_EXT else "audio" if ext in audio_ext else "pdf" if ext in PDF_EXT else None
        if kind is None or not cfg.get({"image": "images", "audio": "audio", "pdf": "pdf_pages"}[kind], True):
            continue
        try:
            key = file_key(path)
            size_mb = path.stat().st_size / 1e6
        except OSError:
            continue
        if kind == "image" and size_mb > MAX_IMAGE_MB:
            continue
        rec = store.get(key)
        if kind in ("image", "audio"):
            if rec and rec["text"]:
                counts["done"] += 1
            else:
                counts[kind] += 1
                items.append(WorkItem(path, kind, key))
            continue
        lens = store.pdf_lens(key)                              # PDF: which pages carry no text?
        if lens is None:
            if not scan_new_pdfs:
                continue
            pdf_seen += 1
            if progress:
                progress(f"Checking PDF {pdf_seen}: {path.name}")
            try:
                lens = pdf_page_lengths(path)
            except Exception:
                continue
            store.put_pdf_lens(key, lens)
        empty = [i + 1 for i, n in enumerate(lens) if n < MIN_PAGE_CHARS]
        done_pages = {k for k, v in rec["pages"].items() if v} if rec else set()
        todo = [p for p in empty if str(p) not in done_pages][: int(cfg.get("pdf_max_pages", 80))]
        if todo:
            counts["pdf"] += 1
            items.append(WorkItem(path, "pdf", key, todo))
        elif empty:
            counts["done"] += 1
    return items, counts


# ---------------------------------------------------------------------- OCR and rendering
class OCR:
    """Text from a picture.  'auto' prefers Tesseract (best for German) and falls back to RapidOCR."""

    def __init__(self, engine="auto", langs="eng+deu"):
        self.pref, self.langs, self.name, self.problem, self._eng = engine, langs, None, "", None

    @staticmethod
    def _tesseract_cmd():
        found = shutil.which("tesseract")
        if found:
            return found
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(p):
                return p
        return None

    def setup(self):
        problems = []
        if self.pref in ("auto", "tesseract"):
            try:
                import pytesseract
                cmd = self._tesseract_cmd()
                if not cmd:
                    raise RuntimeError("the Tesseract program is not installed")
                pytesseract.pytesseract.tesseract_cmd = cmd
                have = set(pytesseract.get_languages(config=""))
                langs = "+".join(l for l in self.langs.split("+") if l in have) or "eng"
                self._eng, self.name, self._lang = pytesseract, "tesseract (" + langs + ")", langs
                return True
            except Exception as e:
                problems.append(f"Tesseract: {str(e)[:80]}")
        if self.pref in ("auto", "rapidocr"):
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._eng, self.name = RapidOCR(), "rapidocr"
                return True
            except Exception as e:
                problems.append(f"RapidOCR: {str(e)[:80]}  (pip install rapidocr-onnxruntime)")
        self.problem = "; ".join(problems) or "no OCR engine"
        return False

    def read(self, img):
        if self.name and self.name.startswith("tesseract"):
            return self._eng.image_to_string(img, lang=self._lang).strip()
        import numpy as np
        result, _ = self._eng(np.array(img.convert("RGB")))
        if not result:
            return ""
        rows = sorted(result, key=lambda r: (round(r[0][0][1] / 14), r[0][0][0]))
        lines, last = [], None
        for box, text, _score in rows:
            row = round(box[0][1] / 14)
            if last is not None and row == last:
                lines[-1] += " " + text
            else:
                lines.append(text)
            last = row
        return "\n".join(lines).strip()


def render_pdf_pages(path, pages, scale=2.0):
    """Yield (page_number, PIL image).  Uses pypdfium2, or PyMuPDF when that is what you have."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            for n in pages:
                yield n, pdf[n - 1].render(scale=scale).to_pil().convert("RGB")
        finally:
            pdf.close()
        return
    except ImportError:
        pass
    try:
        import fitz
        from PIL import Image
    except ImportError:
        raise RuntimeError("Drawing PDF pages needs a package: pip install pypdfium2   (or pymupdf)")
    doc = fitz.open(str(path))
    try:
        for n in pages:
            pix = doc[n - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
            yield n, Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def image_to_data_url(img, max_side=1568):
    from PIL import Image
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------- processing
def compose(kind, name, method, model, ocr_text, description):
    head = f"[{kind}: {name} | {method}" + (f" | model: {model}" if model else "") + "]"
    parts = [head]
    if ocr_text.strip():
        parts += ["Text in the picture:", ocr_text.strip()]
    if description.strip():
        parts += ["Description:", description.strip()]
    return "\n".join(parts)


class Processor:
    """vision(img, ocr_text) -> (text, model_label) or raises VisionUnavailable.
    audio(path, on_progress, on_status, should_stop) -> (text, info dict)."""

    def __init__(self, cfg, store, ocr=None, vision=None, audio=None, log=None):
        self.cfg, self.store, self.ocr, self.vision, self.audio = cfg, store, ocr, vision, audio
        self.log = log or (lambda t: None)
        self.vision_ok = bool(cfg.get("allow_vision", True)) and vision is not None

    def read_picture(self, img, label):
        """-> (text, method, model).  Follows the 'image_mode' setting."""
        mode = self.cfg.get("image_mode", "auto")
        ocr_text, desc, model, used = "", "", "", []
        if mode in ("auto", "ocr", "both") and self.ocr is not None and self.ocr.name:
            ocr_text = self.ocr.read(img)
            used.append("OCR " + self.ocr.name.split(" ")[0])
        want_vision = mode in ("vision", "both") or (mode == "auto" and len(ocr_text.strip()) < int(self.cfg.get("ocr_min_chars", 60)))
        if want_vision and self.vision_ok:
            try:
                desc, model = self.vision(img, ocr_text)
                used.append("vision")
            except VisionUnavailable as e:
                self.log(f"  {label}: no picture analysis ({e})")
        return ocr_text, desc, " + ".join(used) or "nothing readable", model

    def run(self, item, progress, should_stop):
        """progress(stage, fraction, note).  -> short summary text.  Raises Stopped."""
        t0 = time.time()
        if item.kind == "image":
            from PIL import Image, ImageOps
            progress("Reading the picture", 0.1, "")
            img = ImageOps.exif_transpose(Image.open(item.path)).convert("RGB")
            ocr_text, desc, method, model = self.read_picture(img, item.name)
            text = compose("Picture", item.name, method, model, ocr_text, desc)
            if not (ocr_text.strip() or desc.strip()):
                raise RuntimeError("nothing readable: no OCR engine and no usable vision model (see the options)")
            self.store.put(item.key, "image", method, model, text, seconds=time.time() - t0)
            return method
        if item.kind == "pdf":
            pages_done = dict((self.store.get(item.key) or {}).get("pages", {}))
            total = len(item.pages)
            methods = set()
            for n, (page_no, img) in enumerate(render_pdf_pages(item.path, item.pages, float(self.cfg.get("pdf_dpi_scale", 2.0)))):
                if should_stop():
                    raise Stopped()
                progress(f"Page {page_no} ({n + 1}/{total})", n / total, "")
                ocr_text, desc, method, model = self.read_picture(img, f"{item.name} p.{page_no}")
                methods.add(method)
                if ocr_text.strip() or desc.strip():          # unreadable pages stay "to do" for a later run
                    pages_done[str(page_no)] = compose(f"PDF page {page_no}", item.name, method, model, ocr_text, desc)
                    self.store.put(item.key, "pdf", ", ".join(sorted(methods)), "", "", pages_done, time.time() - t0)
            if not any(str(p) in pages_done for p in item.pages):
                raise RuntimeError("nothing readable: no OCR engine and no usable vision model (see the options)")
            return ", ".join(sorted(methods))
        if item.kind == "audio":
            if self.audio is None:
                raise RuntimeError("audio transcription is not available (pip install faster-whisper)")
            text, info = self.audio(item.path, lambda stage, frac, note: progress(stage, frac, note), should_stop)
            if not text.strip():
                raise RuntimeError("no speech found")
            method = f"transcript ({self.cfg.get('audio_model', 'small')})"
            self.store.put(item.key, "audio", method, "", text, seconds=time.time() - t0)
            return method
        raise ValueError(item.kind)


# ---------------------------------------------------------------------- the document-index hook
def make_extract(original, store, audio_ext, enabled=lambda: True):
    """A replacement for doc_manager.extract_pages that also knows pictures, audio and OCR'd PDF pages."""
    def extract(path):
        p = Path(path)
        ext = p.suffix.lower()
        if enabled() and (ext in IMAGE_EXT or ext in audio_ext):
            try:
                rec = store.get(file_key(p))
            except OSError:
                rec = None
            if rec and rec["text"]:
                return [(None, rec["text"])], "ok", rec["method"] + (f" ({rec['model']})" if rec["model"] else "")
            return [], "pending", "not processed yet - press Re-scan to read it"
        pages, status, note = original(path)
        if enabled() and ext in PDF_EXT:
            try:
                rec = store.get(file_key(p))
            except OSError:
                rec = None
            ocr = {int(k): v for k, v in (rec["pages"].items() if rec else []) if v.strip()}
            if ocr:
                merged = {n: t for n, t in pages if n}
                for n, t in ocr.items():
                    if len(merged.get(n, "").strip()) < MIN_PAGE_CHARS:
                        merged[n] = t
                return sorted(merged.items()), "ok", (note + " + OCR/vision pages").strip()
        return pages, status, note
    return extract
