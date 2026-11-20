"""Cerry tuning engine - no GUI.  Thinking level, temperature and study depth.

THINKING   Default | Fast | Think | Deep
           Every level adds a short instruction to the system prompt.  If you allow it, the level is also sent to
           providers that understand it as "reasoning_effort" (Fast=low, Think=medium, Deep=high).
TEMPERATURE  Auto (the model's own default) or a number 0.0 - 2.0.  Low = precise and repeatable, high = more varied.
           Both settings are only sent with normal chat requests.  A provider or model that rejects one of them is
           remembered (ai_workspace_data/tuning_unsupported.json) and then simply not sent that setting again.
STUDY DEPTH  Quick | Standard | Deep | Thorough  - how much of your documents is read to answer a question:
           Quick     3 best passages
           Standard  6 best passages (the old behaviour)
           Deep      12 best passages, each with the text before and after it
           Thorough  Deep, plus: the most relevant files are read COMPLETELY, section by section, and the model takes
                     notes about your question first; the notes are added to the question (many more model calls)
"""
import json
import re
import threading
from pathlib import Path

THINK_LEVELS = ["Default", "Fast", "Think", "Deep"]
THINK_NOTES = {
    "Default": "",
    "Fast": "Answer directly and concisely. Do not think step by step unless it is really needed. If you support /no_think, use /no_think.",
    "Think": ("Check assumptions and verify your answer carefully before you give it. Give a concise explanation of the "
              "conclusion, not your private internal reasoning."),
    "Deep": ("Take your time. Break the problem into steps, check each step, consider alternative explanations and "
             "possible mistakes, and verify the result before you answer. Then give a clear final answer."),
}
EFFORT = {"Fast": "low", "Think": "medium", "Deep": "high"}
DEPTH_LEVELS = ["Quick", "Standard", "Deep", "Thorough"]
DEPTH = {"Quick": {"k": 3, "expand": False}, "Standard": {"k": 6, "expand": False},
         "Deep": {"k": 12, "expand": True}, "Thorough": {"k": 12, "expand": True}}
DEFAULTS = {"thinking": "Default", "temperature": "Auto", "temp_writing": 0.7, "send_effort": True,
            "effort_providers": ["gemini", "groq"], "depth": "Standard", "thorough_files": 5, "thorough_sections": 24,
            "section_chars": 7000}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_temperature(value):
    """'Auto' -> None, '0.7' -> 0.7, junk -> None."""
    try:
        t = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return max(0.0, min(2.0, t))


class Tuner:
    """Decides which settings go into a request. Thread-safe enough for our use (small dict reads)."""

    def __init__(self, cfg, learned_path, base_prefix):
        self.cfg, self.learned_path, self.base_prefix = cfg, Path(learned_path), base_prefix
        self.learned = set(tuple(x) for x in load_json(self.learned_path, []))
        self.context = threading.local()                  # context.kind == "writing" while a letter/CV is written

    def kind_of(self, kwargs):
        if getattr(self.context, "kind", None):
            return self.context.kind
        msgs = kwargs.get("messages") or []
        if msgs and msgs[0].get("role") == "system" and str(msgs[0].get("content", "")).startswith(self.base_prefix):
            return "chat"
        return None

    def prepare(self, provider, kwargs):
        """-> (kwargs_with_prompt_changes, extras).  extras = parameters that may be refused (temperature, effort)."""
        kind = self.kind_of(kwargs)
        extras = {}
        if kind is None or "temperature" in kwargs:
            return kwargs, extras
        model = kwargs.get("model", "")
        level = self.cfg.get("thinking", "Default") if kind == "chat" else "Default"
        note = THINK_NOTES.get(level, "")
        if note and kwargs.get("messages"):
            msgs = [dict(m) for m in kwargs["messages"]]
            msgs[0]["content"] = str(msgs[0]["content"]) + "\n\n" + note
            kwargs = dict(kwargs, messages=msgs)
        temp = parse_temperature(self.cfg["temp_writing"]) if kind == "writing" else parse_temperature(self.cfg.get("temperature", "Auto"))
        if temp is not None and (provider, model, "temperature") not in self.learned:
            extras["temperature"] = temp
        if (kind == "chat" and level in EFFORT and self.cfg.get("send_effort", True)
                and provider in self.cfg.get("effort_providers", []) and (provider, model, "reasoning_effort") not in self.learned):
            extras["reasoning_effort"] = EFFORT[level]
        return kwargs, extras

    @staticmethod
    def is_param_error(exc):
        code = getattr(exc, "status_code", None)
        text = str(exc).lower()
        if code in (400, 422):
            return True
        return any(w in text for w in ("unsupported", "not supported", "unknown parameter", "unrecognized", "temperature",
                                       "reasoning_effort", "invalid_request"))

    def learn(self, provider, model, names):
        for n in names:
            self.learned.add((provider, model, n))
        try:
            self.learned_path.write_text(json.dumps(sorted(self.learned)), encoding="utf-8")
        except OSError:
            pass

    def call(self, create, provider, args, kwargs):
        """Run create(...) with our settings; drop settings the provider refuses (one at a time) and remember that."""
        base, extras = self.prepare(provider, kwargs)
        if not extras:
            return create(*args, **base)
        order = [dict(extras)]
        if "reasoning_effort" in extras:
            order.append({k: v for k, v in extras.items() if k != "reasoning_effort"})
        order.append({})
        last = None
        for attempt, use in enumerate(order):
            try:
                return create(*args, **base, **use)
            except Exception as e:                       # noqa
                last = e
                if attempt == len(order) - 1 or not self.is_param_error(e):
                    raise
                dropped = [k for k in extras if k not in order[attempt + 1]] if attempt + 1 < len(order) else list(extras)
                self.learn(provider, kwargs.get("model", ""), dropped)
        raise last


