import asyncio
import os
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import Fit
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as pdf_canvas

BASE_URL = "https://new.abb.com/products/{item_code}"
MAX_CONCURRENT_PAGES = 5

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

# Use a writable persistent temp location on Streamlit Cloud
BROWSERS_DIR = Path("/tmp/playwright-browsers")
BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)


def firefox_installed() -> bool:
    return any(BROWSERS_DIR.glob("firefox-*/firefox/firefox"))


@st.cache_resource(show_spinner=False)
def ensure_playwright_firefox():
    if firefox_installed():
        return True

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "firefox"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to install Playwright Firefox.\n\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )

    if not firefox_installed():
        raise RuntimeError(
            "Playwright reported success, but Firefox browser was not found in "
            f"{BROWSERS_DIR}"
        )

    return True
# ---------------------------
# Helpers
# ---------------------------
def clean_code(value: str) -> str:
    value = str(value).strip()
    if not value:
        return ""

    if "-" in value:
        value = value.split("-", 1)[1].strip()

    return value


def ensure_pdf_filename(filename: str) -> str:
    filename = filename.strip() or "abb_datasheet_pack.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def read_excel_file(uploaded_file) -> pd.DataFrame:
    return pd.read_excel(uploaded_file)


def is_valid_pdf_bytes(pdf_bytes: bytes) -> bool:
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        _ = len(reader.pages)
        return True
    except Exception:
        return False


def extract_items_from_excel(df: pd.DataFrame) -> List[dict]:
    """Parse Type / Code rows from the uploaded Excel file.

    Columns are matched by name (a column containing "type" and one containing
    "code" or "item"), falling back to the first two columns. Each row becomes
    one item: {"type": ..., "code": ...}. Rows are kept in order and repeated
    codes stay as separate items.
    """
    columns = list(df.columns)

    def find_column(keywords: tuple, fallback_index: int):
        for column in columns:
            name = str(column).strip().lower()
            if any(keyword in name for keyword in keywords):
                return column
        if len(columns) > fallback_index:
            return columns[fallback_index]
        return None

    if len(columns) == 1:
        type_col = None
        code_col = columns[0]
    else:
        type_col = find_column(("type",), 0)
        code_col = find_column(("code", "item"), 1)

    if code_col is None:
        return []

    def cell_text(row, column) -> str:
        if column is None:
            return ""
        value = row.get(column)
        if value is None or pd.isna(value):
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    items = []

    for _, row in df.iterrows():
        code = clean_code(cell_text(row, code_col))
        if not code:
            continue

        items.append(
            {
                "type": cell_text(row, type_col),
                "code": code,
            }
        )

    return items


def drop_untyped_duplicates(items: List[dict]) -> List[dict]:
    """Apply the duplicates rule to the item list.

    Items WITH a Type always keep their own cover page + datasheet, even
    when several items share the same code. Items WITHOUT a Type are
    included only once: repeated untyped occurrences are dropped, and an
    untyped occurrence is also dropped when the same code appears elsewhere
    with a Type (its datasheet is already in the pack).
    """
    typed_keys = {
        item["code"].casefold() for item in items if (item.get("type") or "").strip()
    }

    seen_untyped = set()
    result = []

    for item in items:
        key = item["code"].casefold()

        if not (item.get("type") or "").strip():
            if key in typed_keys or key in seen_untyped:
                continue
            seen_untyped.add(key)

        result.append(item)

    return result


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


