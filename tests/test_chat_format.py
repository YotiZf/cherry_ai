import pytest

from chat_format import equation_bitmap, spans


def test_dollars_in_prose_and_code():
    text = "Pay $5 or $10. Use $2 x$ and " + chr(96) + "$literal$" + chr(96) + "."
    assert [body for kind, body, _ in spans(text) if kind == "inline"] == ["2 x"]


def test_copyable_blocks_preserve_source():
    code = "  echo '$literal$'\n\n"
    tokens = list(spans("~~~sh\n" + code + "~~~\n" + r"\[\frac{1}{2}\]"))
    assert tokens == [("code", code, "sh"), ("text", "\n", ""), ("display", r"\frac{1}{2}", "")]


def test_math_render_and_fallback():
    pytest.importorskip("matplotlib")
    assert equation_bitmap(r"\frac{1}{2}", "#202123", 96).getbbox()
    assert equation_bitmap(r"\unknowncommand{x}", "#202123", 96) is None
