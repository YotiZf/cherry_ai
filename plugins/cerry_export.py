"""Cerry export + copy: save a reply or a whole chat as Markdown or PDF, and copy text while an answer is
still being written.

* Under every finished reply:   "📋 copy"   "⬇ export"
* Strip above the input box:    Copy answer  |  Export answer...  |  Export chat...
* Selecting text in the chat now works while Cerry is answering: the selection is kept, redrawing pauses
  while you drag with the mouse, and Ctrl+C / right-click > Copy work on the selected text.

PDF needs the package reportlab:   pip install reportlab      (Markdown needs nothing extra)
Switch it on by adding  "cerry_export"  to the list in  ai_workspace_data/plugins.json
"""
import os
import re
import textwrap
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
import tkinter as tk
from tkinter import ttk, filedialog

DRAG_TIMEOUT = 30            # seconds; a drag older than this no longer blocks redrawing


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------
def slug(text, default="cerry"):
    return re.sub(r"[^\w\-]+", "_", text or "").strip("_")[:40] or default


def message_markdown(m, name, reasoning=False, refs=True):
    """One message as Markdown text."""
    if m["role"] == "user":
        text = f"**You**\n\n{m['content']}"
        if m.get("images"):
            text += "\n\n*(attached images: " + ", ".join(m["images"]) + ")*"
        return text
    head = f"**{name}**" + (f" · {m['model']}" if m.get("model") else "") + ("  *(stopped)*" if m.get("stopped") else "")
    parts = [head, m["content"]]
    if reasoning and m.get("reasoning"):
        parts.append("> **Reasoning**\n>\n> " + m["reasoning"].strip().replace("\n", "\n> "))
    if refs:
        if m.get("sources"):
            lines = ["**References**" + (f" (web search: {m['query']})" if m.get("query") else "")]
            lines += [f"{n}. [{s.get('title') or s.get('url')}]({s.get('url')})" for n, s in enumerate(m["sources"], 1)]
            parts.append("\n".join(lines))
        if m.get("evidence"):
            lines = ["**Documents used**"]
            for e in m["evidence"]:
                where = e.get("file", "") + (f", p. {e['page']}" if e.get("page") else "")
                lines.append(f"- [{e.get('id', '')}] {where}")
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def reply_markdown(chat, index, name, question=True, reasoning=False, refs=True):
    msgs = chat["messages"]
    out = []
    if question and index > 0 and msgs[index - 1]["role"] == "user":
        out.append(message_markdown(msgs[index - 1], name))
    out.append(message_markdown(msgs[index], name, reasoning, refs))
    return "\n\n---\n\n".join(out) + "\n"


def chat_markdown(chat, name, workspace="", reasoning=False, refs=True):
    head = [f"# {chat.get('title') or 'Chat'}", "",
            f"*Exported from {name} on {datetime.now():%Y-%m-%d %H:%M}" + (f" · workspace: {workspace}" if workspace else "") + "*", ""]
    body = [message_markdown(m, name, reasoning, refs) for m in chat["messages"]]
    return "\n".join(head) + "\n" + "\n\n---\n\n".join(body) + "\n"


# ----------------------------------------------------------------------
# PDF (reportlab)
# ----------------------------------------------------------------------
_FONTS = {}
FONT_SETS = [
    (["segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"], "consola.ttf"),
    (["arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"], "cour.ttf"),
    (["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"], "DejaVuSansMono.ttf"),
]


def font_dirs():
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
            Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    try:
        import matplotlib
        dirs.append(Path(matplotlib.get_data_path()) / "fonts" / "ttf")
    except Exception:
        pass
    return dirs


def find_font(filename, dirs):
    for d in dirs:
        if (d / filename).exists():
            return d / filename
    return None


