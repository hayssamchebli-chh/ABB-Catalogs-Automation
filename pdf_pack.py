"""Build the merged datasheet pack.

Layout: optional front cover, a clickable table of contents, then for every
item a cover page (the template with the item's Type written on it)
followed by its datasheet.
"""
import os
import re
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import Fit
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as pdf_canvas

APP_DIR = Path(__file__).resolve().parent

# Cover page inserted before each item's datasheet in the merged PDF
COVER_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "item_type_template.pdf",
)
COVER_TEXT_COLOR = "#1F4EA1"  # same blue as the Harb Electric logo
COVER_TEXT_X = 42  # left aligned with the logo
COVER_TEXT_TOP_OFFSET = 170  # distance of the first line from the top of the page
COVER_TEXT_MAX_WIDTH = 340  # keep the text inside the white area
COVER_TEXT_FONT = "Helvetica-Bold"
COVER_TEXT_FONT_SIZE = 34
COVER_TEXT_MIN_FONT_SIZE = 18

# Table of contents at the beginning of the merged PDF
TOC_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toc_logo.png")
TOC_ACCENT_COLOR = "#1F4EA1"
TOC_TITLE_COLOR = "#102033"
TOC_DOTS_COLOR = "#9AA7B5"
TOC_MARGIN_X = 48
TOC_ENTRY_SPACING = 28
TOC_ENTRIES_FIRST_PAGE = 18
TOC_ENTRIES_LATER_PAGES = 22

# Optional front cover placed before the table of contents
FRONT_COVER_PATH = APP_DIR / "front_cover.pdf"


def ensure_pdf_filename(filename: str) -> str:
    filename = str(filename or "").strip() or "datasheet_pack.pdf"
    filename = re.sub(r"[\\/:*?\"<>|]+", "-", filename)
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def trim_to_pdf_start(data: bytes) -> bytes:
    marker = data.find(b"%PDF-")
    return data[marker:] if marker > 0 else data


def is_valid_pdf_bytes(pdf_bytes: bytes) -> bool:
    try:
        reader = PdfReader(BytesIO(trim_to_pdf_start(pdf_bytes)), strict=False)
        return len(reader.pages) > 0
    except Exception:
        return False


def get_front_cover_bytes(uploaded_cover, include_cover: bool) -> Tuple[Optional[bytes], Optional[str]]:
    """Return (cover_bytes, problem). problem explains why there is no cover."""
    if not include_cover:
        return None, None

    if uploaded_cover is not None:
        data = uploaded_cover.getvalue()
        if is_valid_pdf_bytes(data):
            return data, None
        return None, "The uploaded front cover is not a valid PDF."

    if FRONT_COVER_PATH.is_file():
        data = FRONT_COVER_PATH.read_bytes()
        if is_valid_pdf_bytes(data):
            return data, None

    return None, "No front_cover.pdf was found. The pack will be created without a front cover."


def load_cover_template_bytes() -> bytes | None:
    """Load the cover page template PDF shipped with the app."""
    try:
        with open(COVER_TEMPLATE_PATH, "rb") as f:
            return f.read()
    except Exception:
        return None


def build_type_overlay(type_text: str, page_width: float, page_height: float) -> bytes:
    """Draw the item type in the blank space under the logo of the cover page."""
    buffer = BytesIO()
    overlay = pdf_canvas.Canvas(buffer, pagesize=(page_width, page_height))
    overlay.setFillColor(HexColor(COVER_TEXT_COLOR))

    def wrap_lines(font_size: int) -> List[str]:
        lines = []
        current = ""
        for word in type_text.split():
            candidate = f"{current} {word}".strip()
            if overlay.stringWidth(candidate, COVER_TEXT_FONT, font_size) <= COVER_TEXT_MAX_WIDTH:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    font_size = COVER_TEXT_FONT_SIZE
    lines = wrap_lines(font_size)

    while font_size > COVER_TEXT_MIN_FONT_SIZE and (
        len(lines) > 3
        or any(
            overlay.stringWidth(line, COVER_TEXT_FONT, font_size) > COVER_TEXT_MAX_WIDTH
            for line in lines
        )
    ):
        font_size -= 2
        lines = wrap_lines(font_size)

    overlay.setFont(COVER_TEXT_FONT, font_size)
    y = page_height - COVER_TEXT_TOP_OFFSET

    for line in lines:
        overlay.drawString(COVER_TEXT_X, y, line)
        y -= font_size * 1.3

    overlay.save()
    return buffer.getvalue()


