"""Carlo Gavazzi datasheet downloads.

Every product has a page at
https://www.gavazziautomation.com/en-global/product/<code> whose downloads
list the datasheet in several languages, e.g.
https://www.gavazziautomation.com/fileadmin/images/PIM/DATASHEET/ENG/<family>_DS_ENG.pdf

The datasheet file is named after the product family, not the order code, so
the link is read from the product page and the English version is preferred.
"""
import base64
import re
from typing import List, Tuple
from urllib.parse import quote, urljoin

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pdf_pack import is_valid_pdf_bytes, trim_to_pdf_start

PRODUCT_URL = "https://www.gavazziautomation.com/en-global/product/{code}"
DATASHEET_LINK_SELECTOR = "a[href*='datasheet' i]"
ENGLISH_MARKERS = ("/eng/", "_eng.", "dseng.", "-en.", "_en.")

# Runs inside the product page: download the PDF with the browser's own
# network stack (same cookies and headers as the page) and return base64.
FETCH_AS_BASE64_JS = """
async (url) => {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) {
        throw new Error("HTTP " + response.status);
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
}
"""


def first_line(error: Exception) -> str:
    """Playwright errors carry a long call log; keep the message only."""
    return (str(error).strip().splitlines() or [type(error).__name__])[0]


def gavazzi_product_url(code: str) -> str:
    return PRODUCT_URL.format(code=quote(code, safe=""))


async def open_product_page(page, url: str, retries: int = 3):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            print(f"Opening: {url} (attempt {attempt}/{retries})")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # The downloads list can be rendered after the HTML arrives.
            try:
                await page.wait_for_selector(DATASHEET_LINK_SELECTOR, state="attached", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            return response
        except Exception as e:
            last_error = e
            print(f"Goto failed: {e}")
            await page.wait_for_timeout(1200)

    raise last_error


async def collect_pdf_links(page) -> List[Tuple[str, str]]:
    """Every PDF the page links to, as (absolute url, link text), in page order."""
    links = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => [e.href, (e.innerText || e.title || '').trim()])",
    )

    # Also catch PDF paths that only appear in the page source (data
    # attributes, embedded JSON), where JSON escapes "/" as "\/".
    html = await page.content()
    for raw in re.findall(r"""[^"'\s<>()]+?\.pdf""", html, flags=re.I):
        links.append([urljoin(page.url, raw.replace("\\/", "/")), ""])

    seen = set()
    result = []

    for href, text in links:
        url = (href or "").split("#", 1)[0]
        if not re.search(r"\.pdf($|\?)", url, flags=re.I) or url in seen:
            continue
        seen.add(url)
        result.append((url, text or ""))

    return result


def pick_datasheet_urls(links: List[Tuple[str, str]]) -> List[str]:
    """Datasheet links only, English first, otherwise in page order."""
    datasheets = [
        (url, text)
        for url, text in links
        if "/datasheet/" in url.lower() or re.search(r"data\s*sheet", text, flags=re.I)
    ]

    def is_english(url: str, text: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in ENGLISH_MARKERS) or "english" in text.lower()

    english = [url for url, text in datasheets if is_english(url, text)]
    others = [url for url, text in datasheets if not is_english(url, text)]
    return english + others


async def fetch_pdf(page, url: str) -> bytes:
    problems = []

    try:
        response = await page.context.request.get(url, timeout=60000)
        body = await response.body()
        if response.ok and is_valid_pdf_bytes(body):
            return trim_to_pdf_start(body)
        problems.append(f"direct download returned HTTP {response.status}")
    except Exception as e:
        problems.append(f"direct download failed: {first_line(e)}")

    try:
        body = base64.b64decode(await page.evaluate(FETCH_AS_BASE64_JS, url))
        if is_valid_pdf_bytes(body):
            return trim_to_pdf_start(body)
        problems.append("in-page download did not return a PDF")
    except Exception as e:
        problems.append(f"in-page download failed: {first_line(e)}")

    raise RuntimeError(f"Could not download {url} ({'; '.join(problems)})")


async def download_gavazzi_pdf_from_page(page, code: str) -> Tuple[bytes, str]:
    """Return (pdf_bytes, datasheet_url) for one Carlo Gavazzi product code."""
    url = gavazzi_product_url(code)
    response = await open_product_page(page, url)

    links = await collect_pdf_links(page)
    datasheet_urls = pick_datasheet_urls(links)

    if not datasheet_urls:
        status = response.status if response is not None else "no response"
        other_pdfs = [link for link, _ in links][:5]
        raise RuntimeError(
            f"No datasheet link found on the Carlo Gavazzi product page "
            f"(HTTP {status}, page '{await page.title()}' at {page.url}). "
            f"Other PDFs on the page: {other_pdfs or 'none'}"
        )

    last_error = None
    for datasheet_url in datasheet_urls[:3]:
        try:
            return await fetch_pdf(page, datasheet_url), datasheet_url
        except Exception as e:
            last_error = first_line(e)
            print(f"{code}: {e}")

    raise RuntimeError(f"Datasheet link found but the download failed. Last error: {last_error}")
