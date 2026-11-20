"""Cerry memory engine - no GUI.

A memory is one line that starts with "- " in a plain Markdown file:
    global memory     ai_workspace_data/global_memory.md      (true everywhere: your profession, language, tools ...)
    workspace memory  workspaces/<name>/notes.md              (true for this project only)
These are the same two files Cerry has always put into the prompt, so /remember and /note and the old "Edit ..."
buttons keep working.  A line like "- \u2605 text" is pinned: it is always sent to the model.
Everything that is not a "- " line (headings, free text) is left exactly as it is when memories are edited.
"""
import json
import os
import re
from pathlib import Path

PIN = "\u2605"
BULLET = re.compile(r"^\s*[-*]\s+(?:(" + PIN + r")\s*)?(.*\S)\s*$")
SECRET = re.compile(
    r"(api[_ -]?key|passwor[dt]|passwort|secret|token|iban|credit card|kreditkarte|pin code)\s*(is|ist|:|=)"
    r"|\bsk-[A-Za-z0-9_-]{16,}|\bAIza[0-9A-Za-z_-]{20,}|\bgsk_[A-Za-z0-9]{16,}|\bhf_[A-Za-z0-9]{16,}|\b[A-Za-z0-9+/=_-]{36,}\b",
    re.I)
SIGNALS = re.compile(
    r"\b(i am|i'm|i work|i use|i like|i love|i hate|i prefer|i need|i speak|i live|i study|i have|my |mine|we use|we are|"
    r"our |always|never|from now on|going forward|please use|please don't|do not|don't|remember|"
    r"ich bin|ich arbeite|ich nutze|ich benutze|ich mag|ich liebe|ich brauche|ich spreche|ich wohne|mein|meine|"
    r"wir nutzen|wir sind|unser|immer|nie|ab jetzt|bitte nutze|bitte nicht|merk dir|denk daran)\b", re.I)
REMEMBER_CMD = re.compile(r"^\s*(?:please\s+|bitte\s+)?(?:remember|merk dir|merke dir|denk daran)(?:\s+that|\s+dass)?[\s:,-]+(.+)$",
                          re.I | re.S)
GLOBAL_WORDS = re.compile(r"\b(always|everywhere|in general|globally|in all|immer|überall|ueberall|generell|global)\b", re.I)
WORKSPACE_WORDS = re.compile(r"\b(this (workspace|project)|for this|in this|hier|dieses projekt|in diesem)\b", re.I)


def words(text):
    return set(w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2)


