# -*- coding: utf-8 -*-
"""
Created on Thu Oct 29 14:50:36 2026

@author: panaj
"""

import math
import re
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk, scrolledtext

TEXT_EXT = {".txt", ".md", ".py", ".r", ".csv", ".tsv", ".json", ".yaml", ".yml",
            ".log", ".tex", ".html", ".ini", ".cfg", ".sh", ".ipynb"}
SUPPORTED = TEXT_EXT | {".pdf", ".docx"}
SKIP_DIRS = {"__pycache__", "node_modules", "venv", ".venv"}
MAX_FILE_MB = 8


def tokenize(text):
    return re.findall(r"\w+", text.lower())


def extract_pages(path: Path):
    """Return (pages, status, note). pages = [(page_number_or_None, text)]."""
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError as e:
        return [], "error", str(e)
    if size > MAX_FILE_MB * 1024 * 1024:
        return [], "too_large", f"{size / 1e6:.1f} MB (limit {MAX_FILE_MB} MB)"
    if ext not in SUPPORTED:
        return [], "unsupported", f"File type '{ext or 'none'}' is not read"
    try:
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return [], "error", "pypdf is not installed"
            reader = PdfReader(str(path))
            total = len(reader.pages)
            pages = [(i + 1, pg.extract_text() or "") for i, pg in enumerate(reader.pages)]
            pages = [(n, t) for n, t in pages if t.strip()]
            if not pages:
                return [], "empty", f"{total} pages, no extractable text (scanned PDF?)"
            return pages, "ok", f"{total} pages"
        if ext == ".docx":
            try:
                from docx import Document
            except ImportError:
                return [], "error", "python-docx is not installed"
            doc = Document(str(path))
            text = "\n".join(p.text for p in doc.paragraphs)
            for table in doc.tables:
                for row in table.rows:
                    text += "\n" + " | ".join(c.text for c in row.cells)
            pages = [(None, text)]
        else:
            pages = [(None, path.read_text(encoding="utf-8", errors="ignore"))]
    except Exception as e:
        return [], "error", f"{type(e).__name__}: {str(e)[:100]}"
    if not any(t.strip() for _, t in pages):
        return [], "empty", "File contains no text"
    return pages, "ok", ""


def read_full_text(path: Path) -> str:
    pages, status, _ = extract_pages(Path(path))
    return "\n".join(t for _, t in pages) if status == "ok" else ""


def preview_text(path: Path, limit=8000):
    pages, status, note = extract_pages(Path(path))
    if status != "ok":
        return f"[{status}] {note}"
    out = []
    for n, t in pages:
        out.append((f"--- page {n} ---\n" if n else "") + t)
    text = "\n\n".join(out)
    return text[:limit] + ("\n\n[... preview truncated ...]" if len(text) > limit else "")


@dataclass
class ScanRecord:
    key: str
    label: str
    rel: str
    path: Path
    status: str = "ok"
    note: str = ""
    size: int = 0
    chars: int = 0
    chunks: int = 0


class DocumentIndex:
    def __init__(self):
        self.records = []
        self.chunks = []      # (key, filename, page, text, Counter, length)
        self.df = Counter()
        self.avg_len = 1.0

    def build(self, sources, chunk_words=220, overlap=40):
        """sources: list of (label, folder). Hidden files/folders (e.g. .env) are never read."""
        records, chunks = [], []
        for label, folder in sources:
            folder = Path(folder)
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(folder)
                if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                    continue
                pages, status, note = extract_pages(path)
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                rec = ScanRecord(key=f"{label}/{rel.as_posix()}", label=label, rel=rel.as_posix(),
                                 path=path, status=status, note=note, size=size)
                step = chunk_words - overlap
                for page_no, text in pages:
                    rec.chars += len(text)
                    words = text.split()
                    for i in range(0, len(words), step):
                        piece = " ".join(words[i:i + chunk_words])
                        toks = tokenize(piece)
                        if toks:
                            chunks.append((rec.key, path.name, page_no, piece, Counter(toks), len(toks)))
                            rec.chunks += 1
                        if i + chunk_words >= len(words):
                            break
                records.append(rec)
        df = Counter()
        for c in chunks:
            for t in c[4]:
                df[t] += 1
        avg = (sum(c[5] for c in chunks) / len(chunks)) if chunks else 1.0
        self.records, self.chunks, self.df, self.avg_len = records, chunks, df, avg

    def active_files(self, excluded=()):
        return [r.rel for r in self.records if r.status == "ok" and r.key not in excluded]

    def search(self, query, k=6, excluded=()):
        q = set(tokenize(query))
        n = len(self.chunks)
        if not q or n == 0:
            return []
        k1, b = 1.5, 0.75
        scored = []
        for key, name, page, text, tf, length in self.chunks:
            if key in excluded:
                continue
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self.avg_len))
            if s > 0:
                scored.append((s, key, name, page, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, "key": key, "file": name, "page": page, "text": text}
                for s, key, name, page, text in scored[:k]]