# ---------------------------------------------------------------------- study depth
def expand_hits(index, hits):
    """Give every hit the chunk before and after it (same file), so a passage is not cut in the middle."""
    chunks = index.chunks
    pos = getattr(index, "_depth_pos", None)
    if pos is None or getattr(index, "_depth_pos_for", None) is not chunks:
        pos = {(c[0], c[3]): i for i, c in enumerate(chunks)}
        index._depth_pos, index._depth_pos_for = pos, chunks
    out = []
    for h in hits:
        i = pos.get((h["key"], h["text"]))
        if i is None:
            out.append(h)
            continue
        parts = []
        if i > 0 and chunks[i - 1][0] == h["key"]:
            parts.append(chunks[i - 1][3])
        parts.append(h["text"])
        if i + 1 < len(chunks) and chunks[i + 1][0] == h["key"]:
            parts.append(chunks[i + 1][3])
        out.append(dict(h, text="\n[...]\n".join(parts)))
    return out


def split_sections(text, size=7000):
    """Cut a long text into pieces of about `size` characters at paragraph / line / sentence borders."""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    out, cur = [], ""
    for para in re.split(r"(?<=\n\n)|(?<=[.!?])\s+(?=[A-Z0-9])", text):
        if len(cur) + len(para) > size and cur:
            out.append(cur.strip())
            cur = ""
        while len(para) > size:                          # a single huge paragraph
            out.append(para[:size])
            para = para[size:]
        cur += para + " "
    if cur.strip():
        out.append(cur.strip())
    return out


STUDY_SYSTEM = ("You read one section of a document and take notes for a question. Reply with notes only. "
                "Do not use any <cerry_file> or <cerry_run> tags.")
STUDY_USER = """Question: {question}

Document: {name}   (part {i} of {n})
--- section start ---
{section}
--- section end ---

Write short bullet notes with EVERYTHING in this section that helps to answer the question: facts, numbers, names, dates,
definitions, steps, and where it is (page, heading) if visible. Quote exact figures. Do not answer the question yet and do not
invent anything. If nothing in this section is relevant, reply with exactly: NONE"""


def rank_files(index, question, count):
    """The `count` most relevant files for a question: [(key, path, name)]."""
    hits = index.search(question, 60)
    score = {}
    for h in hits:
        score[h["key"]] = score.get(h["key"], 0.0) + h["score"]
    paths = {r.key: r.path for r in index.records}
    ranked = sorted(score, key=lambda k: -score[k])
    return [(k, paths[k], paths[k].name) for k in ranked if k in paths][:count]


def study(question, files, read_text, ask, size, max_sections, progress, should_stop):
    """Read whole files section by section and collect notes.  ask(system, user) -> text.  -> notes text."""
    plan = []
    for key, path, name in files:
        sections = split_sections(read_text(path), size)
        plan += [(name, i + 1, len(sections), s) for i, s in enumerate(sections)]
    if len(plan) > max_sections:                         # spread the budget over the files
        step = len(plan) / max_sections
        plan = [plan[int(i * step)] for i in range(max_sections)]
    notes = []
    for n, (name, i, total, section) in enumerate(plan, 1):
        if should_stop():
            raise InterruptedError()
        progress(f"Studying {name}: part {i}/{total}  ({n}/{len(plan)} sections)")
        answer = ask(STUDY_SYSTEM, STUDY_USER.format(question=question, name=name, i=i, n=total, section=section)).strip()
        if answer and answer.upper().strip(". ") != "NONE":
            notes.append(f"### {name} (part {i}/{total})\n{answer}")
    return "\n\n".join(notes)