def merge_items_with_covers(items: List[dict], template_bytes) -> bytes:
    """Merge every item's datasheet into one PDF.

    The document starts with a clickable table of contents listing each
    item's Type. Every successful item then contributes a cover page (the
    template with the item's Type written on it) followed by its datasheet.
    Items are kept in order and duplicates are NOT removed: every item gets
    its own cover and datasheet even when two items share the same file.

    items: [{"type": str, "code": str, "pdf_bytes": bytes}]
    """
    prepared = []
    for item in items:
        if not item.get("pdf_bytes"):
            continue
        reader = PdfReader(BytesIO(item["pdf_bytes"]), strict=False)
        prepared.append((item, reader))

    if not prepared:
        return b""

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
    cursor = toc_page_count
    for item, reader in prepared:
        title = (item.get("type") or "").strip() or item.get("code", "") or "Item"
        entries.append({"title": title, "target_page": cursor})
        cursor += cover_pages + len(reader.pages)

    toc_bytes, link_boxes = build_toc_pdf(entries, page_width, page_height)

    writer = PdfWriter()

    for page in PdfReader(BytesIO(toc_bytes)).pages:
        writer.add_page(page)

    for item, reader in prepared:
        if template_bytes:
            writer.add_page(build_cover_page(template_bytes, (item.get("type") or "").strip()))
        for page in reader.pages:
            writer.add_page(page)

    for box in link_boxes:
        writer.add_annotation(
            page_number=box["page"],
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


# ---------------------------
# Async Playwright logic
# ---------------------------
async def safe_goto(page, url: str, retries: int = 3):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            print(f"Opening: {url} (attempt {attempt}/{retries})")
            await page.goto(url, wait_until="load", timeout=60000)

            try:
                await page.locator("h1").first.wait_for(timeout=12000)
            except PlaywrightTimeoutError:
                pass

            await page.wait_for_timeout(1200)
            return
        except Exception as e:
            last_error = e
            print(f"Goto failed: {e}")
            await page.wait_for_timeout(1200)

    raise last_error


async def dismiss_cookie_overlay(page):
    possible_buttons = [
        page.get_by_role("button", name="Accept all"),
        page.get_by_role("button", name="Accept All"),
        page.get_by_role("button", name="Accept"),
        page.get_by_role("button", name="I Accept"),
        page.get_by_role("button", name="Allow all"),
        page.get_by_role("button", name="Agree"),
        page.get_by_text("Accept all", exact=False),
        page.get_by_text("Accept", exact=False),
    ]

    for locator in possible_buttons:
        try:
            await locator.first.wait_for(timeout=1500)
            await locator.first.click(timeout=2500)
            await page.wait_for_timeout(700)
            print("Cookie banner dismissed.")
            return True
        except Exception:
            pass

    try:
        await page.locator("#cassie-widget").evaluate(
            """el => {
                el.style.display = 'none';
                el.remove();
            }"""
        )
        await page.wait_for_timeout(300)
        print("Cookie overlay removed.")
        return True
    except Exception:
        pass

    return False


async def neutralize_cookie_overlay(page):
    """Stop the cookie-consent overlay from intercepting clicks.

    ABB's consent cookies are rejected by the browser ("invalid domain"), so
    the cassie widget reloads on every product page with an invisible
    full-page overlay that swallows clicks - and it re-creates the overlay
    when it is removed from the DOM. Injected CSS keeps applying to
    re-created elements, which makes it the reliable way to disable it.
    """
    try:
        await page.add_style_tag(
            content=(
                "#cassie-widget, .cassie-overlay, .syrenis-cookie-widget "
                "{ display: none !important; pointer-events: none !important; }"
            )
        )
    except Exception:
        pass

    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll(
                    '#cassie-widget, .cassie-overlay, .syrenis-cookie-widget'
                ).forEach(el => el.remove());
            }"""
        )
    except Exception:
        pass


async def prepare_shared_context(context):
    warmup_page = await context.new_page()
    try:
        await safe_goto(warmup_page, BASE_URL.format(item_code="TZW510"))
        await dismiss_cookie_overlay(warmup_page)
    except Exception as e:
        print(f"Warm-up step failed: {e}")
    finally:
        await warmup_page.close()


async def download_abb_pdf_from_page(page, item_code: str, output_dir: str, click_attempts: int = 3):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    url = BASE_URL.format(item_code=item_code)
    await safe_goto(page, url)

    # ABB generates some product PDFs on demand (PisWebApi Pdf/Generate) and
    # the first generation can take longer than one wait window. The result
    # is cached server-side, so retrying the click usually succeeds even when
    # the first attempt times out.
    download = None
    last_error = None

    for attempt in range(1, click_attempts + 1):
        try:
            await neutralize_cookie_overlay(page)

            async with page.expect_download(timeout=90000) as download_info:
                button = page.get_by_role("button", name="Print to PDF").first

                try:
                    await button.click(timeout=10000)
                except Exception:
                    # An overlay is still eating the click: trigger the
                    # button's handler directly, which no overlay can block.
                    await button.evaluate("el => el.click()")

            download = await download_info.value
            break
        except Exception as e:
            last_error = e
            print(f"{item_code}: no download on attempt {attempt}/{click_attempts} ({e})")

            if attempt < click_attempts:
                try:
                    await page.reload(wait_until="load", timeout=60000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)

    if download is None:
        raise RuntimeError(
            f"PDF was not generated after {click_attempts} attempts. The ABB PDF "
            f"generator can be slow for this product; running the same code again "
            f"usually works because the PDF gets cached. Last error: {last_error}"
        )

    suggested_name = download.suggested_filename
    if not suggested_name.lower().endswith(".pdf"):
        suggested_name = f"{item_code}.pdf"

    final_path = output_path / suggested_name
    await download.save_as(str(final_path))
    pdf_bytes = final_path.read_bytes()

    if not is_valid_pdf_bytes(pdf_bytes):
        raise ValueError(f"Downloaded file for {item_code} is not a valid PDF.")

    return final_path, pdf_bytes


async def worker(context, queue: asyncio.Queue, results: list, output_dir: str):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break

        index, item_code = item
        page = await context.new_page()

        try:
            file_path, pdf_bytes = await download_abb_pdf_from_page(page, item_code, output_dir)
            results.append(
                {
                    "index": index,
                    "code": item_code,
                    "success": True,
                    "file_path": str(file_path),
                    "pdf_bytes": pdf_bytes,
                    "error": None,
                }
            )
        except Exception as e:
            results.append(
                {
                    "index": index,
                    "code": item_code,
                    "success": False,
                    "file_path": None,
                    "pdf_bytes": None,
                    "error": str(e),
                }
            )
        finally:
            await page.close()
            queue.task_done()


async def download_abb_pdfs_shared_context(
    item_codes: List[str],
    output_dir: str,
    max_concurrent_pages: int = MAX_CONCURRENT_PAGES,
):
    queue: asyncio.Queue = asyncio.Queue()
    results = []

    for index, code in enumerate(item_codes):
        await queue.put((index, code))

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)

        try:
            await prepare_shared_context(context)

            workers = [
                asyncio.create_task(worker(context, queue, results, output_dir))
                for _ in range(min(max_concurrent_pages, len(item_codes)))
            ]

            for _ in workers:
                await queue.put(None)

            await queue.join()
            await asyncio.gather(*workers)

        finally:
            await context.close()
            await browser.close()

    results.sort(key=lambda x: x["index"])
    return results


def run_abb_download_pipeline(items: List[dict], max_concurrent_pages: int = MAX_CONCURRENT_PAGES):
    """Download every unique code once, then merge one cover + datasheet per item.

    items: [{"type": str, "code": str}] in the order they should appear in the
    merged PDF. Repeated codes are downloaded once but each item still gets
    its own cover page and datasheet copy.
    """
    ensure_playwright_firefox()

    unique_codes = []
    for item in items:
        if item["code"] not in unique_codes:
            unique_codes.append(item["code"])

    with TemporaryDirectory() as temp_dir:
        results = asyncio.run(
            download_abb_pdfs_shared_context(
                item_codes=unique_codes,
                output_dir=temp_dir,
                max_concurrent_pages=max_concurrent_pages,
            )
        )

        results_by_code = {result["code"]: result for result in results}

        success_rows = []
        failed_rows = []
        merge_items = []

        for item in items:
            result = results_by_code.get(item["code"], {})

            if result.get("success") and result.get("pdf_bytes"):
                merge_items.append(
                    {
                        "type": item["type"],
                        "code": item["code"],
                        "pdf_bytes": result["pdf_bytes"],
                    }
                )
                success_rows.append(
                    {
                        "Code": item["code"],
                        "Type": item["type"],
                        "Status": "Downloaded",
                    }
                )
            else:
                failed_rows.append(
                    {
                        "Code": item["code"],
                        "Type": item["type"],
                        "Status": "Failed",
                        "Error": result.get("error"),
                    }
                )

        merged_pdf = None
        if merge_items:
            template_bytes = load_cover_template_bytes()
            merged_pdf = merge_items_with_covers(merge_items, template_bytes)

        return {
            "merged_pdf": merged_pdf,
            "success_rows": success_rows,
            "failed_rows": failed_rows,
            "downloaded_count": len(merge_items),
            "failed_count": len(failed_rows),
            "submitted_count": len(items),
        }


# ---------------------------
# Page config
# ---------------------------
st.set_page_config(
    page_title="ABB Datasheet Pack Builder",
    page_icon="📕",
    layout="centered",
)


# ---------------------------
# Custom CSS (ABB-inspired)
# ---------------------------
st.markdown(
    """
    <style>
        :root {
            --abb-red: #e00000;
            --abb-red-dark: #b80000;
            --abb-charcoal: #1f1f1f;
            --abb-charcoal-soft: #2d2d2d;
            --abb-bg: #f5f5f5;
            --abb-card: #ffffff;
            --abb-border: #d9d9d9;
            --abb-muted: #666666;
            --abb-soft-red: #fff1f1;
        }

        .stApp {
            background-color: var(--abb-bg);
        }

        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 1000px;
        }

        .hero-card {
            background: linear-gradient(135deg, var(--abb-red) 0%, var(--abb-red-dark) 100%);
            color: white;
            border-radius: 18px;
            padding: 1.6rem 1.6rem 1.4rem 1.6rem;
            box-shadow: 0 10px 25px rgba(224, 0, 0, 0.18);
            margin-bottom: 1.2rem;
        }

        .hero-badge {
            display: inline-block;
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.4px;
            background: rgba(255,255,255,0.16);
            border: 1px solid rgba(255,255,255,0.20);
            padding: 0.35rem 0.7rem;
            border-radius: 999px;
            margin-bottom: 0.8rem;
        }

        .hero-title {
            font-size: 2rem;
            font-weight: 800;
            line-height: 1.2;
            margin-bottom: 0.45rem;
        }

        .hero-subtitle {
            font-size: 1rem;
            color: #ffeaea;
            line-height: 1.55;
            margin-bottom: 0;
        }

        .section-card {
            background: var(--abb-card);
            border: 1px solid var(--abb-border);
            border-radius: 16px;
            padding: 1.2rem 1.2rem 1rem 1.2rem;
            box-shadow: 0 6px 18px rgba(0, 0, 0, 0.04);
            margin-bottom: 1rem;
        }

        .section-title {
            font-size: 1.05rem;
            font-weight: 700;
            color: var(--abb-charcoal);
            margin-bottom: 0.2rem;
        }

        .section-subtitle {
            font-size: 0.93rem;
            color: var(--abb-muted);
            margin-bottom: 0.8rem;
        }

        .panel-title {
            color: var(--abb-charcoal);
            font-size: 1rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }

        .panel-subtitle {
            color: var(--abb-muted);
            font-size: 0.88rem;
            margin-bottom: 0.8rem;
        }

        div[data-testid="stTextArea"] textarea {
            background-color: #fbfbfb !important;
            border: 1px solid var(--abb-border) !important;
            border-radius: 12px !important;
            color: #111111 !important;
            font-size: 0.95rem !important;
            min-height: 220px !important;
        }

        div[data-testid="stTextInput"] input,
        div[data-testid="stSelectbox"] div[data-baseweb="select"] {
            background-color: #fbfbfb !important;
            border-radius: 12px !important;
        }

        div[data-testid="stFileUploader"] {
            background: #fbfbfb !important;
            border: 2px dashed #c8c8c8 !important;
            border-radius: 12px !important;
            padding: 24px !important;
            min-height: 220px !important;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        div[data-testid="stFileUploader"] section {
            width: 100%;
        }

        div[data-testid="stTextArea"] label,
        div[data-testid="stTextInput"] label,
        div[data-testid="stCheckbox"] label,
        div[data-testid="stSelectbox"] label,
        div[data-testid="stFileUploader"] label {
            color: var(--abb-charcoal) !important;
            font-weight: 700 !important;
        }

        .stButton > button,
        div[data-testid="stDownloadButton"] > button {
            background: var(--abb-red) !important;
            color: white !important;
            border: none !important;
            border-radius: 12px !important;
            font-weight: 700 !important;
            padding: 0.7rem 1rem !important;
            box-shadow: 0 8px 18px rgba(224, 0, 0, 0.14);
        }

        .stButton > button:hover,
        div[data-testid="stDownloadButton"] > button:hover {
            background: var(--abb-red-dark) !important;
        }

        .metric-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.9rem;
            margin: 1rem 0 1rem 0;
        }

        .metric-card {
            background: white;
            border: 1px solid var(--abb-border);
            border-radius: 14px;
            padding: 1rem;
            box-shadow: 0 6px 16px rgba(0, 0, 0, 0.04);
        }

        .metric-label {
            color: var(--abb-muted);
            font-size: 0.85rem;
            margin-bottom: 0.35rem;
        }

        .metric-value {
            color: var(--abb-charcoal);
            font-size: 1.7rem;
            font-weight: 800;
            line-height: 1.1;
        }

        .info-note {
            background: var(--abb-soft-red);
            border: 1px solid #f0caca;
            color: var(--abb-charcoal);
            border-radius: 12px;
            padding: 0.85rem 1rem;
            font-size: 0.93rem;
            margin-top: 0.5rem;
        }

        .footer-note {
            text-align: center;
            color: var(--abb-muted);
            font-size: 0.85rem;
            margin-top: 1rem;
        }

        div[data-testid="stExpander"] {
            background: white;
            border: 1px solid var(--abb-border);
            border-radius: 12px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------
# Header
# ---------------------------
st.markdown(
    """
    <div class="hero-card">
        <div class="hero-badge">ABB PDF AUTOMATION TOOL</div>
        <div class="hero-title">ABB Datasheet Pack Builder</div>
        <div class="hero-subtitle">
            Enter ABB item codes, retrieve their datasheets automatically,
            and generate one consolidated PDF pack ready for download.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------
# Input section
# ---------------------------
st.markdown(
    """
    <div class="section-card">
        <div class="section-title">Build your PDF pack</div>
        <div class="section-subtitle">
            Add codes manually or upload an Excel file with a Type column and a Code column.
            The Type is written on the cover page inserted before each item's datasheet.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

manual_items = []
excel_items = []
excel_df = None
uploaded_excel = None

input_col1, input_col2 = st.columns(2)

with input_col1:
    st.markdown(
        """
        <div class="panel-title">Paste item types and codes</div>
        <div class="panel-subtitle">
            Line 1 of Type belongs to line 1 of Code, and so on. Type can stay empty.
        </div>
        """,
        unsafe_allow_html=True,
    )

    manual_type_col, manual_code_col = st.columns(2)

    with manual_type_col:
        types_text = st.text_area(
            "Type",
            height=220,
            placeholder="Example:\nMotion Sensor\nSwitch 2G",
        )

    with manual_code_col:
        codes_text = st.text_area(
            "Code",
            height=220,
            placeholder="Example:\nZW213\nTZ107\nABB-TZW510",
        )

    type_lines = types_text.splitlines()
    code_lines = codes_text.splitlines()

    for line_index, raw_code in enumerate(code_lines):
        code = clean_code(raw_code.strip())
        if not code:
            continue

        type_text = ""
        if line_index < len(type_lines):
            type_text = type_lines[line_index].strip()

        manual_items.append({"type": type_text, "code": code})

with input_col2:
    st.markdown(
        """
        <div class="panel-title">Upload Excel file</div>
        <div class="panel-subtitle">
            Drag and drop your Excel file here, or browse to upload it.
        </div>
        """,
        unsafe_allow_html=True,
    )

    uploaded_excel = st.file_uploader(
        "Upload Excel file",
        type=["xlsx", "xls"],
        label_visibility="collapsed",
    )

    if uploaded_excel is not None:
        try:
            excel_df = read_excel_file(uploaded_excel)

            if excel_df.empty:
                st.warning("The uploaded Excel file is empty.")
            else:
                excel_items = extract_items_from_excel(excel_df)
                st.caption(f"{len(excel_items)} item(s) detected from Excel.")
        except Exception as e:
            st.error(f"Could not read Excel file: {e}")

# Full-width Excel preview below the input row
if excel_df is not None and not excel_df.empty:
    st.caption("Excel preview")
    st.dataframe(excel_df.head(10), use_container_width=True)

col1, col2 = st.columns([1, 1])
with col1:
    max_pages = st.selectbox(
        "Parallel pages",
        options=[2, 3, 4, 5],
        index=1,
    )
with col2:
    output_name = st.text_input("Output file name", value="abb_datasheet_pack.pdf")

st.markdown(
    """
    <div class="info-note">
        The merged PDF starts with a clickable table of contents, and each item's datasheet
        is preceded by a cover page showing its Type from the Excel file. Repeated codes with
        a Type each keep their own cover and datasheet; repeated codes without a Type are
        included only once.
    </div>
    """,
    unsafe_allow_html=True,
)

run_clicked = st.button("Build PDF Pack", type="primary", use_container_width=True)


# ---------------------------
# Action / Processing
# ---------------------------
if run_clicked:
    all_items = drop_untyped_duplicates(manual_items + excel_items)

    if not all_items:
        st.error("Please enter item codes manually or upload an Excel file.")
    else:
        with st.spinner("Downloading ABB PDFs and building the merged pack..."):
            try:
                result = run_abb_download_pipeline(
                    items=all_items,
                    max_concurrent_pages=min(max_pages, len(all_items)),
                )

                st.markdown(
                    f"""
                    <div class="metric-grid">
                        <div class="metric-card">
                            <div class="metric-label">Submitted</div>
                            <div class="metric-value">{result["submitted_count"]}</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">Downloaded</div>
                            <div class="metric-value">{result["downloaded_count"]}</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">Failed</div>
                            <div class="metric-value">{result["failed_count"]}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                if result["downloaded_count"] == 0 or result["merged_pdf"] is None:
                    st.error("No PDFs were downloaded, so no merged file could be created.")
                else:
                    st.success("Your consolidated ABB PDF pack is ready.")

                    st.download_button(
                        label="Download Merged PDF",
                        data=result["merged_pdf"],
                        file_name=ensure_pdf_filename(output_name),
                        mime="application/pdf",
                        use_container_width=True,
                    )

                    with st.expander("Downloaded items", expanded=False):
                        st.dataframe(result["success_rows"], use_container_width=True)

                    if result["failed_rows"]:
                        with st.expander("Failed codes", expanded=True):
                            st.dataframe(result["failed_rows"], use_container_width=True)

            except Exception as e:
                st.error(f"Processing failed: {e}")

st.markdown(
    """
    <div class="footer-note">
        Built for fast retrieval and packaging of ABB product documentation.
    </div>
    """,
    unsafe_allow_html=True,
)
