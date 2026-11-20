"""Cerry tools: parse model output, save files (txt/py/md/pdf/docx...), run Python scripts.
Pure logic, no GUI. Used by cerri_extensions.py."""
import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ALLOWED_EXT = {'.py', '.txt', '.md', '.csv', '.tsv', '.json', '.html', '.yaml', '.yml',
               '.pdf', '.docx', '.tex', '.bib', '.r', '.sh_txt'}
RUN_TIMEOUT = 120
MAX_OUTPUT = 20000

TOOLS_PROMPT = (
    "FILE AND SCRIPT TOOLS. Use them only when the user asks you to create a file or to compute/run something.\n"
    "To create a file, write exactly:\n"
    '<cerry_file name="report.md">\nfile content here\n</cerry_file>\n'
    "Allowed file types: py, txt, md, csv, tsv, json, html, yaml, tex, bib, pdf, docx. "
    "For pdf and docx write the content as Markdown (# headings, - bullets, 1. numbered lists, "
    "**bold**, | tables |); Cerry converts it. Use a plain file name without folders.\n"
    "To run Python, write exactly:\n"
    "<cerry_run>\npython code here\n</cerry_run>\n"
    "Rules: the user reviews and approves everything, so never claim a file was saved or a script was run; "
    "Cerry shows you the real output afterwards. Scripts must be self-contained, print their results, "
    "write any output files into the current folder, never delete files, never read environment variables "
    "or API keys, and never install packages. Use at most one cerry_run block per reply. "
    "Put short explanations outside the tags."
)

FILE_RE = re.compile(r'<cerry_file\s+name\s*=\s*["\']([^"\']+)["\']\s*>\s*?\n?(.*?)</cerry_file>', re.S | re.I)
RUN_RE = re.compile(r'<cerry_run>\s*?\n?(.*?)</cerry_run>', re.S | re.I)


def strip_fence(text):
    t = text.strip('\n')
    m = re.match(r'^\s*```[\w+.-]*[ \t]*\n(.*?)\n?```\s*$', t, re.S)
    return m.group(1) if m else t


def extract_items(text):
    items = []
    for m in FILE_RE.finditer(text or ''):
        items.append({'kind': 'file', 'name': m.group(1).strip(),
                      'content': strip_fence(m.group(2)), 'pos': m.start()})
    for m in RUN_RE.finditer(text or ''):
        items.append({'kind': 'run', 'name': 'script.py',
                      'content': strip_fence(m.group(1)), 'pos': m.start()})
    items.sort(key=lambda i: i['pos'])
    return items


def safe_name(name):
    base = Path(str(name).replace('\\', '/')).name.strip()
    base = re.sub(r'[^\w.\- ]', '_', base).strip(' .')
    ext = Path(base).suffix.lower()
    if not base or ext not in ALLOWED_EXT:
        raise ValueError(f'File type not allowed: {name!r}. Allowed: ' + ', '.join(sorted(ALLOWED_EXT)))
    return base


def unique_path(folder, name):
    p = Path(folder) / name
    n = 2
    while p.exists():
        p = Path(folder) / f'{Path(name).stem}_{n}{Path(name).suffix}'
        n += 1
    return p


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


def save_file(outdir, name, content):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = unique_path(outdir, safe_name(name))
    ext = target.suffix.lower()
    if ext == '.pdf':
        make_pdf(content, target)
    elif ext == '.docx':
        make_docx(content, target)
    else:
        target.write_text(content, encoding='utf-8', newline='\n')
    return target


def _clip(text, limit=MAX_OUTPUT):
    if text is None:
        return ''
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    return text if len(text) <= limit else text[:limit] + f'\n... [{len(text) - limit} more characters cut]'


def run_python(code, outdir, timeout=RUN_TIMEOUT):
    """Run code in a separate Python process. Not a sandbox: it has the user's permissions."""
    outdir = Path(outdir)
    scripts = outdir / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    script = unique_path(scripts, time.strftime('run_%Y%m%d_%H%M%S.py'))
    script.write_text(code, encoding='utf-8', newline='\n')
    before = {p.name: p.stat().st_mtime_ns for p in outdir.iterdir() if p.is_file()}
    env = {k: v for k, v in os.environ.items()
           if not any(w in k.upper() for w in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', MPLBACKEND='Agg')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    timed_out, rc, out, err = False, None, '', ''
    try:
        r = subprocess.run([sys.executable, str(script)], cwd=str(outdir), env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=flags)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        timed_out, out, err = True, e.stdout, e.stderr
    new = sorted(p.name for p in outdir.iterdir()
                 if p.is_file() and before.get(p.name) != p.stat().st_mtime_ns)
    return {'returncode': rc, 'timed_out': timed_out, 'stdout': _clip(out), 'stderr': _clip(err),
            'new_files': new, 'script': str(script), 'timeout': timeout}


def format_result(res):
    head = (f"Timed out after {res['timeout']}s." if res['timed_out']
            else f"Exit code {res['returncode']}.")
    parts = [head]
    if res['stdout'].strip():
        parts.append('--- output ---\n' + res['stdout'].rstrip())
    if res['stderr'].strip():
        parts.append('--- errors ---\n' + res['stderr'].rstrip())
    if res['new_files']:
        parts.append('New or changed files: ' + ', '.join(res['new_files']))
    return '\n'.join(parts)
