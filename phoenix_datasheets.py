"""Phoenix Contact datasheet downloads.

The download logic is taken unchanged from the Phoenix Contact Datasheet Pack
Builder: the user picks the PDF sections (Technical data and Drawings by
default), the item number is base64-encoded into the Phoenix Contact PDF API
URL, the AE / en-AE path is tried first and the PC / en-PC path second, and
Chrome (Selenium) downloads the generated PDF.
"""
import base64
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from pypdf import PdfReader
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# -----------------------------------------------------------------------------
# Phoenix Contact PDF API settings
# -----------------------------------------------------------------------------
BASE_PDF_API_URL = "https://www.phoenixcontact.com/product/pdf/api/v1/{encoded_code}"

DEFAULT_TIMEOUT_SECONDS = 60

PDF_BLOCK_OPTIONS: List[Tuple[str, str, str]] = [
    ("commercial-data", "Commercial data", "Basic commercial and ordering information."),
    ("technical-data", "Technical data", "Electrical, mechanical, and product specifications."),
    ("drawings", "Drawings", "Dimensional drawings and product graphics."),
    ("classifications", "Classifications", "ETIM, eCl@ss, UNSPSC, and other classifications."),
    (
        "environmental-compliance-data",
        "Environmental compliance data",
        "RoHS, REACH, China RoHS, and related compliance information.",
    ),
    ("all-accessories", "Accessories", "Compatible accessories listed in the PDF."),
]

DEFAULT_SELECTED_BLOCK_LABELS = [
    "Technical data",
    "Drawings",
]

PDF_URL_PROFILES = [
    {
        "name": "AE",
        "realm": "ae",
        "locale": "en-AE",
    },
    {
        "name": "PC",
        "realm": "pc",
        "locale": "en-PC",
    },
]

LOGIN_URL_MARKERS = [
    "login.phoenixcontact.com",
    "/oauth2/",
    "_oidc_auth",
]

NOT_FOUND_TITLE_MARKERS = [
    "page not found",
    "not found",
]

# -----------------------------------------------------------------------------
# Code helpers
# -----------------------------------------------------------------------------
def clean_phoenix_code(value: str) -> str:
    """Return the Phoenix Contact numeric item number safely.

    Handles:
    - PHX-3010110 -> 3010110
    - 3010110 -> 3010110
    - 3010110.0 -> 3010110
    - 3,010,110 -> 3010110
    """
    if value is None:
        return ""

    value = str(value).strip()

    if not value or value.lower() in {"nan", "none", "null"}:
        return ""

    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"\s*-\s*", "-", value)

    if "-" in value:
        value = value.rsplit("-", 1)[1].strip()

    value = value.replace(",", "").replace(" ", "")

    if re.fullmatch(r"\d+\.0+", value):
        value = value.split(".", 1)[0]

    if re.fullmatch(r"\d+\.\d+", value):
        value = value.split(".", 1)[0]

    return re.sub(r"[^0-9]", "", value)


def normalize_codes(raw_codes: Iterable[str]) -> List[str]:
    codes: List[str] = []

    for item in raw_codes:
        if item is None:
            continue

        item_str = str(item).strip()
        if not item_str:
            continue

        for part in re.split(r"[\s,;]+", item_str):
            code = clean_phoenix_code(part)
            if code:
                codes.append(code)

    unique: List[str] = []
    seen = set()

    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)

    return unique


def encode_item_number_for_phoenix(item_number: str) -> str:
    item_number = clean_phoenix_code(item_number)

    if not item_number:
        raise ValueError("Phoenix Contact item number is empty.")

    return base64.b64encode(item_number.encode("ascii")).decode("ascii").rstrip("=")


def build_phoenix_pdf_url(
    item_number: str,
    realm: str,
    locale: str,
    selected_blocks: Sequence[str],
) -> str:
    """Build Phoenix Contact PDF API URL without action parameter.

    Example:
    https://www.phoenixcontact.com/product/pdf/api/v1/{converted_code}
    ?_realm=ae&_locale=en-AE&blocks=technical-data%2Cdrawings
    """
    if not selected_blocks:
        raise ValueError("At least one PDF content block must be selected.")

    encoded_code = encode_item_number_for_phoenix(item_number)

    query = urlencode(
        [
            ("_realm", realm),
            ("_locale", locale),
            ("blocks", ",".join(selected_blocks)),
        ]
    )

    return f"{BASE_PDF_API_URL.format(encoded_code=encoded_code)}?{query}"


def build_candidate_pdf_urls(
    code: str,
    selected_blocks: Sequence[str],
) -> List[Tuple[str, str]]:
    """Return only the two allowed Phoenix Contact PDF URLs.

    Order:
    1. AE / en-AE
    2. PC / en-PC

    The blocks are chosen by the user, with Technical data and Drawings selected by default.
    """
    candidates: List[Tuple[str, str]] = []

    for profile in PDF_URL_PROFILES:
        url = build_phoenix_pdf_url(
            code,
            realm=profile["realm"],
            locale=profile["locale"],
            selected_blocks=selected_blocks,
        )
        candidates.append((url, profile["name"]))

    return candidates


