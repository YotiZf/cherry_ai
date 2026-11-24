"""Cerry writing-style engine - no GUI.  Learns how YOU write from a folder of your own documents.

Put your cover letters, motivation letters, CVs, emails or articles (.txt .md .docx .pdf .tex) in one folder - the
"writing training folder".  Cerry
  1. reads them (recursively, up to 60 files), tells letters from CVs by file name and content, and German from English;
  2. asks a model once to describe your style (tone, openings/closings, structure, sentence length, typical phrases,
     what you never write) - the result is a short guide that you can read AND EDIT;
  3. for every new CV / cover letter adds that guide plus 1-3 of your own letters as examples (the most similar ones to
     the job) to the instructions.  The examples teach the VOICE only: facts, names and sentences are never reused.
The guide is rebuilt when the folder changes - unless you edited it by hand (then only "Study folder now" rebuilds it).
"""
import hashlib
import json
import re
import time
from itertools import islice
from pathlib import Path

if __package__:
    from .file_scan import iter_files
else:
    from file_scan import iter_files

SAMPLE_EXT = {".txt", ".md", ".docx", ".pdf", ".tex", ".rtf"}
MAX_FILES = 60
MAX_CHARS = 7000
LETTER_WORDS = ("cover", "letter", "anschreiben", "motivation", "bewerbung", "lettre", "lettera", "statement", "brief")
CV_WORDS = ("cv", "lebenslauf", "resume", "curriculum", "vita")
DE_WORDS = set("der die das und ist nicht ich sie wir ein eine mit für auf von den dem des zu im es aber auch wie wenn oder "
               "noch nur bei aus nach über dass kann wird sind haben hat ihr sein werden sich sehr geehrte freundlichen grüßen".split())
EN_WORDS = set("the and is are of to that it for with as this be not on you have but or at from can will your they their "
               "which dear sincerely regards".split())
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\w)(\+?\d[\d ()/.-]{7,}\d)(?!\w)")
URL = re.compile(r"https?://\S+")
KEYS = ("letter_en", "letter_de", "cv", "avoid", "phrases")


def read_sample(path):
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".txt", ".md", ".rtf"):
        text = p.read_text(encoding="utf-8", errors="ignore")
    elif ext == ".tex":
        text = p.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"(?m)(?<!\\)%.*$", "", text)
        text = re.sub(r"\\(?:begin|end|documentclass|usepackage|setlength|renewcommand|newcommand|input|include)\*?(?:\[[^\]]*\])*(?:\{[^}]*\})*", " ", text)
        text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text).replace("{", " ").replace("}", " ")
    elif ext == ".docx":
        from docx import Document
        doc = Document(str(p))
        text = "\n".join(par.text for par in doc.paragraphs)
    elif ext == ".pdf":
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:6])
    else:
        text = ""
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()[:MAX_CHARS]


def detect_language(text):
    words = re.findall(r"[a-zäöüß]+", text.lower())
    de = sum(w in DE_WORDS for w in words) + 3 * len(re.findall(r"[äöüß]", text.lower()))
    en = sum(w in EN_WORDS for w in words)
    return "de" if de > en else "en"


def classify(name, text):
    low = name.lower()
    if any(w in low for w in CV_WORDS):
        return "cv"
    if any(w in low for w in LETTER_WORDS):
        return "letter"
    head = text[:600].lower()
    if re.search(r"\b(dear|sehr geehrte|madame|messieurs|to whom)\b", head):
        return "letter"
    if re.search(r"\b(education|experience|publications|skills|berufserfahrung|ausbildung)\b", head):
        return "cv"
    return "other"


def candidate_files(folder):
    visible = iter_files(folder, lambda name: name.startswith("."))
    return list(islice((path for path in visible if path.suffix.lower() in SAMPLE_EXT), MAX_FILES))


def fingerprint(folder):
    h = hashlib.sha1()
    for p in candidate_files(folder):
        try:
            st = p.stat()
            h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()


def scan_folder(folder):
    """-> list of samples {name, path, kind, lang, text}."""
    samples = []
    for p in candidate_files(folder):
        try:
            text = read_sample(p)
        except Exception:
            continue
        if len(text) < 200:
            continue
        samples.append({"name": p.name, "path": str(p), "kind": classify(p.name, text), "lang": detect_language(text), "text": text})
    return samples


def scrub(text):
    """Contact data of the examples is replaced, so it cannot slip into a new letter."""
    return PHONE.sub("[phone]", EMAIL.sub("[email]", URL.sub("[link]", text)))


STYLE_SYSTEM = ("You analyse how one person writes and describe their style so that another writer can imitate it. "
                "Answer with one JSON object and nothing else. Do not use any <cerry_file> or <cerry_run> tags.")
