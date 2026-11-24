"""Cerry file engine - no GUI.  Creates files (Python, text, Word, PDF, Excel ...) and edits existing PDF / Word files.

The model asks for files with two tags in its answer (the user never sees them as tags - Cerry replaces them with a
code box and creates the files in  workspaces/<name>/outputs/ ):

  <cerry_make name="report.docx"> ...Markdown or file content... </cerry_make>        a NEW file
  <cerry_edit source="old.pdf" output="new.pdf"> [ {"op": ...}, ... ] </cerry_edit>   a CHANGED COPY of a workspace file

Editing never touches the original: the result is always a new file.  Operations are a fixed list (no free-form code runs):
  PDF   replace_text, highlight_text (need: pip install pymupdf) | delete_pages, keep_pages, rotate_pages, reorder_pages,
        add_text, watermark, merge, metadata (pypdf + reportlab)
  Word  replace_text, insert_after, delete_paragraph, append_paragraph (python-docx)
"""
import copy
import html
import io
import json
import os
import re
import shutil
import time
from pathlib import Path

if __package__:
    from .file_scan import iter_files
else:
    from file_scan import iter_files

TEXT_EXT = {".py", ".txt", ".md", ".csv", ".tsv", ".json", ".html", ".yaml", ".yml", ".tex", ".bib", ".r", ".sql", ".js", ".css", ".xml", ".ini", ".sh", ".bat", ".ps1", ".ipynb"}
MAKE_EXT = TEXT_EXT | {".docx", ".pdf", ".xlsx"}
EDIT_EXT = {".pdf", ".docx"}
MAKE_RE = re.compile(r"<cerry_make\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>\s*?\n?(.*?)</cerry_make>", re.S | re.I)
EDIT_RE = re.compile(r"<cerry_edit\s+source\s*=\s*[\"']([^\"']+)[\"']\s+output\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</cerry_edit>", re.S | re.I)


def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'<i>\1</i>', s)
    s = re.sub(r'`(.+?)`', r'<font face="Courier">\1</font>', s)
    return s


def _pdf_fonts():
    try:
        import matplotlib
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        d = Path(matplotlib.get_data_path()) / 'fonts' / 'ttf'
        reg, bold = d / 'DejaVuSans.ttf', d / 'DejaVuSans-Bold.ttf'
        ital = d / 'DejaVuSans-Oblique.ttf'
        bi = d / 'DejaVuSans-BoldOblique.ttf'
        if reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont('DejaVu', str(reg)))
            pdfmetrics.registerFont(TTFont('DejaVu-Bold', str(bold)))
            pdfmetrics.registerFont(TTFont('DejaVu-Italic', str(ital if ital.exists() else reg)))
            pdfmetrics.registerFont(TTFont('DejaVu-BoldItalic', str(bi if bi.exists() else bold)))
            pdfmetrics.registerFontFamily('DejaVu', normal='DejaVu', bold='DejaVu-Bold',
                                          italic='DejaVu-Italic', boldItalic='DejaVu-BoldItalic')
            return 'DejaVu', 'DejaVu-Bold'
    except Exception:
        pass
    return 'Helvetica', 'Helvetica-Bold'


def _table_rows(lines):
    rows = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def make_pdf(markdown, path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle, XPreformatted)
    body, head = _pdf_fonts()
    st = {
        'p': ParagraphStyle('p', fontName=body, fontSize=10.5, leading=15, spaceAfter=5),
        'h1': ParagraphStyle('h1', fontName=head, fontSize=18, leading=22, spaceBefore=8, spaceAfter=8),
        'h2': ParagraphStyle('h2', fontName=head, fontSize=14, leading=18, spaceBefore=8, spaceAfter=6),
        'h3': ParagraphStyle('h3', fontName=head, fontSize=12, leading=16, spaceBefore=6, spaceAfter=4),
        'li': ParagraphStyle('li', fontName=body, fontSize=10.5, leading=15, leftIndent=16, bulletIndent=4, spaceAfter=2),
        'code': ParagraphStyle('code', fontName='Courier', fontSize=9, leading=11, leftIndent=8, spaceAfter=6,
                               backColor=colors.whitesmoke),
    }
    flow, lines, i = [], markdown.replace('\r\n', '\n').split('\n'), 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith('```'):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            flow.append(XPreformatted(html.escape('\n'.join(buf), quote=False), st['code']))
        elif s.startswith('|'):
            buf = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                buf.append(lines[i])
                i += 1
            i -= 1
            rows = _table_rows(buf)
            if rows:
                width = max(len(r) for r in rows)
                data = [[Paragraph(_inline(c), st['p']) for c in r + [''] * (width - len(r))] for r in rows]
                t = Table(data, repeatRows=1)
                t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                                       ('BACKGROUND', (0, 0), (-1, 0), colors.whitesmoke),
                                       ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
                flow += [t, Spacer(1, 8)]
        elif not s:
            flow.append(Spacer(1, 4))
        elif s.startswith('### '):
            flow.append(Paragraph(_inline(s[4:]), st['h3']))
        elif s.startswith('## '):
            flow.append(Paragraph(_inline(s[3:]), st['h2']))
        elif s.startswith('# '):
            flow.append(Paragraph(_inline(s[2:]), st['h1']))
        elif re.match(r'^[-*] ', s):
            flow.append(Paragraph(_inline(s[2:]), st['li'], bulletText='\u2022'))
        elif re.match(r'^\d+[.)] ', s):
            num, rest = re.split(r'[.)] ', s, 1)
            flow.append(Paragraph(_inline(rest), st['li'], bulletText=num + '.'))
        else:
            flow.append(Paragraph(_inline(s), st['p']))
        i += 1
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    doc.build(flow or [Paragraph(' ', st['p'])])