# -----------------------------------------------------------------------------
# PDF helpers
# -----------------------------------------------------------------------------
def trim_to_pdf_start(data: bytes) -> bytes:
    marker = data.find(b"%PDF-")
    return data[marker:] if marker > 0 else data


def is_valid_pdf_bytes(pdf_bytes: bytes) -> bool:
    try:
        reader = PdfReader(BytesIO(trim_to_pdf_start(pdf_bytes)), strict=False)
        return len(reader.pages) > 0
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Chrome / Selenium helpers
# -----------------------------------------------------------------------------
def find_chrome_binary() -> Optional[str]:
    candidates = [
        os.environ.get("CHROME_BINARY"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)

    return None


def find_chromedriver_binary() -> Optional[str]:
    candidates = [
        os.environ.get("CHROMEDRIVER"),
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)

    return None


def create_selenium_driver(download_dir: Path, headless: bool) -> webdriver.Chrome:
    chrome_options = Options()

    chrome_binary = find_chrome_binary()
    if chrome_binary:
        chrome_options.binary_location = chrome_binary

    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--window-size=1280,900")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    chrome_options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
            "profile.default_content_setting_values.automatic_downloads": 1,
            "safebrowsing.enabled": True,
        },
    )

    chromedriver = find_chromedriver_binary()

    if chromedriver:
        driver = webdriver.Chrome(service=Service(chromedriver), options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)

    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {
            "behavior": "allow",
            "downloadPath": str(download_dir.resolve()),
        },
    )

    return driver


def clear_download_dir(download_dir: Path) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)

    for path in download_dir.iterdir():
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def wait_for_pdf_download(
    driver: webdriver.Chrome,
    download_dir: Path,
    timeout_seconds: int,
    expected_url: str,
) -> Tuple[Optional[Path], str]:
    """Wait for a PDF download and avoid stale Page Not Found detection.

    The key fix is expected_url: we only treat "Page not found" as real
    when Chrome is actually on the URL requested for this item/path.
    """
    start = time.time()
    last_seen = ""

    expected_base = expected_url.split("?", 1)[0].lower()

    while time.time() - start < timeout_seconds:
        try:
            current_url = driver.current_url or ""
            title = driver.title or ""
        except Exception:
            current_url = ""
            title = ""

        current_url_lower = current_url.lower()
        title_lower = title.lower()

        is_on_expected_request = (
            expected_base in current_url_lower
            or current_url_lower.startswith("chrome://downloads")
        )

        if any(marker in current_url_lower for marker in LOGIN_URL_MARKERS):
            return (
                None,
                "Phoenix Contact redirected this item to the sign-in page. "
                "This PDF appears to require a logged-in Phoenix Contact session.",
            )

        # Only accept Page Not Found when it belongs to this requested URL.
        # This prevents a stale not-found title from the previous item
        # from failing the next valid item.
        if is_on_expected_request and any(marker in title_lower for marker in NOT_FOUND_TITLE_MARKERS):
            return (
                None,
                "Phoenix Contact returned a page-not-found response for this generated PDF URL.",
            )

        pdf_files = list(download_dir.glob("*.pdf"))
        partial_files = list(download_dir.glob("*.crdownload"))
        all_files = list(download_dir.iterdir())

        last_seen = ", ".join(path.name for path in all_files) if all_files else "no files yet"

        if pdf_files and not partial_files:
            newest = max(pdf_files, key=lambda p: p.stat().st_mtime)

            # Stability check: make sure Chrome has finished writing.
            size_1 = newest.stat().st_size
            time.sleep(0.25)
            size_2 = newest.stat().st_size

            if size_1 == size_2 and size_2 > 0:
                return newest, ""

        time.sleep(0.2)

    return None, f"Download did not finish within {timeout_seconds} seconds. Files: {last_seen}"


def download_pdf_bytes_with_selenium(
    driver: webdriver.Chrome,
    download_dir: Path,
    url: str,
    timeout_seconds: int,
) -> Tuple[bool, Optional[bytes], str]:
    """Download one PDF using Chrome.

    This resets Chrome to about:blank before every candidate URL so a failed
    item cannot poison the next request.
    """
    clear_download_dir(download_dir)

    try:
        driver.get("about:blank")
        time.sleep(0.25)
        clear_download_dir(download_dir)

        driver.get(url)
    except Exception as exc:
        return False, None, f"Chrome could not open URL: {exc}"

    pdf_path, wait_error = wait_for_pdf_download(
        driver=driver,
        download_dir=download_dir,
        timeout_seconds=timeout_seconds,
        expected_url=url,
    )

    if pdf_path is None:
        current_url = ""
        title = ""

        try:
            current_url = driver.current_url
            title = driver.title
        except Exception:
            pass

        return False, None, f"{wait_error}. Browser title: {title}. Current URL: {current_url}"

    try:
        data = pdf_path.read_bytes()
    except OSError as exc:
        return False, None, f"Downloaded PDF could not be read: {exc}"

    if not is_valid_pdf_bytes(data):
        return False, None, f"Downloaded file is not a valid PDF: {pdf_path.name}"

    return True, trim_to_pdf_start(data), ""