def similar(a, b, threshold=0.75):
    """True when two memories say (almost) the same."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    na, nb = " ".join(sorted(wa)), " ".join(sorted(wb))
    if na in nb or nb in na:
        return True
    return len(wa & wb) / len(wa | wb) >= threshold


def clean_text(text):
    text = re.sub(r"\s+", " ", (text or "")).strip().lstrip("-*\u2022 ").strip()
    return text.replace(PIN, "").strip()


def is_safe(text):
    return not SECRET.search(text or "")


class MemoryFile:
    """The memories of one Markdown file. Every call re-reads the file, so edits made elsewhere are never lost."""

    def __init__(self, path):
        self.path = Path(path)

    def _lines(self):
        try:
            return self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []

    def _write(self, lines):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines).rstrip("\n") + "\n" if lines else "", encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _format(text, pinned):
        return "- " + (PIN + " " if pinned else "") + clean_text(text)

    def items(self):
        out = []
        for i, line in enumerate(self._lines()):
            m = BULLET.match(line)
            if m:
                out.append({"line": i, "text": m.group(2).strip(), "pinned": bool(m.group(1))})
        return out

    def raw(self):
        return "\n".join(self._lines())

    def _locate(self, lines, item):
        """Line number of an item, even if the file changed since the list was drawn."""
        i = item.get("line", -1)
        for cand in ([i] if 0 <= i < len(lines) else []) + list(range(len(lines))):
            m = BULLET.match(lines[cand])
            if m and m.group(2).strip() == item["text"]:
                return cand
        raise KeyError("That memory has changed or was removed - refresh the list.")

    def add(self, text, pinned=False, skip_similar=True):
        """-> (True, item) when added, (False, existing_item) when a similar memory already exists."""
        text = clean_text(text)
        if not text:
            raise ValueError("Empty memory.")
        if skip_similar:
            for it in self.items():
                if similar(text, it["text"]):
                    return False, it
        lines = self._lines()
        lines.append(self._format(text, pinned))
        self._write(lines)
        return True, {"line": len(lines) - 1, "text": text, "pinned": pinned}

    def update(self, item, new_text, pinned=None):
        lines = self._lines()
        i = self._locate(lines, item)
        keep = item.get("pinned", False) if pinned is None else pinned
        lines[i] = self._format(new_text, keep)
        self._write(lines)

    def delete(self, item):
        lines = self._lines()
        del lines[self._locate(lines, item)]
        self._write(lines)

    def set_pinned(self, item, pinned):
        self.update(item, item["text"], pinned)


def select_for_prompt(text, query, budget=2500):
    """Return (text_for_the_prompt, number_left_out).  Short memories are sent whole; long ones are reduced to the
    pinned lines plus the lines that match the question, newest first, up to the budget."""
    if len(text) <= budget:
        return text, 0
    lines = text.splitlines()
    others = [l for l in lines if l.strip() and not BULLET.match(l)]
    head = "\n".join(others)[: budget // 4]
    bullets = [(i, l, BULLET.match(l)) for i, l in enumerate(lines) if BULLET.match(l)]
    q = words(query)
    scored = []
    for rank, (i, l, m) in enumerate(bullets):
        score = (1000 if m.group(1) else 0) + 10 * len(q & words(m.group(2))) + rank / max(1, len(bullets))
        scored.append((score, i, l))
    used, chosen = len(head) + 1, []
    for score, i, l in sorted(scored, reverse=True):
        if used + len(l) + 1 <= budget:
            chosen.append((i, l))
            used += len(l) + 1
    chosen.sort()
    left = len(bullets) - len(chosen)
    out = ([head] if head else []) + [l for _, l in chosen]
    if left:
        out.append(f"(+{left} more memories are not shown here; they are used when they match the question.)")
    return "\n".join(out), left


def remember_command(text):
    """'remember that I prefer short answers' -> (memory_text, scope) with scope 'global', 'workspace' or None."""
    m = REMEMBER_CMD.match(text or "")
    if not m or text.lstrip().startswith("/"):
        return None
    body = clean_text(m.group(1))
    if len(body) < 4:
        return None
    scope = "global" if GLOBAL_WORDS.search(body) else ("workspace" if WORKSPACE_WORDS.search(body) else None)
    return body, scope


def looks_memorable(user_text, min_chars=30):
    t = (user_text or "").strip()
    return len(t) >= min_chars and not t.startswith("/") and bool(SIGNALS.search(t))


EXTRACT_SYSTEM = ("You decide what a personal AI assistant should remember about its user between conversations. "
                  "Answer with one JSON object and nothing else. Do not use any file or script tags.")
EXTRACT_USER = """Workspace: {workspace}

Already remembered (do not repeat these or say the same in other words):
GLOBAL:
{global_items}
WORKSPACE:
{workspace_items}

The latest exchange:
USER: {user}
ASSISTANT: {assistant}

Return JSON: {{"memories": [{{"text": "...", "scope": "global" or "workspace"}}]}}
Rules:
- At most 3 memories; an empty list is the normal answer when nothing is worth keeping.
- Keep only durable facts: who the user is, their field, tools, languages, preferences about how answers should look,
  decisions and facts about a project. NOT one-off tasks, questions, or things only true right now.
- scope "global" = true in every conversation; "workspace" = only for this project/workspace.
- One short sentence each, in the language the user wrote in, written so it still makes sense later
  (for example "The user prefers cover letters in English unless the posting is in German.").
- Never keep passwords, API keys, tokens, bank or ID numbers, or sensitive health details."""


def extraction_prompt(workspace, global_items, workspace_items, user, assistant):
    def block(items):
        return "\n".join("- " + t for t in items[-40:]) or "(nothing yet)"
    return EXTRACT_USER.format(workspace=workspace, global_items=block(global_items),
                               workspace_items=block(workspace_items), user=(user or "")[:1500],
                               assistant=(assistant or "")[:1500])


def parse_extraction(answer, existing=()):
    """Model answer -> [{'text', 'scope'}], safe, short, not duplicated."""
    answer = re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S | re.I)
    data = None
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\{\[]", answer):
        try:
            data, _ = dec.raw_decode(answer[m.start():])
            break
        except ValueError:
            continue
    items = data.get("memories") if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        text = clean_text(it.get("text") if isinstance(it, dict) else str(it))
        scope = (it.get("scope") if isinstance(it, dict) else "") or "workspace"
        if not (8 <= len(text) <= 240) or not is_safe(text):
            continue
        if any(similar(text, e) for e in list(existing) + [o["text"] for o in out]):
            continue
        out.append({"text": text, "scope": "global" if str(scope).lower().startswith("g") else "workspace"})
    return out[:3]