def build_cover_page(template_bytes: bytes, type_text: str):
    """Return the cover template page, with the item type written on it."""
    template_reader = PdfReader(BytesIO(template_bytes))
    page = template_reader.pages[0]

    if type_text:
        overlay_bytes = build_type_overlay(
            type_text,
            float(page.mediabox.width),
            float(page.mediabox.height),
        )
        overlay_reader = PdfReader(BytesIO(overlay_bytes))
        page.merge_page(overlay_reader.pages[0])

    return page


def toc_pages_needed(entry_count: int) -> int:
    """Number of pages the table of contents itself will occupy."""
    if entry_count <= TOC_ENTRIES_FIRST_PAGE:
        return 1

    remaining = entry_count - TOC_ENTRIES_FIRST_PAGE
    extra_pages = -(-remaining // TOC_ENTRIES_LATER_PAGES)  # ceiling division
    return 1 + extra_pages


def build_toc_pdf(
    entries: List[dict],
    page_width: float,
    page_height: float,
) -> tuple:
    """Draw the table of contents pages.

    entries: [{"title": str, "target_page": int}] where target_page is the
    0-based page index of the item's cover page in the final document.

    Returns (pdf_bytes, link_boxes). link_boxes hold the clickable rectangle
    of every entry: [{"page": toc_page_index, "rect": (x0,y0,x1,y1), "target": int}].
    """
    buffer = BytesIO()
    toc = pdf_canvas.Canvas(buffer, pagesize=(page_width, page_height))

    accent = HexColor(TOC_ACCENT_COLOR)
    title_color = HexColor(TOC_TITLE_COLOR)
    dots_color = HexColor(TOC_DOTS_COLOR)

    number_x = TOC_MARGIN_X
    title_x = TOC_MARGIN_X + 34
    page_num_right = page_width - TOC_MARGIN_X
    max_title_width = page_num_right - title_x - 60

    def truncate(text: str, font: str, size: float) -> str:
        if toc.stringWidth(text, font, size) <= max_title_width:
            return text
        while text and toc.stringWidth(text + "...", font, size) > max_title_width:
            text = text[:-1]
        return text.rstrip() + "..."

    def draw_first_page_header() -> float:
        """Draw logo + title, return the y where entries start."""
        y_top = page_height - 52

        try:
            from reportlab.lib.utils import ImageReader

            logo = ImageReader(TOC_LOGO_PATH)
            logo_w, logo_h = logo.getSize()
            draw_h = 26
            draw_w = logo_w * draw_h / logo_h
            toc.drawImage(
                logo,
                TOC_MARGIN_X,
                y_top - draw_h,
                width=draw_w,
                height=draw_h,
                mask="auto",
            )
        except Exception:
            pass

        title_y = y_top - 64
        toc.setFillColor(title_color)
        toc.setFont("Helvetica-Bold", 27)
        toc.drawString(TOC_MARGIN_X, title_y, "Table of Contents")

        toc.setFillColor(accent)
        toc.rect(TOC_MARGIN_X, title_y - 14, 64, 4, stroke=0, fill=1)

        return title_y - 52

    def draw_later_page_header() -> float:
        toc.setFillColor(dots_color)
        toc.setFont("Helvetica", 11)
        toc.drawString(TOC_MARGIN_X, page_height - 56, "Table of Contents (continued)")
        toc.setFillColor(accent)
        toc.rect(TOC_MARGIN_X, page_height - 64, 42, 2.6, stroke=0, fill=1)
        return page_height - 100

    link_boxes = []
    toc_page_index = 0
    y = draw_first_page_header()
    capacity = TOC_ENTRIES_FIRST_PAGE
    drawn_on_page = 0

    for position, entry in enumerate(entries, start=1):
        if drawn_on_page >= capacity:
            toc.showPage()
            toc_page_index += 1
            y = draw_later_page_header()
            capacity = TOC_ENTRIES_LATER_PAGES
            drawn_on_page = 0

        title = truncate(entry["title"], "Helvetica-Bold", 12.5)
        page_label = str(entry["target_page"] + 1)

        toc.setFillColor(accent)
        toc.setFont("Helvetica-Bold", 10.5)
        toc.drawString(number_x, y, f"{position:02d}")

        toc.setFillColor(title_color)
        toc.setFont("Helvetica-Bold", 12.5)
        toc.drawString(title_x, y, title)

        toc.setFont("Helvetica-Bold", 11.5)
        toc.setFillColor(accent)
        toc.drawRightString(page_num_right, y, page_label)

        title_end = title_x + toc.stringWidth(title, "Helvetica-Bold", 12.5) + 8
        num_start = page_num_right - toc.stringWidth(page_label, "Helvetica-Bold", 11.5) - 8
        if num_start > title_end + 12:
            toc.setFillColor(dots_color)
            toc.setFont("Helvetica", 10)
            dot = "."
            dot_width = toc.stringWidth(dot, "Helvetica", 10) + 3.2
            x = title_end
            while x < num_start:
                toc.drawString(x, y + 1, dot)
                x += dot_width

        link_boxes.append(
            {
                "page": toc_page_index,
                "rect": (TOC_MARGIN_X - 6, y - 8, page_num_right + 6, y + 14),
                "target": entry["target_page"],
            }
        )

        y -= TOC_ENTRY_SPACING
        drawn_on_page += 1

    toc.save()
    return buffer.getvalue(), link_boxes


def merge_items_with_covers(
    items: List[dict],
    template_bytes,
    front_cover_bytes: Optional[bytes] = None,
) -> bytes:
    """Merge every item's datasheet into one PDF.

    The document starts with the optional front cover and a clickable table
    of contents listing each item's Type. Every successful item then
    contributes a cover page (the template with the item's Type written on
    it) followed by its datasheet. Items are kept in order and duplicates are
    NOT removed: every item gets its own cover and datasheet even when two
    items share the same file.

    items: [{"type": str, "code": str, "pdf_bytes": bytes}]
    """
    prepared = []
    for item in items:
        if not item.get("pdf_bytes"):
            continue
        reader = PdfReader(BytesIO(trim_to_pdf_start(item["pdf_bytes"])), strict=False)
        prepared.append((item, reader))

    if not prepared:
        return b""

    front_pages = []
    if front_cover_bytes:
        front_pages = list(PdfReader(BytesIO(trim_to_pdf_start(front_cover_bytes)), strict=False).pages)

    cover_pages = 1 if template_bytes else 0
    toc_page_count = toc_pages_needed(len(prepared))

    if template_bytes:
        template_page = PdfReader(BytesIO(template_bytes)).pages[0]
        page_width = float(template_page.mediabox.width)
        page_height = float(template_page.mediabox.height)
    else:
        first_page = prepared[0][1].pages[0]
        page_width = float(first_page.mediabox.width)
        page_height = float(first_page.mediabox.height)

    entries = []
    cursor = len(front_pages) + toc_page_count
    for item, reader in prepared:
        title = (item.get("type") or "").strip() or item.get("code", "") or "Item"
        entries.append({"title": title, "target_page": cursor})
        cursor += cover_pages + len(reader.pages)

    toc_bytes, link_boxes = build_toc_pdf(entries, page_width, page_height)

    writer = PdfWriter()

    for page in front_pages:
        writer.add_page(page)

    for page in PdfReader(BytesIO(toc_bytes)).pages:
        writer.add_page(page)

    for item, reader in prepared:
        if template_bytes:
            writer.add_page(build_cover_page(template_bytes, (item.get("type") or "").strip()))
        for page in reader.pages:
            writer.add_page(page)

    for box in link_boxes:
        writer.add_annotation(
            page_number=len(front_pages) + box["page"],
            annotation=Link(
                rect=box["rect"],
                target_page_index=box["target"],
                fit=Fit(fit_type="/Fit"),
            ),
        )

    for entry in entries:
        writer.add_outline_item(entry["title"], entry["target_page"])

    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output.getvalue()