STYLE_USER = """Below are documents written by the same person. Describe THEIR writing style.

{blocks}

Return JSON with exactly these keys (plain text values, no markdown):
{{"letter_en": "style guide for English cover/motivation letters, or empty if there are no English letters",
"letter_de": "style guide for German letters (Anschreiben), or empty if there are none",
"cv": "style guide for the wording of their CV: how bullets are phrased, tense, level of detail, order, length",
"avoid": "things this person never does or that would sound wrong for them (clichés, phrases, tone)",
"phrases": "short phrases, openings and closings they really use, one per line"}}
Each guide: at most 200 words, concrete and checkable (tone, formality, typical opening and closing, paragraph structure
and length, sentence length, vocabulary, first person, how achievements are presented, punctuation habits).
Never include personal facts such as names, employers, projects or numbers."""


def build_profile(samples, ask, folder=""):
    """samples -> profile dict.  ask(system, user) -> model answer text."""
    order = [("letter", "en"), ("letter", "de"), ("cv", "en"), ("cv", "de"), ("other", "en"), ("other", "de")]
    blocks, used, budget = [], [], 15000
    for kind, lang in order:
        group = [s for s in samples if s["kind"] == kind and s["lang"] == lang][:5]
        if not group or budget <= 0:
            continue
        per = max(900, min(3000, budget // (len(group) + 1)))
        title = {"letter": "LETTERS", "cv": "CVS", "other": "OTHER TEXTS"}[kind] + (" (English)" if lang == "en" else " (German)")
        body = "\n\n".join(f"--- {s['name']} ---\n{s['text'][:per]}" for s in group)
        blocks.append(f"=== {title} ===\n{body}")
        used += [s["name"] for s in group]
        budget -= sum(min(len(s["text"]), per) for s in group)
    if not blocks:
        raise ValueError("No usable documents in the folder (need .txt .md .docx .pdf .tex files with some text).")
    answer = re.sub(r"<think>.*?</think>", "", ask(STYLE_SYSTEM, STYLE_USER.format(blocks="\n\n".join(blocks))), flags=re.S | re.I)
    data, dec = None, json.JSONDecoder()
    for m in re.finditer(r"\{", answer):
        try:
            data, _ = dec.raw_decode(answer[m.start():])
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        raise ValueError("The model did not return a style guide. Try again or choose another model.")
    prof = {k: (data.get(k) if isinstance(data.get(k), str) else "\n".join(map(str, data.get(k) or []))).strip() for k in KEYS}
    prof.update(fingerprint=fingerprint(folder) if folder else "", files=used, built=time.strftime("%Y-%m-%d %H:%M"),
                counts={"letters": sum(s["kind"] == "letter" for s in samples), "cvs": sum(s["kind"] == "cv" for s in samples),
                        "other": sum(s["kind"] == "other" for s in samples)}, edited=False)
    return prof


def pick_examples(samples, kind, lang, query, n=2, chars=1800):
    """The n documents of this kind most similar to the job text; same language first."""
    pool = [s for s in samples if s["kind"] == kind] or []
    same = [s for s in pool if s["lang"] == lang]
    pool = same or pool
    q = set(w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 3)
    scored = sorted(pool, key=lambda s: -len(q & set(re.findall(r"\w+", s["text"].lower()))))
    return [dict(s, text=scrub(s["text"][:chars])) for s in scored[: max(0, n)]]


def prompt_block(profile, kind, lang, examples=()):
    """The text added to the instructions for a CV (kind 'cv') or a letter (kind 'letter'). lang: 'de' or 'en'."""
    if kind == "cv":
        guide = profile.get("cv", "")
    else:
        guide = profile.get("letter_de" if lang == "de" else "letter_en", "") or profile.get("letter_en", "") or profile.get("letter_de", "")
    parts = ["WRITING STYLE OF THE CANDIDATE - write exactly like this person (it was learned from their own documents):"]
    parts.append(guide or "(no guide available - use a natural, plain, professional style)")
    if profile.get("phrases", "").strip():
        parts.append("Phrases the candidate really uses (use them only where they fit naturally):\n" + profile["phrases"].strip())
    if profile.get("avoid", "").strip():
        parts.append("Never do this:\n" + profile["avoid"].strip())
    if examples:
        parts.append("EXAMPLES of the candidate's own " + ("CVs" if kind == "cv" else "letters") +
                     ". They show the VOICE only. Never copy their facts, employers, names, dates or whole sentences:")
        parts += [f"--- example {i} ({e['name']}) ---\n{e['text']}" for i, e in enumerate(examples, 1)]
    parts.append("Facts about the candidate still come ONLY from the CV text given above.")
    return "\n\n".join(parts)


WRITING_REQUEST = re.compile(r"\b(cover letter|motivation letter|anschreiben|bewerbung|letter|e-?mail|mail|write|draft|"
                             r"schreib\w*|formulier\w*|verfass\w*|reply|antwort)\b", re.I)
