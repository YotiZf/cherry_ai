"""Cerry workspaces - no GUI.  A workspace is a folder with documents, chats, notes and instructions.

Split out of ai_workspace_voice.py so that it can be tested and reused on its own.
Compared with the old built-in version:
  * the list of chats is cached: a chat file is only parsed again when it changed (before, every refresh parsed
    every chat file of the workspace completely);
  * chat files, the workspace config and the settings are written atomically (a crash while saving can no longer
    leave a half-written chat file);
  * files are always read and written as UTF-8.
"""
import json
import logging
import os
import uuid
from pathlib import Path

from doc_manager import DocumentIndex
from document_memory import DocumentMemory


def write_atomic(path, text):
    """Write text so that the file is either the old or the new version, never half of each."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Workspace:
    ROOT = None            # folder that holds all workspaces; set once by configure()

    def __init__(self, name):
        self.name = name
        self.dir = Workspace.ROOT / name
        self.docs = self.dir / "documents"
        self.chats_dir = self.dir / "chats"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chats_dir.mkdir(exist_ok=True)

        old_docs = self.dir / "docs"
        if old_docs.exists() and not self.docs.exists():
            old_docs.rename(self.docs)
        self.docs.mkdir(exist_ok=True)

        self.cfg_path = self.dir / "config.json"
        self.notes_path = self.dir / "notes.md"
        self.instr_path = self.dir / "instructions.txt"
        for p in (self.notes_path, self.instr_path):
            p.touch(exist_ok=True)
        self.cfg = {"folder": "", "excluded": []}
        if self.cfg_path.exists():
            try:
                self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except Exception:
                logging.getLogger(__name__).exception("Could not read workspace config %s", self.cfg_path)
                pass
        self.index = DocumentIndex(DocumentMemory(self.dir / "document_memory.sqlite3"), self.sources())
        self._meta = {}        # chat file -> (mtime_ns, size, id, title, workspace)

        old_hist = self.dir / "history.json"
        if old_hist.exists():
            try:
                msgs = json.loads(old_hist.read_text(encoding="utf-8"))
            except Exception:
                logging.getLogger(__name__).exception("Could not import legacy chat history %s", old_hist)
                msgs = []
            if msgs:
                self.save_chat({"id": uuid.uuid4().hex[:8], "title": "Imported chat", "messages": msgs})
            old_hist.rename(self.dir / "history.json.bak")

    @property
    def excluded(self):
        return set(self.cfg.get("excluded", []))

    def save_cfg(self):
        write_atomic(self.cfg_path, json.dumps(self.cfg, indent=2))

    # ---------------------------------------------------------------- chats
    def list_chats(self):
        """[(chat_id, title)] newest first.  Only files that changed since the last call are parsed."""
        items, seen = [], set()
        for p in self.chats_dir.glob("*.json"):
            try:
                st = p.stat()
                stamp = (st.st_mtime_ns, st.st_size)
                meta = self._meta.get(p)
                if meta is None or meta[:2] != stamp:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    meta = stamp + (d["id"], d.get("title", "Chat"), d.get("workspace", self.name))
                    self._meta[p] = meta
                seen.add(p)
                if meta[4] != self.name:
                    continue          # this chat file belongs to another workspace
                items.append((st.st_mtime, meta[2], meta[3]))
            except Exception:
                logging.getLogger(__name__).exception("Could not read chat metadata %s", p)
                pass
        for gone in set(self._meta) - seen:
            self._meta.pop(gone, None)
        items.sort(reverse=True)
        return [(cid, title) for _, cid, title in items]

    def load_chat(self, cid):
        chat = json.loads((self.chats_dir / f"{cid}.json").read_text(encoding="utf-8"))
        if chat.get("workspace", self.name) != self.name:
            raise FileNotFoundError(f"Chat {cid} belongs to workspace '{chat.get('workspace')}'")
        chat["workspace"] = self.name
        return chat

    def last_chat_id(self):
        """Last chat opened in THIS workspace, else its most recent chat, else None."""
        ids = [c[0] for c in self.list_chats()]
        last = self.cfg.get("last_chat")
        return last if last in ids else (ids[0] if ids else None)

    def remember_chat(self, cid):
        if self.cfg.get("last_chat") != cid:
            self.cfg["last_chat"] = cid
            self.save_cfg()

    def save_chat(self, chat):
        chat["workspace"] = self.name
        path = self.chats_dir / f"{chat['id']}.json"
        write_atomic(path, json.dumps(chat, ensure_ascii=False, indent=1))
        self._meta.pop(path, None)

    def new_chat(self, title="New chat"):
        chat = {"id": uuid.uuid4().hex[:8], "title": title, "workspace": self.name, "messages": []}
        self.save_chat(chat)
        return chat

    def delete_chat(self, cid):
        p = self.chats_dir / f"{cid}.json"
        if p.exists():
            p.unlink()
        self._meta.pop(p, None)

    # ---------------------------------------------------------------- documents
    def sources(self):
        sources = [("documents", self.docs)]
        if self.cfg.get("folder"):
            sources.append(("linked", self.cfg["folder"]))
        return sources

    def reindex(self):
        self.index.build(self.sources())

    def read(self, path):
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()


def configure(root):
    Workspace.ROOT = Path(root)
    Workspace.ROOT.mkdir(parents=True, exist_ok=True)


def list_workspaces():
    names = sorted(p.name for p in Workspace.ROOT.iterdir() if p.is_dir())
    if not names:
        Workspace("default")
        names = ["default"]
    return names