def make_docx(markdown, path):
    from docx import Document
    doc = Document()

    def add_runs(par, text):
        for k, part in enumerate(re.split(r'\*\*(.+?)\*\*', text)):
            if part:
                par.add_run(part).bold = (k % 2 == 1)

    lines, i = markdown.replace('\r\n', '\n').split('\n'), 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith('```'):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            p = doc.add_paragraph()
            r = p.add_run('\n'.join(buf))
            r.font.name = 'Consolas'
        elif s.startswith('|'):
            buf = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                buf.append(lines[i])
                i += 1
            i -= 1
            rows = _table_rows(buf)
            if rows:
                width = max(len(r) for r in rows)
                t = doc.add_table(rows=len(rows), cols=width)
                t.style = 'Table Grid'
                for a, r in enumerate(rows):
                    for b, c in enumerate(r):
                        t.cell(a, b).text = c
        elif s.startswith('### '):
            doc.add_heading(s[4:], level=3)
        elif s.startswith('## '):
            doc.add_heading(s[3:], level=2)
        elif s.startswith('# '):
            doc.add_heading(s[2:], level=1)
        elif re.match(r'^[-*] ', s):
            add_runs(doc.add_paragraph(style='List Bullet'), s[2:])
        elif re.match(r'^\d+[.)] ', s):
            add_runs(doc.add_paragraph(style='List Number'), re.split(r'[.)] ', s, 1)[1])
        elif s:
            add_runs(doc.add_paragraph(), s)
        i += 1
    doc.save(str(path))




# ---------------------------------------------------------------------- reading the model's answer
def strip_fence(text):
    t = text.strip("\n")
    m = re.match(r"^\s*```[\w+.-]*[ \t]*\n(.*?)\n?```\s*$", t, re.S)
    return m.group(1) if m else t


def parse_actions(text):
    """-> list of {'kind': 'make'|'edit', ...} in the order they appear."""
    acts = []
    for m in MAKE_RE.finditer(text or ""):
        acts.append({"kind": "make", "name": m.group(1).strip(), "content": strip_fence(m.group(2)), "pos": m.start(), "end": m.end()})
    for m in EDIT_RE.finditer(text or ""):
        acts.append({"kind": "edit", "source": m.group(1).strip(), "name": m.group(2).strip(), "ops_text": strip_fence(m.group(3)),
                     "pos": m.start(), "end": m.end()})
    acts.sort(key=lambda a: a["pos"])
    return acts[:6]


def display_text(text, acts):
    """The answer as the user sees it: the tags become readable boxes."""
    out, last = [], 0
    for a in acts:
        out.append(text[last:a["pos"]])
        if a["kind"] == "make":
            lang = Path(a["name"]).suffix.lstrip(".").lower()
            lang = "markdown" if lang in ("docx", "pdf") else ("text" if lang in ("txt", "") else lang)
            out.append(f"\n**File: {a['name']}**\n```{lang}\n{a['content']}\n```\n")
        else:
            out.append(f"\n**Edit: {a['source']} \u2192 {a['name']}**\n```json\n{a['ops_text']}\n```\n")
        last = a["end"]
    out.append(text[last:])
    return "".join(out)


def safe_name(name, allowed):
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = re.sub(r"[^\w.\- ()]", "_", base).strip(" .")
    if not base or Path(base).suffix.lower() not in allowed:
        raise ValueError(f"File type not allowed: {name!r}. Allowed: " + ", ".join(sorted(allowed)))
    return base