def process_code_with_driver(
    driver: webdriver.Chrome,
    download_dir: Path,
    index: int,
    code: str,
    selected_blocks: Sequence[str],
    timeout_seconds: int,
) -> Dict[str, object]:
    encoded = encode_item_number_for_phoenix(code)
    candidate_urls = build_candidate_pdf_urls(code, selected_blocks)

    errors: List[str] = []

    for url, profile_name in candidate_urls:
        ok, data, error = download_pdf_bytes_with_selenium(
            driver=driver,
            download_dir=download_dir,
            url=url,
            timeout_seconds=timeout_seconds,
        )

        if ok and data:
            return {
                "index": index,
                "code": code,
                "encoded": encoded,
                "ok": True,
                "pdf_bytes": data,
                "used_url": url,
                "profile": profile_name,
                "status": "Downloaded",
                "error": "",
            }

        errors.append(f"[{profile_name}] {error}")

    first_url = candidate_urls[0][0] if candidate_urls else ""
    error_text = " | ".join(errors)

    if "sign-in page" in error_text.lower() or "login.phoenixcontact.com" in error_text.lower():
        status = "Auth required"
    elif "page-not-found" in error_text.lower() or "page not found" in error_text.lower():
        status = "Not found"
    else:
        status = "Failed"

    return {
        "index": index,
        "code": code,
        "encoded": encoded,
        "ok": False,
        "pdf_bytes": None,
        "used_url": first_url,
        "profile": "",
        "status": status,
        "error": error_text,
    }


def split_work_round_robin(codes: List[str], workers: int) -> List[List[Tuple[int, str]]]:
    chunks: List[List[Tuple[int, str]]] = [[] for _ in range(workers)]

    for index, code in enumerate(codes):
        chunks[index % workers].append((index, code))

    return [chunk for chunk in chunks if chunk]


def download_chunk_with_one_browser(
    chunk: List[Tuple[int, str]],
    selected_blocks: Sequence[str],
    headless: bool,
    timeout_seconds: int,
    on_result: Optional[Callable[[Dict[str, object]], None]] = None,
) -> List[Dict[str, object]]:
    """Download a chunk with one Chrome browser.

    Important fix:
    If one item fails, Chrome is restarted before the next item. This prevents
    a stale Page Not Found or login page from causing every later item to fail.
    """
    results: List[Dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="phoenix_contact_downloads_") as tmp:
        download_dir = Path(tmp)
        driver = create_selenium_driver(download_dir, headless=headless)

        try:
            for index, code in chunk:
                try:
                    result = process_code_with_driver(
                        driver=driver,
                        download_dir=download_dir,
                        index=index,
                        code=code,
                        selected_blocks=selected_blocks,
                        timeout_seconds=timeout_seconds,
                    )
                except Exception as exc:
                    fallback_url = build_candidate_pdf_urls(code, selected_blocks)[0][0]
                    result = {
                        "index": index,
                        "code": code,
                        "encoded": encode_item_number_for_phoenix(code),
                        "ok": False,
                        "pdf_bytes": None,
                        "used_url": fallback_url,
                        "profile": "",
                        "status": "Failed",
                        "error": str(exc),
                    }

                results.append(result)

                if on_result is not None:
                    on_result(result)

                # Reset the browser after a failed item so the next item starts clean.
                if not result.get("ok"):
                    try:
                        driver.quit()
                    except Exception:
                        pass

                    driver = create_selenium_driver(download_dir, headless=headless)

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    return results


def download_phoenix_datasheets(
    codes: List[str],
    selected_blocks: Sequence[str],
    headless: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    browser_workers: int = 1,
    on_result: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, Dict[str, object]]:
    """Download every code and return {code: result}, as the Phoenix app's download_pdfs did.

    on_result is called on the calling thread once per finished item, so it
    may update the Streamlit page.
    """
    if not codes:
        return {}

    workers = max(1, min(int(browser_workers), len(codes)))
    chunks = split_work_round_robin(codes, workers)
    results: List[Dict[str, object]] = []

    if workers == 1:
        results = download_chunk_with_one_browser(
            chunk=chunks[0],
            selected_blocks=selected_blocks,
            headless=headless,
            timeout_seconds=timeout_seconds,
            on_result=on_result,
        )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    download_chunk_with_one_browser,
                    chunk,
                    selected_blocks,
                    headless,
                    timeout_seconds,
                )
                for chunk in chunks
            ]

            for future in as_completed(futures):
                chunk_results = future.result()
                results.extend(chunk_results)

                if on_result is not None:
                    for result in chunk_results:
                        on_result(result)

    return {str(result["code"]): result for result in results}
