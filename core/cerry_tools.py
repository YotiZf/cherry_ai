"""Cerry tools: parse model output, save files (txt/py/md/pdf/docx...), run Python scripts.
Pure logic, no GUI. Used by cerri_extensions.py."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .file_engine import make_docx, make_pdf, strip_fence, unique_path
else:
    from file_engine import make_docx, make_pdf, strip_fence, unique_path

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
