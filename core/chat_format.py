"""Recognize copyable blocks and render mathematical notation without invoking a TeX executable."""
import re
from functools import lru_cache


_TOKEN = re.compile(
    r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})(?P<language>[^\n]*)\n"
    r"|(?P<literal>`+[^`\n]+`+)"
    r"|(?<!\\)(?P<math>\\\[|\\\(|\$\$|\$(?![\s$]))", re.MULTILINE)
_END = {r"\[": r"\]", r"\(": r"\)", "$$": "$$", "$": "$"}


def spans(text):
    """Yield (kind, content, label). Delimiters inside fenced and inline code are literal."""
    start = pos = 0
    while match := _TOKEN.search(text, pos):
        pos = match.end()
        if match["literal"]:
            continue
        label = ""
        if match["fence"]:
            fence = match["fence"]
            close = re.compile(r"^[ ]{0,3}" + re.escape(fence[0]) + "{%d,}" % len(fence)
                               + r"[ \t]*\r?$", re.MULTILINE).search(text, pos)
            label = match["language"].strip()
            body = text[pos:close.start() if close else len(text)]
            kind = "display" if label.lower() in ("math", "latex", "tex") else "code"
            pos = close.end() if close else len(text)
        else:
            opener = match["math"]
            closing = re.escape(_END[opener])
            if opener == "$":
                closing = r"(?<!\s)" + closing + r"(?![\d$])"
            limit = text.find("\n", pos) if opener in ("$", r"\(") else -1
            close = re.compile(r"(?<!\\)" + closing).search(text, pos, len(text) if limit < 0 else limit)
            if not close:
                continue
            body = text[pos:close.start()]
            if opener == "$" and re.search(r"(?<!\\)\$", body):
                continue  # A currency symbol before a separate formula.
            kind = "display" if opener in ("$$", r"\[") else "inline"
            pos = close.end()
        if match.start() > start:
            yield "text", text[start:match.start()], ""
        yield kind, body, label
        start = pos
    if start < len(text):
        yield "text", text[start:], ""


@lru_cache(maxsize=96)
def equation_bitmap(source, color, dpi):
    """Cache successful images and unsupported expressions across chat redraws."""
    if len(source) > 6000:
        return None
    try:
        from matplotlib.mathtext import MathTextParser
        from matplotlib.font_manager import FontProperties
        from PIL import Image
        import numpy as np
        expr = " ".join(source.split())
        parsed = MathTextParser("agg").parse(f"${expr}$", dpi=dpi, prop=FontProperties(size=12))
        mask = Image.fromarray(np.asarray(parsed.image).copy())
        result = Image.new("RGBA", mask.size, color)
        result.putalpha(mask)
        return result
    except (ImportError, ValueError, RuntimeError, OverflowError, RecursionError):
        return None
