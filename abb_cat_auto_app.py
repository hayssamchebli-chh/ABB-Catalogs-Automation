import asyncio
import re
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, List

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pypdf import PdfReader, PdfWriter

BASE_URL = "https://new.abb.com/products/{item_code}"
MAX_CONCURRENT_PAGES = 4

import os
import subprocess
import sys
from pathlib import Path
import streamlit as st

BROWSERS_DIR = Path(".playwright-browsers")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)

def firefox_installed() -> bool:
    return any(BROWSERS_DIR.glob("firefox-*/firefox/firefox"))

def ensure_playwright_firefox():
    if firefox_installed():
        return

    with st.spinner("Installing Playwright Firefox browser... this may take a minute on first startup."):
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "firefox"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Failed to install Playwright Firefox.\n\n"
                f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
            )
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


def normalize_codes(raw_codes: Iterable[str]) -> List[str]:
    codes = []

    for item in raw_codes:
        if item is None:
            continue

        item_str = str(item).strip()
        if not item_str:
            continue

        parts = re.split(r"[\s,;]+", item_str)
        for part in parts:
            part = clean_code(part)
            if part:
                codes.append(part)

    seen = set()
    unique_codes = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)

    return unique_codes


def ensure_pdf_filename(filename: str) -> str:
    filename = filename.strip() or "abb_datasheet_pack.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def read_excel_file(uploaded_file) -> pd.DataFrame:
    return pd.read_excel(uploaded_file)


def extract_codes_from_selected_column(df: pd.DataFrame, selected_column: str) -> List[str]:
    if selected_column not in df.columns:
        return []

    values = df[selected_column].dropna().astype(str).tolist()
    return normalize_codes(values)


def is_valid_pdf_bytes(pdf_bytes: bytes) -> bool:
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        _ = len(reader.pages)
        return True
    except Exception:
        return False


def merge_pdf_bytes(pdf_list: List[bytes]) -> bytes:
    writer = PdfWriter()

    for pdf_bytes in pdf_list:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        for page in reader.pages:
            writer.add_page(page)

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


async def prepare_shared_context(context):
    warmup_page = await context.new_page()
    try:
        await safe_goto(warmup_page, BASE_URL.format(item_code="TZW510"))
        await dismiss_cookie_overlay(warmup_page)
    except Exception as e:
        print(f"Warm-up step failed: {e}")
    finally:
        await warmup_page.close()


async def download_abb_pdf_from_page(page, item_code: str, output_dir: str):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    url = BASE_URL.format(item_code=item_code)
    await safe_goto(page, url)

    async with page.expect_download(timeout=30000) as download_info:
        button = page.get_by_role("button", name="Print to PDF")

        try:
            await button.click(timeout=5000)
        except Exception:
            await button.click(timeout=5000, force=True)

    download = await download_info.value

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


def run_abb_download_pipeline(codes: List[str], max_concurrent_pages: int = MAX_CONCURRENT_PAGES):
    with TemporaryDirectory() as temp_dir:
        results = asyncio.run(
            download_abb_pdfs_shared_context(
                item_codes=codes,
                output_dir=temp_dir,
                max_concurrent_pages=max_concurrent_pages,
            )
        )

        downloaded_pdf_bytes = []
        success_rows = []
        failed_rows = []

        for result in results:
            if result["success"] and result["pdf_bytes"]:
                downloaded_pdf_bytes.append(result["pdf_bytes"])
                success_rows.append(
                    {
                        "Code": result["code"],
                        "Status": "Downloaded",
                    }
                )
            else:
                failed_rows.append(
                    {
                        "Code": result["code"],
                        "Status": "Failed",
                        "Error": result["error"],
                    }
                )

        merged_pdf = merge_pdf_bytes(downloaded_pdf_bytes) if downloaded_pdf_bytes else None

        return {
            "merged_pdf": merged_pdf,
            "success_rows": success_rows,
            "failed_rows": failed_rows,
            "downloaded_count": len(downloaded_pdf_bytes),
            "failed_count": len(failed_rows),
            "submitted_count": len(codes),
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
            Enter ABB item codes, retrieve their Print-to-PDF documents automatically,
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
            Add codes manually or upload an Excel file and select the column containing the item codes.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

manual_codes = []
excel_codes = []
excel_df = None
uploaded_excel = None

input_col1, input_col2 = st.columns(2)

with input_col1:
    st.markdown(
        """
        <div class="panel-title">Paste item codes</div>
        <div class="panel-subtitle">
            Enter one code per line, or separate them with commas, spaces, or semicolons.
        </div>
        """,
        unsafe_allow_html=True,
    )

    codes_text = st.text_area(
        "Paste item codes",
        height=220,
        placeholder="Example:\nZW213\nTZ107\nABB-TZW510",
        label_visibility="collapsed",
    )

    manual_codes = normalize_codes(codes_text.splitlines())

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
                column_options = excel_df.columns.tolist()

                default_index = 0
                if "Item No.1" in column_options:
                    default_index = column_options.index("Item No.1")

                selected_column = st.selectbox(
                    "Select the column containing item codes",
                    options=column_options,
                    index=default_index,
                )

                excel_codes = extract_codes_from_selected_column(excel_df, selected_column)
                st.caption(f"{len(excel_codes)} code(s) detected from Excel.")
        except Exception as e:
            st.error(f"Could not read Excel file: {e}")

col1, col2 = st.columns([1, 1])
with col1:
    max_pages = st.selectbox(
        "Parallel pages",
        options=[2, 3, 4, 5],
        index=2,
    )
with col2:
    output_name = st.text_input("Output file name", value="abb_datasheet_pack.pdf")

st.markdown(
    """
    <div class="info-note">
        If a code contains a dash, the part after the first dash will be used as the final item code.
        Codes from manual input and Excel are combined automatically and duplicates are removed.
        The merged PDF keeps the same order as the codes entered by the user.
    </div>
    """,
    unsafe_allow_html=True,
)

run_clicked = st.button("Build PDF Pack", type="primary", use_container_width=True)


# ---------------------------
# Action / Processing
# ---------------------------
if run_clicked:
    codes = normalize_codes(manual_codes + excel_codes)

    if not codes:
        st.error("Please enter item codes manually or upload an Excel file.")
    else:
        with st.spinner("Downloading ABB PDFs and building the merged pack..."):
            try:
                result = run_abb_download_pipeline(
                    codes=codes,
                    max_concurrent_pages=min(max_pages, len(codes)),
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