def register_fonts(dirs=None):
    """Register a Unicode font family if one is installed; otherwise use the built-in Latin-1 fonts."""
    key = tuple(map(str, dirs)) if dirs else None
    if key in _FONTS:
        return _FONTS[key]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    dirs = dirs or font_dirs()
    for family, mono in FONT_SETS:
        paths = [find_font(f, dirs) for f in family]
        if all(paths):
            names = ["CerryBody", "CerryBold", "CerryItalic", "CerryBoldItalic"]
            for n, p in zip(names, paths):
                pdfmetrics.registerFont(TTFont(n, str(p)))
            pdfmetrics.registerFontFamily("CerryBody", normal="CerryBody", bold="CerryBold",
                                          italic="CerryItalic", boldItalic="CerryBoldItalic")
            mono_path = find_font(mono, dirs)
            mono_name = "CerryMono" if mono_path else "Courier"
            if mono_path:
                pdfmetrics.registerFont(TTFont("CerryMono", str(mono_path)))
            _FONTS[key] = {"body": "CerryBody", "mono": mono_name, "unicode": True,
                           "glyphs": pdfmetrics.getFont("CerryBody").face.charToGlyph}
            return _FONTS[key]
    _FONTS[key] = {"body": "Helvetica", "mono": "Courier", "unicode": False, "glyphs": None}
    return _FONTS[key]


def sanitize(text, fonts):
    """Replace characters the PDF font cannot draw (emoji, rare symbols) with '?'."""
    if fonts["unicode"]:
        glyphs = fonts["glyphs"]
        # PDF fonts here handle 16-bit characters only, so emoji and other rare symbols become "?"
        return "".join(ch if (ch in "\n\t " or (ord(ch) <= 0xFFFF and ord(ch) in glyphs)) else "?" for ch in text)
    return text.encode("latin-1", "replace").decode("latin-1")


def inline(text, fonts):
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`]+)`", stash, text)
    t = escape(t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<link href="\2" color="#1a56c4">\1</link>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", t)
    return re.sub("\x00(\\d+)\x00",
                  lambda m: f'<font name="{fonts["mono"]}" backColor="#eeeeee">{escape(codes[int(m.group(1))])}</font>', t)


def markdown_to_flowables(md, fonts, width):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer, Preformatted, Table, TableStyle, HRFlowable
    body = fonts["body"]
    base = ParagraphStyle("b", fontName=body, fontSize=10.5, leading=14.5, spaceAfter=5)
    heads = {n: ParagraphStyle(f"h{n}", parent=base, fontName=body, fontSize=size, leading=size + 4, spaceBefore=8, spaceAfter=5,
                               textColor=colors.HexColor("#1f2937")) for n, size in zip(range(1, 7), (18, 15, 13, 12, 11, 10.5))}
    quote = ParagraphStyle("q", parent=base, leftIndent=12, textColor=colors.HexColor("#555555"), borderPadding=2)
    code_style = ParagraphStyle("c", fontName=fonts["mono"], fontSize=8.5, leading=11, backColor=colors.HexColor("#f3f4f6"),
                                borderPadding=4, spaceAfter=7, leftIndent=4)
    cell = ParagraphStyle("t", parent=base, fontSize=9, leading=11, spaceAfter=0)
    flow, lines, i = [], md.splitlines(), 0
    list_re = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block += textwrap.wrap(lines[i], 95, drop_whitespace=False, replace_whitespace=False) or [""]
                i += 1
            i += 1
            flow.append(Preformatted("\n".join(block), code_style))
        elif re.match(r"^#{1,6}\s", line):
            level = len(line) - len(line.lstrip("#"))
            flow.append(Paragraph(inline(line.lstrip("#").strip(), fonts), heads[min(level, 6)]))
            i += 1
        elif re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cccccc"), spaceBefore=6, spaceAfter=8))
            i += 1
        elif line.lstrip().startswith(">"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                block.append(lines[i].lstrip()[1:].strip())
                i += 1
            for para in "\n".join(block).split("\n\n"):
                if para.strip(" \n"):
                    flow.append(Paragraph(inline(para.replace("\n", " "), fonts), quote))
        elif "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = []
            while i < len(lines) and "|" in lines[i]:
                if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            cols = max(len(r) for r in rows)
            data = [[Paragraph(inline(c, fonts), cell) for c in r + [""] * (cols - len(r))] for r in rows]
            table = Table(data, colWidths=[width / cols] * cols, repeatRows=1)
            table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
                                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            flow += [table, Spacer(1, 7)]
        elif list_re.match(line):
            while i < len(lines) and list_re.match(lines[i]):
                indent, marker, item = list_re.match(lines[i]).groups()
                depth = len(indent.replace("\t", "    ")) // 2
                style = ParagraphStyle("l", parent=base, leftIndent=16 + 14 * depth, bulletIndent=4 + 14 * depth, spaceAfter=2,
                                       bulletFontName=body)
                bullet = "•" if marker in "-*+" else marker
                flow.append(Paragraph(inline(item, fonts), style, bulletText=bullet))
                i += 1
            flow.append(Spacer(1, 4))
        elif not line.strip():
            i += 1
        else:
            block = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|\s*>|\s*([-*+]|\d+[.)])\s)", lines[i]):
                block.append(lines[i].strip())
                i += 1
            flow.append(Paragraph(inline(" ".join(block), fonts), base))
    return flow


