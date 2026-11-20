"""Cerry docs: faster document reading and searching.

1. Cache: every time a workspace is opened or re-scanned, Cerry read every PDF / Word / text file again. Now the
   extracted text of each file is remembered (keyed by path, modification time and size) in a small local database,
   ai_workspace_data/doc_cache.sqlite, so only new or changed files are read. Delete that file to start fresh.
2. Search index: asking a question used to compare it with EVERY text chunk of EVERY document, on the window thread.
   Now each word points to the chunks that contain it, so only those chunks are scored. The scores and the order of
   the results are exactly the same as before.
Switch on:  "cerry_docs" in ai_workspace_data/plugins.json
"""
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

ORDER = 5
KEEP_DAYS = 120


def install(ns):
    import doc_manager
    if getattr(doc_manager, "_cd_installed", False):
        return
    doc_manager._cd_installed = True
    stats = doc_manager._cd_stats = {"hits": 0, "misses": 0}

    # ------------------------------------------------------------------ 1. extraction cache
    db = sqlite3.connect(str(Path(ns["DATA_DIR"]) / "doc_cache.sqlite"), check_same_thread=False, isolation_level=None)
    lock = threading.Lock()
    with lock:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, data TEXT NOT NULL, used REAL NOT NULL)")
        db.execute("DELETE FROM pages WHERE used < ?", (time.time() - KEEP_DAYS * 86400,))
    original_extract = doc_manager.extract_pages

    def cached(path):
        p = Path(path)
        try:
            st = p.stat()
            key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            return original_extract(path)
        try:
            with lock:
                row = db.execute("SELECT data FROM pages WHERE key=?", (key,)).fetchone()
                if row:
                    db.execute("UPDATE pages SET used=? WHERE key=?", (time.time(), key))
            if row:
                d = json.loads(row[0])
                stats["hits"] += 1
                return [tuple(x) for x in d["pages"]], d["status"], d["note"]
        except (sqlite3.Error, ValueError, KeyError):
            pass
        pages, status, note = original_extract(path)
        stats["misses"] += 1
        if status in ("ok", "empty"):
            try:
                with lock:
                    db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)",
                               (key, json.dumps({"pages": pages, "status": status, "note": note}), time.time()))
            except sqlite3.Error:
                pass
        return pages, status, note

    doc_manager.extract_pages = cached

    # ------------------------------------------------------------------ 2. inverted index for search
    Index = getattr(doc_manager, "DocumentIndex", None)
    if Index is None:
        return
    original_build, original_search = Index.build, Index.search

    def build(self, *args, **kwargs):
        result = original_build(self, *args, **kwargs)
        post = {}
        for i, chunk in enumerate(self.chunks):
            for term in chunk[4]:                      # chunk[4] = Counter of the words in the chunk
                post.setdefault(term, []).append(i)
        self._cd_post = post
        return result

    def search(self, query, k=6, excluded=()):
        post = getattr(self, "_cd_post", None)
        if post is None or len(post) == 0:
            return original_search(self, query, k, excluded)
        q = set(doc_manager.tokenize(query))
        n = len(self.chunks)
        if not q or n == 0:
            return []
        k1, b = 1.5, 0.75
        candidates = set()
        for term in q:
            candidates.update(post.get(term, ()))
        scored = []
        for i in sorted(candidates):                   # same order as before, so equal scores keep their order
            key, name, page, text, tf, length = self.chunks[i]
            if key in excluded:
                continue
            s = 0.0
            for term in q:
                f = tf.get(term)
                if f:
                    idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self.avg_len))
            if s > 0:
                scored.append((s, key, name, page, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, "key": key, "file": name, "page": page, "text": text}
                for s, key, name, page, text in scored[:k]]

    Index.build, Index.search = build, search
