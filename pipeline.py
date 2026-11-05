"""Download every item's datasheet from its supplier, then build the pack.

ABB and Carlo Gavazzi pages are opened in one shared Firefox (Playwright).
Phoenix Contact PDFs are downloaded afterwards with Chrome (Selenium),
exactly as the Phoenix Contact app did. Running the browsers one after the
other keeps memory use low on Streamlit Cloud.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import streamlit as st
from playwright.async_api import async_playwright

from abb_datasheets import abb_product_url, download_abb_pdf_from_page, prepare_shared_context
from gavazzi_datasheets import download_gavazzi_pdf_from_page, gavazzi_product_url
from item_codes import SUPPLIER_NAMES, SUPPLIER_PREFIX
from pdf_pack import load_cover_template_bytes, merge_items_with_covers
from phoenix_datasheets import download_phoenix_datasheets

MAX_CONCURRENT_PAGES = 5
FIREFOX_SUPPLIERS = ("abb", "gavazzi")

# Use a writable persistent temp location on Streamlit Cloud
BROWSERS_DIR = Path("/tmp/playwright-browsers")
BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)

ProgressCallback = Callable[[int, int, str], None]
JobKey = Tuple[str, str]  # (supplier, code)


def format_duration(seconds: float) -> str:
    """42.3 -> "42 s", 67 -> "1 min 07 s", 3725 -> "1 h 02 min"."""
    if seconds < 1:
        return "under 1 s"
    if seconds < 10:
        return f"{seconds:.1f} s"

    total = int(round(seconds))
    if total < 60:
        return f"{total} s"

    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


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


def make_result(
    success: bool,
    pdf_bytes: Optional[bytes] = None,
    error: str = "",
    source_url: str = "",
    status: str = "",
) -> dict:
    return {
        "success": success,
        "pdf_bytes": pdf_bytes,
        "error": error,
        "source_url": source_url,
        "status": status or ("Downloaded" if success else "Failed"),
    }


def product_url(supplier: str, code: str) -> str:
    if supplier == "abb":
        return abb_product_url(code)
    if supplier == "gavazzi":
        return gavazzi_product_url(code)
    return ""


# ---------------------------
# Firefox: ABB + Carlo Gavazzi
# ---------------------------
async def download_one_with_firefox(page, supplier: str, code: str, output_dir: str) -> dict:
    if supplier == "abb":
        _, pdf_bytes = await download_abb_pdf_from_page(page, code, output_dir)
        return make_result(True, pdf_bytes, source_url=abb_product_url(code))

    pdf_bytes, datasheet_url = await download_gavazzi_pdf_from_page(page, code)
    return make_result(True, pdf_bytes, source_url=datasheet_url)


async def firefox_worker(context, queue: asyncio.Queue, results: dict, output_dir: str, on_result):
    while True:
        job = await queue.get()
        if job is None:
            queue.task_done()
            break

        supplier, code = job
        page = None

        try:
            page = await context.new_page()
            result = await download_one_with_firefox(page, supplier, code, output_dir)
        except Exception as e:
            result = make_result(False, error=str(e), source_url=product_url(supplier, code))
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            queue.task_done()

        results[job] = result
        if on_result is not None:
            on_result(supplier, code, result)


async def download_with_firefox(
    jobs: List[JobKey],
    max_concurrent_pages: int,
    on_result=None,
) -> Dict[JobKey, dict]:
    results: Dict[JobKey, dict] = {}
    queue: asyncio.Queue = asyncio.Queue()

    for job in jobs:
        await queue.put(job)

    with TemporaryDirectory() as output_dir:
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)

            try:
                if any(supplier == "abb" for supplier, _ in jobs):
                    await prepare_shared_context(context)

                workers = [
                    asyncio.create_task(firefox_worker(context, queue, results, output_dir, on_result))
                    for _ in range(max(1, min(max_concurrent_pages, len(jobs))))
                ]

                for _ in workers:
                    await queue.put(None)

                await queue.join()
                await asyncio.gather(*workers)

            finally:
                await context.close()
                await browser.close()

    return results


# ---------------------------
# Chrome: Phoenix Contact
# ---------------------------
def phoenix_result(raw: dict) -> dict:
    return make_result(
        bool(raw.get("ok") and raw.get("pdf_bytes")),
        raw.get("pdf_bytes"),
        error=str(raw.get("error") or ""),
        source_url=str(raw.get("used_url") or ""),
        status=str(raw.get("status") or ""),
    )


def download_phoenix(
    codes: List[str],
    selected_blocks: Sequence[str],
    chrome_settings: dict,
    on_result=None,
) -> Dict[JobKey, dict]:
    if not selected_blocks:
        error = "Select at least one Phoenix Contact PDF section."
        return {("phoenix", code): make_result(False, error=error) for code in codes}

    def report(raw: dict):
        if on_result is not None:
            on_result("phoenix", str(raw["code"]), phoenix_result(raw))

    try:
        raw_results = download_phoenix_datasheets(
            codes=codes,
            selected_blocks=selected_blocks,
            headless=chrome_settings.get("headless", True),
            timeout_seconds=chrome_settings.get("timeout_seconds", 60),
            browser_workers=chrome_settings.get("browser_workers", 1),
            on_result=report,
        )
    except Exception as e:
        error = (
            f"Chrome/Selenium failed before downloads could complete: {e}. "
            "On Streamlit Cloud, packages.txt must include chromium and chromium-driver."
        )
        return {("phoenix", code): make_result(False, error=error) for code in codes}

    results = {}
    for code in codes:
        raw = raw_results.get(code)
        results[("phoenix", code)] = (
            phoenix_result(raw) if raw else make_result(False, error="No result was returned for this item.")
        )
    return results


# ---------------------------
# Whole run
# ---------------------------
def download_all(
    items: List[dict],
    phoenix_blocks: Sequence[str],
    chrome_settings: dict,
    max_concurrent_pages: int = MAX_CONCURRENT_PAGES,
    on_progress: Optional[ProgressCallback] = None,
) -> Dict[JobKey, dict]:
    """Download each unique (supplier, code) once and return {(supplier, code): result}."""
    jobs: List[JobKey] = []
    for item in items:
        job = (item["supplier"], item["code"])
        if job not in jobs:
            jobs.append(job)

    total = len(jobs)
    finished = 0

    def report(supplier: str, code: str, result: dict):
        nonlocal finished
        finished += 1
        if on_progress is not None:
            on_progress(finished, total, f"{SUPPLIER_NAMES[supplier]} {code}: {result['status']}")

    results: Dict[JobKey, dict] = {}

    firefox_jobs = [job for job in jobs if job[0] in FIREFOX_SUPPLIERS]
    if firefox_jobs:
        if on_progress is not None:
            on_progress(finished, total, "Opening ABB and Carlo Gavazzi product pages...")
        try:
            ensure_playwright_firefox()
            results.update(asyncio.run(download_with_firefox(firefox_jobs, max_concurrent_pages, report)))
        except Exception as e:
            for job in firefox_jobs:
                if job not in results:
                    results[job] = make_result(False, error=f"Firefox could not run: {e}")

    phoenix_codes = [code for supplier, code in jobs if supplier == "phoenix"]
    if phoenix_codes:
        if on_progress is not None:
            on_progress(finished, total, "Downloading Phoenix Contact PDFs with Chrome...")
        results.update(download_phoenix(phoenix_codes, phoenix_blocks, chrome_settings, report))

    return results


def build_pack(items: List[dict], results: Dict[JobKey, dict], front_cover_bytes: Optional[bytes] = None) -> dict:
    """Merge one cover + datasheet per item, in the order the items were given."""
    merge_items = []
    success_rows = []
    failed_rows = []
    retry_codes = []

    for position, item in enumerate(items, start=1):
        result = results.get((item["supplier"], item["code"])) or make_result(False, error="Not downloaded.")
        row = {
            "#": position,
            "Supplier": SUPPLIER_NAMES[item["supplier"]],
            "Code": item["code"],
            "Type": item["type"],
            "Status": result["status"],
        }

        if result["success"] and result["pdf_bytes"]:
            merge_items.append(
                {
                    "type": item["type"],
                    "code": item["code"],
                    "pdf_bytes": result["pdf_bytes"],
                }
            )
            success_rows.append({**row, "Source": result["source_url"]})
        else:
            failed_rows.append({**row, "Error": result["error"], "Source": result["source_url"]})
            retry_code = f"{SUPPLIER_PREFIX[item['supplier']]}{item['code']}"
            if retry_code not in retry_codes:
                retry_codes.append(retry_code)

    merged_pdf = None
    if merge_items:
        merged_pdf = merge_items_with_covers(merge_items, load_cover_template_bytes(), front_cover_bytes)

    return {
        "merged_pdf": merged_pdf,
        "success_rows": success_rows,
        "failed_rows": failed_rows,
        "retry_codes": retry_codes,
        "downloaded_count": len(merge_items),
        "failed_count": len(failed_rows),
        "submitted_count": len(items),
    }
