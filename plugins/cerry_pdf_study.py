# -*- coding: utf-8 -*-
"""
Created on Tue Nov  3 14:36:10 2026

@author: panaj
"""

import os
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit


# ---------------------------------------------------------------------------
# 1. Extract text from every page of the input PDF
# ---------------------------------------------------------------------------
def extract_pages(pdf_path: Path):
    """Return list of (page_number, text) tuples."""
    reader = PdfReader(str(pdf_path))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append((idx, text.strip()))
    return pages


# ---------------------------------------------------------------------------
# 2. Ask the LLM to explain one page
# ---------------------------------------------------------------------------
def explain_page(page_number: int, page_text: str, model_call, language: str = "en") -> str:
    """
    model_call: a function that takes a list of message dicts
                ([{"role": "user", "content": "..."}]) and returns a string.
    """
    if not page_text:
        return f"[Page {page_number}: no extractable text]"

    prompt = f"""
You are a patient study assistant. Explain the text below in clear, structured language.
Language: {language}
Reference the original page number ({page_number}) in your answer.
Do not invent facts that are not in the text.

Original page {page_number} text:
\"\"\"
{page_text}
\"\"\"
"""
    messages = [{"role": "user", "content": prompt}]
    return model_call(messages)


# ---------------------------------------------------------------------------
# 3. Render explanations into a PDF with reportlab
# ---------------------------------------------------------------------------
def build_explanation_pdf(explanations, output_path: Path, language: str = "en"):
    """
    explanations: list of (page_number, explanation_text)
    """
    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    margin = 54  # ~1.9 cm
    y = height - margin
    line_height = 15
    title_size = 13
    body_size = 10

    c.setFont("Helvetica-Bold", title_size)
    c.drawString(margin, y, f"Study Explanations ({language})")
    y -= line_height * 2

    for page_number, text in explanations:
        # New section header for each original page
        c.setFont("Helvetica-Bold", body_size + 1)
        header = f"--- Explanation for original page {page_number} ---"
        if y < margin:
            c.showPage()
            y = height - margin
        c.drawString(margin, y, header)
        y -= line_height

        c.setFont("Helvetica", body_size)
        wrapped = simpleSplit(text, "Helvetica", body_size, width - 2 * margin)
        for line in wrapped:
            if y < margin:
                c.showPage()
                y = height - margin
                c.setFont("Helvetica", body_size)
            c.drawString(margin, y, line)
            y -= line_height

        y -= line_height  # extra gap between pages

    c.save()


# ---------------------------------------------------------------------------
# 4. Merge original PDF + explanation PDF
# ---------------------------------------------------------------------------
def merge_pdfs(original_path: Path, extra_path: Path, output_path: Path):
    writer = PdfWriter()

    reader_orig = PdfReader(str(original_path))
    for page in reader_orig.pages:
        writer.add_page(page)

    reader_extra = PdfReader(str(extra_path))
    for page in reader_extra.pages:
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)


# ---------------------------------------------------------------------------
# 5. High-level entry point
# ---------------------------------------------------------------------------
def study_pdf(input_pdf: str | Path,
              model_call,
              output_pdf: str | Path = None,
              language: str = "en") -> Path:
    """
    Generate <name>.study.pdf next to the input file.
    Returns the path to the merged study PDF.
    """
    input_pdf = Path(input_pdf)
    if output_pdf is None:
        output_pdf = input_pdf.with_suffix(".study.pdf")

    pages = extract_pages(input_pdf)

    explanations = []
    for page_number, page_text in pages:
        explanation = explain_page(page_number, page_text, model_call, language=language)
        explanations.append((page_number, explanation))

    explanation_pdf = input_pdf.with_suffix(".explanation.pdf")
    build_explanation_pdf(explanations, explanation_pdf, language=language)

    merge_pdfs(input_pdf, explanation_pdf, output_pdf)

    # Clean up the intermediate explanation-only file
    try:
        explanation_pdf.unlink()
    except OSError:
        pass

    return output_pdf


# ---------------------------------------------------------------------------
# 6. Cherry plugin registration (pattern from cerry_restart.py / cerry_jobs_1.py)
# ---------------------------------------------------------------------------
def install(ns):
    """
    Called by Cerry's plugin loader. ns contains the App object.
    Expose study_pdf on the app so UI code or chat commands can call it.
    """
    app = ns.get("App")
    if app is None:
        return

    # Attach the function so other Cherry code can reach it:
    #   app.pdf_study("somefile.pdf", my_model_call)
    setattr(app, "pdf_study", staticmethod(study_pdf))

    # Optional: register a chat command /study-pdf <path>
    # You would hook this into App.send() depending on your Cherry version.
    # Example skeleton:
    original_send = getattr(app, "send", None)
    if original_send and not getattr(app, "_pdf_study_cmd_installed", False):
        def send_with_pdf_study(self, text, *args, **kwargs):
            if text.strip().startswith("/study-pdf "):
                path = text.strip()[len("/study-pdf "):].strip().strip('"')
                if os.path.isfile(path):
                    # You must supply a real model_call here, e.g. from Cherry's router.
                    raise NotImplementedError(
                        "Plug in your LLM model_call inside study_pdf()"
                    )
                else:
                    return f"File not found: {path}"
            return original_send(text, *args, **kwargs)
        app.send = send_with_pdf_study.__get__(app, type(app))
        app._pdf_study_cmd_installed = True
