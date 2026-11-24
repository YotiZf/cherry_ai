"""Cache extracted pages for document previews and indexing in doc_cache.sqlite."""
import json
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