class DocumentPanel(tk.Toplevel):
    """Table of all files: tick/untick to include them, see scan problems, preview text."""

    def __init__(self, parent, get_state, set_excluded, rescan, open_docs):
        super().__init__(parent)
        self.title("Documents")
        self.geometry("900x460")
        self.get_state, self.set_excluded = get_state, set_excluded
        self.rescan, self.open_docs = rescan, open_docs
        self._recs = {}

        self.summary = tk.StringVar()
        ttk.Label(self, textvariable=self.summary, padding=6).pack(fill="x")

        cols = ("use", "file", "status", "detail", "chunks")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, text, width in (("use", "Use", 50), ("file", "File", 330), ("status", "Status", 90),
                               ("detail", "Detail", 320), ("chunks", "Chunks", 60)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor="center" if c in ("use", "chunks") else "w")
        self.tree.tag_configure("bad", foreground="#b3261e")
        self.tree.tag_configure("off", foreground="#888888")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", lambda e: self.preview())

        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=self.select_all).pack(side="left", padx=2)
        ttk.Button(bar, text="Select none", command=self.select_none).pack(side="left", padx=2)
        ttk.Button(bar, text="Preview text", command=self.preview).pack(side="left", padx=2)
        ttk.Button(bar, text="Re-scan", command=self.rescan).pack(side="left", padx=2)
        ttk.Button(bar, text="Open folder", command=self.open_docs).pack(side="left", padx=2)
        ttk.Label(bar, text="Click the box to include/exclude a file. Double-click previews it.",
                  foreground="#666").pack(side="right")
        self.refresh()

    def refresh(self):
        index, excluded = self.get_state()
        self._recs = {r.key: r for r in index.records}
        self.tree.delete(*self.tree.get_children())
        ok = included = 0
        for r in index.records:
            if r.status == "ok":
                ok += 1
                used = r.key not in excluded
                included += used
                mark, tag = ("☑" if used else "☐"), ("" if used else "off")
            else:
                mark, tag = "–", "bad"
            self.tree.insert("", "end", iid=r.key, tags=(tag,),
                             values=(mark, f"[{r.label}] {r.rel}", r.status, r.note, r.chunks))
        self.summary.set(f"{len(index.records)} files found, {ok} readable, {included} included, "
                         f"{len(index.records) - ok} skipped or unreadable.")

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        rec = self._recs.get(row)
        if rec and rec.status == "ok":
            _, excluded = self.get_state()
            excluded ^= {row}
            self.set_excluded(excluded)
            self.refresh()

    def select_all(self):
        self.set_excluded(set())
        self.refresh()

    def select_none(self):
        index, _ = self.get_state()
        self.set_excluded({r.key for r in index.records if r.status == "ok"})
        self.refresh()

    def preview(self):
        sel = self.tree.selection()
        rec = self._recs.get(sel[0]) if sel else None
        if not rec:
            return
        win = tk.Toplevel(self)
        win.title(f"Preview - {rec.rel}")
        win.geometry("720x480")
        box = scrolledtext.ScrolledText(win, wrap="word")
        box.pack(fill="both", expand=True)
        box.insert("1.0", preview_text(rec.path))
        box.config(state="disabled")