def unique_path(folder, name):
    p = Path(folder) / name
    n = 2
    while p.exists():
        p = Path(folder) / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return p


def find_source(name, folders):
    """A workspace file by (file) name: exact name first, then case-insensitive, searched in the given folders."""
    wanted = Path(str(name).replace("\\", "/")).name.lower()
    best = None
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for p in folder.rglob("*"):
            if p.is_file() and p.name.lower() == wanted:
                if p.name == Path(name).name:
                    return p
                best = best or p
    return best


# ---------------------------------------------------------------------- creating files
def make_xlsx(csv_text, path):
    import csv
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in csv.reader(io.StringIO(csv_text)):
        ws.append(row)
    wb.save(str(path))


def make_file(folder, name, content):
    """Create a new file from text. Returns the path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = unique_path(folder, safe_name(name, MAKE_EXT))
    ext = target.suffix.lower()
    if ext == ".pdf":
        make_pdf(content, target)
    elif ext == ".docx":
        make_docx(content, target)
    elif ext == ".xlsx":
        make_xlsx(content, target)
    else:
        target.write_text(content, encoding="utf-8", newline="\n")
    return target


# ---------------------------------------------------------------------- editing PDF files
def _pages(spec, total):
    out = []
    for p in spec or []:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total:
            out.append(n)
    return out


def _need_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        raise RuntimeError("This edit needs the package PyMuPDF: pip install pymupdf")


def _fitz_text_edit(src, dst, op):
    fitz = _need_fitz()
    doc = fitz.open(str(src))
    find, repl = str(op.get("find", "")), op.get("replace")
    if not find:
        raise ValueError("'find' is empty")
    pages = _pages(op.get("pages"), len(doc)) or list(range(1, len(doc) + 1))
    count = 0
    for n in pages:
        page = doc[n - 1]
        rects = page.search_for(find)
        if not rects:
            continue
        if op["op"] == "highlight_text":
            for r in rects:
                page.add_highlight_annot(r)
            count += len(rects)
            continue
        size = 11.0
        try:
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for sp in line["spans"]:
                        if fitz.Rect(sp["bbox"]).intersects(rects[0]):
                            size = float(sp["size"])
        except Exception:
            pass
        for r in rects:
            page.add_redact_annot(r, text=str(repl or ""), fontname="helv", fontsize=size, fill=(1, 1, 1), text_color=(0, 0, 0))
        page.apply_redactions()
        count += len(rects)
    doc.save(str(dst), garbage=3, deflate=True)
    doc.close()
    return count


def _overlay(width, height, draw):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    draw(c)
    c.save()
    buf.seek(0)
    return buf


def edit_pdf(src, dst, ops, log):
    """Apply the operations one after the other; the original file is never changed. Returns the number of changes."""
    from pypdf import PdfReader, PdfWriter
    work = Path(dst).with_suffix(".work1.pdf")
    shutil.copy2(src, work)
    changes = 0
    try:
        for n, op in enumerate(ops, 1):
            kind = str(op.get("op", "")).lower()
            nxt = Path(dst).with_suffix(f".work{n + 1}.pdf")
            if kind in ("replace_text", "highlight_text"):
                op = dict(op, op=kind)
                c = _fitz_text_edit(work, nxt, op)
                log(f"  {kind} '{str(op.get('find'))[:40]}': {c} place(s)")
                changes += c
                work.unlink(missing_ok=True)
                work = nxt
                continue
            reader = PdfReader(str(work))
            total = len(reader.pages)
            writer = PdfWriter()
            pages = list(reader.pages)
            if kind == "delete_pages":
                drop = set(_pages(op.get("pages"), total))
                keep = [p for i, p in enumerate(pages, 1) if i not in drop]
                if not keep:
                    raise ValueError("this would delete every page")
                pages, c = keep, len(drop)
            elif kind == "keep_pages":
                sel = _pages(op.get("pages"), total)
                if not sel:
                    raise ValueError("no valid page numbers")
                pages, c = [pages[i - 1] for i in sel], len(sel)
            elif kind == "reorder_pages":
                sel = _pages(op.get("order") or op.get("pages"), total)
                if not sel:
                    raise ValueError("no valid page order")
                pages, c = [pages[i - 1] for i in sel], len(sel)
            elif kind == "rotate_pages":
                angle = int(op.get("angle", 90))
                sel = _pages(op.get("pages"), total) or list(range(1, total + 1))
                for i in sel:
                    pages[i - 1].rotate(angle)
                c = len(sel)
            elif kind in ("add_text", "watermark"):
                text = str(op.get("text", ""))
                sel = _pages(op.get("pages") or ([op["page"]] if "page" in op else None), total) or list(range(1, total + 1))
                for i in sel:
                    page = pages[i - 1]
                    w, h = float(page.mediabox.width), float(page.mediabox.height)

                    def draw(cv, w=w, h=h):
                        if kind == "watermark":
                            cv.saveState()
                            cv.setFillGray(0.6, 0.35)
                            cv.setFont("Helvetica-Bold", float(op.get("size", 60)))
                            cv.translate(w / 2, h / 2)
                            cv.rotate(45)
                            cv.drawCentredString(0, 0, text)
                            cv.restoreState()
                        else:
                            cv.setFont("Helvetica", float(op.get("size", 12)))
                            cv.drawString(float(op.get("x", 72)), h - float(op.get("y", 72)), text)
                    page.merge_page(PdfReader(_overlay(w, h, draw)).pages[0])
                c = len(sel)
            elif kind == "merge":
                c = 0
                for other in op.get("_paths", []):
                    pages += list(PdfReader(str(other)).pages)
                    c += 1
                if not c:
                    raise ValueError("no PDF file to merge was found in the workspace")
            elif kind == "metadata":
                for p in pages:
                    pass
                c = 1
            else:
                raise ValueError(f"unknown PDF operation '{kind}'")
            for p in pages:
                writer.add_page(p)
            if kind == "metadata":
                writer.add_metadata({"/" + k.capitalize(): str(v) for k, v in op.items() if k in ("title", "author", "subject")})
            with open(nxt, "wb") as f:
                writer.write(f)
            log(f"  {kind}: {c} change(s)")
            changes += c
            work.unlink(missing_ok=True)
            work = nxt
        shutil.move(str(work), str(dst))
    finally:
        for p in Path(dst).parent.glob(Path(dst).stem + ".work*.pdf"):
            p.unlink(missing_ok=True)
    return changes


# ---------------------------------------------------------------------- editing Word files
def _all_paragraphs(doc):
    for p in doc.paragraphs:
        yield p
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p


def _replace_in_paragraph(p, find, repl):
    if find not in p.text:
        return 0
    count = p.text.count(find)
    for run in p.runs:                                   # best case: the text sits inside one run - formatting stays
        if find in run.text:
            run.text = run.text.replace(find, repl)
            return count
    text = p.text.replace(find, repl)                    # the text spans several runs: keep the first run's formatting
    for i, run in enumerate(p.runs):
        run.text = text if i == 0 else ""
    return count


def edit_docx(src, dst, ops, log):
    from docx import Document
    doc = Document(str(src))
    changes = 0
    for op in ops:
        kind = str(op.get("op", "")).lower()
        find = str(op.get("find", ""))
        c = 0
        if kind == "replace_text":
            if not find:
                raise ValueError("'find' is empty")
            for p in _all_paragraphs(doc):
                c += _replace_in_paragraph(p, find, str(op.get("replace", "")))
        elif kind in ("insert_after", "delete_paragraph"):
            for p in list(doc.paragraphs):
                if find and find in p.text:
                    if kind == "delete_paragraph":
                        p._element.getparent().remove(p._element)
                    else:
                        new = copy.deepcopy(p._element)
                        p._element.addnext(new)
                        from docx.text.paragraph import Paragraph
                        np_ = Paragraph(new, p._parent)
                        for i, run in enumerate(np_.runs):
                            run.text = str(op.get("text", "")) if i == 0 else ""
                    c += 1
                    break
        elif kind == "append_paragraph":
            doc.add_paragraph(str(op.get("text", "")))
            c = 1
        else:
            raise ValueError(f"unknown Word operation '{kind}'")
        log(f"  {kind} '{find[:40]}': {c} change(s)")
        changes += c
    doc.save(str(dst))
    return changes


def run_edit(act, folders, out_dir, log):
    """Execute one cerry_edit action -> (path, changes, report_lines)."""
    src = find_source(act["source"], folders)
    if src is None:
        raise FileNotFoundError(f"'{act['source']}' was not found in the workspace documents or outputs")
    ext = src.suffix.lower()
    if ext not in EDIT_EXT:
        raise ValueError(f"Editing {ext} files is not supported (only PDF and Word)")
    try:
        data = json.loads(act["ops_text"])
    except ValueError as e:
        raise ValueError(f"The list of operations is not valid JSON ({e})")
    ops = data.get("operations", data) if isinstance(data, dict) else data
    if not isinstance(ops, list) or not ops:
        raise ValueError("No operations given")
    for op in ops:
        if isinstance(op, dict) and str(op.get("op", "")).lower() == "merge":
            op["_paths"] = [p for p in (find_source(n, folders) for n in op.get("files", [])) if p is not None]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = act["name"] if Path(act["name"]).suffix.lower() == ext else Path(act["name"]).stem + ext
    dst = unique_path(out_dir, safe_name(name, EDIT_EXT))
    lines = []
    changes = (edit_pdf if ext == ".pdf" else edit_docx)(src, dst, [o for o in ops if isinstance(o, dict)], lines.append)
    if changes == 0:
        dst.unlink(missing_ok=True)
        raise ValueError("Nothing was changed - the text to find was not in the file. " + " ".join(lines))
    return dst, changes, lines


# ---------------------------------------------------------------------- what the model is told
FILE_RULES = """FILES. You can hand the user real files. Cerry creates them right after your answer - never say that a file exists
unless you used these tags, and keep your explanation outside the tags.
1) A NEW file:  <cerry_make name="report.docx"> ...content... </cerry_make>
   py txt md csv json html tex yaml sql ...: the content is written exactly as you give it.
   docx and pdf: write the content as Markdown (# headings, - bullets, 1. numbers, **bold**, | tables |, ``` code ```).
   xlsx: write CSV.
2) A CHANGED COPY of a PDF or Word file that is in the workspace (never retype such a file yourself):
   <cerry_edit source="exact file name.pdf" output="new name.pdf"> [ {"op": "replace_text", "find": "...", "replace": "..."} ] </cerry_edit>
   PDF operations:  replace_text(find, replace, pages), highlight_text(find, pages), delete_pages(pages), keep_pages(pages),
                    rotate_pages(pages, angle), reorder_pages(order), add_text(page, x, y, text, size) with x and y in points from the
                    top-left corner, watermark(text), merge(files), metadata(title, author).   Page numbers start at 1.
   Word operations: replace_text(find, replace), insert_after(find, text), delete_paragraph(find), append_paragraph(text).
   "find" must be copied EXACTLY from the document text that was given to you (one line of it, no line breaks).