def build_pdf(md, path, title="Cerry", fonts=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate
    fonts = fonts or register_fonts()
    md = sanitize(md, fonts)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=sanitize(title, fonts), author="Cerry")

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(fonts["body"], 8)
        canvas.setFillGray(0.5)
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"{d.page}")
        canvas.restoreState()

    doc.build(markdown_to_flowables(md, fonts, doc.width), onFirstPage=footer, onLaterPages=footer)
    return Path(path)


# ----------------------------------------------------------------------
# the add-on
# ----------------------------------------------------------------------
def install(ns):
    App = ns["App"]
    if App.__dict__.get("_ex_installed"):
        return
    App._ex_installed = True
    name = ns.get("APP_NAME", "Cerry")

    # ---------- helpers ----------
    def put_clipboard(app, text):
        app.clipboard_clear()
        app.clipboard_append(text)
        app.update()

    def last_reply_index(app):
        for i in range(len(app.chat["messages"]) - 1, -1, -1):
            if app.chat["messages"][i]["role"] == "assistant":
                return i
        return None

    def copy_message(app, i):
        put_clipboard(app, app.chat["messages"][i]["content"])
        app.status.set("Reply copied to the clipboard.")

    def copy_current(app):
        """Copy the answer being written right now, or else the last finished reply."""
        if app.live and app.live.get("answer"):
            put_clipboard(app, app.live["answer"])
            app.status.set("Copied the answer written so far.")
        else:
            i = last_reply_index(app)
            if i is None:
                app.status.set("There is no reply to copy yet.")
            else:
                copy_message(app, i)

    def export_dialog(app, index=None):
        """index = number of a reply, or None for the whole chat."""
        if index is None and not app.chat["messages"]:
            app.status.set("This chat is empty.")
            return
        win = tk.Toplevel(app)
        win.title("Export reply" if index is not None else "Export chat")
        win.transient(app)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        fmt = tk.StringVar(value="Markdown")
        q = tk.BooleanVar(value=True)
        r = tk.BooleanVar(value=False)
        s = tk.BooleanVar(value=True)
        ttk.Label(frm, text="Format").pack(anchor="w")
        ttk.Radiobutton(frm, text="Markdown (.md)", value="Markdown", variable=fmt).pack(anchor="w", padx=14)
        ttk.Radiobutton(frm, text="PDF (.pdf)", value="PDF", variable=fmt).pack(anchor="w", padx=14)
        ttk.Label(frm, text="Include").pack(anchor="w", pady=(10, 0))
        if index is not None:
            ttk.Checkbutton(frm, text="my question", variable=q).pack(anchor="w", padx=14)
        ttk.Checkbutton(frm, text="the reasoning (if the model returned any)", variable=r).pack(anchor="w", padx=14)
        ttk.Checkbutton(frm, text="references and documents used", variable=s).pack(anchor="w", padx=14)

        def save():
            chat = app.chat
            if index is not None:
                md = reply_markdown(chat, index, name, q.get(), r.get(), s.get())
                title = (chat.get("title") or "reply") + " - reply"
            else:
                md = chat_markdown(chat, name, app.ws.name, r.get(), s.get())
                title = chat.get("title") or "chat"
            ext = ".pdf" if fmt.get() == "PDF" else ".md"
            folder = app.ws.dir / "exports"
            folder.mkdir(parents=True, exist_ok=True)
            target = filedialog.asksaveasfilename(
                parent=win, title="Save as", initialdir=str(folder), defaultextension=ext,
                initialfile=f"{slug(title)}_{time.strftime('%Y%m%d_%H%M')}{ext}",
                filetypes=[("PDF", "*.pdf")] if ext == ".pdf" else [("Markdown", "*.md")])
            if not target:
                return
            try:
                if ext == ".pdf":
                    build_pdf(md, target, title)
                else:
                    Path(target).write_text(md, encoding="utf-8")
            except ImportError:
                app.show("PDF export needs the package reportlab. Run in the Anaconda Prompt:  pip install reportlab")
                return
            except Exception as e:
                app.show(f"Export failed: {str(e)[:200]}")
                return
            app.show(f"⬇ Exported: {target}")
            app.status.set("Export saved.")
            win.destroy()

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Save as...", command=save).pack(side="right", padx=3)
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")

    def export_last(app):
        i = last_reply_index(app)
        if i is None:
            app.status.set("There is no reply to export yet.")
        else:
            export_dialog(app, i)

    # ---------- links under each reply + keeping the selection ----------
    def offset(w, index):
        res = w.count("1.0", index, "chars")          # a number, or a tuple holding one, depending on the Python version
        if isinstance(res, (tuple, list)):
            res = res[0] if res else 0
        return int(res or 0)

    def add_links(app):
        w = app.chat_w
        w.config(state="normal")
        for i, m in enumerate(app.chat["messages"]):
            if m["role"] != "assistant":
                continue
            r = w.tag_ranges(f"br{i}")
            if not r:
                continue
            pos = offset(w, r[1])
            for label, tag, fn in (("  📋 copy", f"cp{i}", lambda i=i: copy_message(app, i)),
                                   ("  ⬇ export", f"ex{i}", lambda i=i: export_dialog(app, i))):
                w.insert(f"1.0+{pos}c", label, ("link", tag))
                pos += len(label)
                w.tag_bind(tag, "<Button-1>", lambda e, fn=fn: fn())
                w.tag_bind(tag, "<Enter>", lambda e: w.config(cursor="hand2"))
                w.tag_bind(tag, "<Leave>", lambda e: w.config(cursor=""))
        w.config(state="disabled")

    orig_init = App.__init__
    orig_render = App.render_chat

    def init(app, *a, **k):
        app._ex_drag_at = None
        app._ex_skipped = False
        orig_init(app, *a, **k)
        w = app.chat_w

        def drag(e):
            app._ex_drag_at = time.time()

        def release(e):
            app._ex_drag_at = None
            if w.tag_ranges("sel"):
                w.focus_set()                         # so Ctrl+C copies the selected text
            if app._ex_skipped:
                app._ex_skipped = False
                app.render_chat(keep_view="auto")

        def key(e):
            if e.char and e.char.isprintable() and not (e.state & 0x4):      # typing: send it to the input box
                app.inp.focus_set()
                app.inp.insert("end", e.char)
                return "break"
            return None

        w.bind("<B1-Motion>", drag, add="+")
        w.bind("<ButtonRelease-1>", release, add="+")
        w.bind("<Control-c>", lambda e: (w.event_generate("<<Copy>>"), "break")[1])
        w.bind("<Control-a>", lambda e: (w.tag_add("sel", "1.0", "end-1c"), "break")[1])
        w.bind("<Key>", key, add="+")

        strip = app.sidebar.tool_row("Chat and export")
        ttk.Button(strip, text="📋 Copy answer", command=lambda: copy_current(app)).pack(side="left", padx=2)
        ttk.Button(strip, text="⬇ Export answer...", command=lambda: export_last(app)).pack(side="left", padx=2)
        ttk.Button(strip, text="⬇ Export chat...", command=lambda: export_dialog(app, None)).pack(side="left", padx=2)

    def render_chat(app, *a, **k):
        w = app.chat_w
        dragging = app._ex_drag_at is not None and time.time() - app._ex_drag_at < DRAG_TIMEOUT
        if dragging and app.live is not None:
            app._ex_skipped = True                    # do not redraw under the mouse while the user is selecting
            return None
        selection = None
        try:
            ranges = w.tag_ranges("sel")
            if ranges:
                selection = (offset(w, ranges[0]), offset(w, ranges[1]))
        except tk.TclError:
            pass
        result = orig_render(app, *a, **k)
        try:
            add_links(app)
            if selection:
                w.tag_add("sel", f"1.0+{selection[0]}c", f"1.0+{selection[1]}c")
        except tk.TclError:
            pass
        return result

    App.__init__, App.render_chat = init, render_chat
    App._ex_api = {"copy_current": copy_current, "export": export_dialog}
