# -*- coding: utf-8 -*-
"""
Created on Thu Oct 29 14:56:02 2026

@author: panaj
"""

import re

_STOP = {"the", "and", "for", "that", "with", "this", "from", "are", "was", "what", "how", "you",
         "your", "can", "about", "have", "has", "not", "but", "out", "all", "any", "does", "did",
         "der", "die", "das", "und", "ist", "ein", "eine", "mit", "von", "zu", "den", "nicht"}


def _words(text):
    return re.findall(r"\w+", text.lower())


def matched_terms(query, text, limit=6):
    in_text = set(_words(text))
    seen, out = set(), []
    for w in _words(query):
        if len(w) > 2 and w not in _STOP and w in in_text and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


def build_evidence(hits, query):
    """hits come from DocumentIndex.search(). Entries keep the full text for the prompt."""
    out = []
    for n, h in enumerate(hits, 1):
        out.append({"id": f"D{n}", "kind": "excerpt", "file": h["file"], "page": h.get("page"),
                    "score": round(h["score"], 2), "matched": matched_terms(query, h["text"]),
                    "text": h["text"]})
    return out


def attachment_evidence(items, start=1):
    """items = [(filename, text)] for files attached to the next message."""
    out = []
    for n, (name, text) in enumerate(items, start):
        out.append({"id": f"D{n}", "kind": "attachment", "file": name, "page": None,
                    "score": None, "matched": [], "text": text})
    return out


def format_prompt(evidence):
    if not evidence:
        return ""
    parts = []
    for e in evidence:
        where = e["file"] + (f", page {e['page']}" if e.get("page") else "")
        tag = " (attached, full text)" if e["kind"] == "attachment" else ""
        parts.append(f"[{e['id']}] {where}{tag}\n{e['text']}")
    return ("Document excerpts. Cite the ones you use as [D1], [D2], ...:\n\n" + "\n\n".join(parts))


def compact(evidence, max_chars=700):
    """Version stored in the chat file: no full text, trimmed excerpt."""
    out = []
    for e in evidence:
        text = " ".join(e["text"].split())
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + " ..."
        item = {k: v for k, v in e.items() if k != "text"}
        item["excerpt"] = text
        out.append(item)
    return out


def render_lines(evidence):
    lines = []
    for e in evidence:
        where = e["file"] + (f", p. {e['page']}" if e.get("page") else "")
        extra = []
        if e.get("score") is not None:
            extra.append(f"score {e['score']}")
        if e.get("matched"):
            extra.append("matched: " + ", ".join(e["matched"]))
        if e["kind"] == "attachment":
            extra.append("attached file")
        suffix = f"  ({'; '.join(extra)})" if extra else ""
        lines.append(f"[{e['id']}] {where}{suffix}\n    {e.get('excerpt', '')}")
    return "\n\n".join(lines)