At most 3 files per answer. When you write code, a command or a complete text the user will copy (letter, e-mail, CV),
put it in a fenced block with a language tag (```python, ```text, ...)."""

REQUEST_RE = re.compile(
    r"\b(pdf|docx?|word|excel|xlsx|csv|script|\.py|\.txt|markdown|latex|file|datei|dokument|export\w*|exportier\w*|"
    r"save as|speicher\w*|convert\w*|umwandel\w*|modify|edit\w*|change|replace|ersetz\w*|änder\w*|aender\w*|"
    r"update|create|erstell\w*|generate|make me|write me|return it|give me)\b", re.I)


# ---------------------------------------------------------------------- code boxes in the chat (pure helpers)
def find_blocks(text):
    """Fenced code blocks of a text: [(open_line, close_line, language)] with 1-based line numbers.
    A fence is a line that starts with three backticks; an unmatched opening fence is ignored."""
    blocks, opened = [], None
    for i, line in enumerate(text.split("\n"), 1):
        if line.startswith("```"):
            if opened is None:
                opened = (i, line[3:].strip().split(" ")[0].lower())
            else:
                blocks.append((opened[0], i, opened[1]))
                opened = None
    return blocks


def shade(color, dark_to_light):
    """A slightly different colour than the chat background, for the code box. color: '#rrggbb' or a Tk name (-> grey)."""
    m = re.fullmatch(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", color or "")
    if not m:
        return "#2f3136" if dark_to_light else "#eef0f2"
    r, g, b = (int(x, 16) for x in m.groups())
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    step = 18 if lum < 128 else -14
    clamp = lambda v: max(0, min(255, v + step))
    return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b))


def mentioned_files(user_text, folders, exts=(".pdf", ".docx")):
    """Workspace files (pdf/docx) that the user's message names, by file name or by file name without extension."""
    low = (user_text or "").lower()
    found = []
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for p in iter_files(folder, lambda name: name.startswith(".")):
            if p.suffix.lower() in exts:
                if p.name.lower() in low or (len(p.stem) >= 4 and p.stem.lower() in low):
                    if p not in found:
                        found.append(p)
                        if len(found) == 3:
                            return found
    return found


def page_text(pages, limit=24000):
    """Pages [(number_or_None, text)] -> one text with page markers, shortened to `limit` characters."""
    parts = [(f"[page {n}]\n" if n else "") + t.strip() for n, t in pages if t and t.strip()]
    text = "\n\n".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n[... the rest of the document is not shown here ...]"
