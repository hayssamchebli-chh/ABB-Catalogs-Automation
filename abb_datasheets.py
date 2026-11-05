"""ABB datasheet downloads.

Opens https://new.abb.com/products/<code> in Firefox (Playwright) and clicks
the product page's print button, which makes ABB generate and download the
product datasheet PDF.
"""
import asyncio
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pdf_pack import is_valid_pdf_bytes

BASE_URL = "https://new.abb.com/products/{item_code}"
DOWNLOAD_TIMEOUT_MS = 90000  # how long one click may wait for ABB to generate and send the PDF


def abb_product_url(item_code: str) -> str:
    return BASE_URL.format(item_code=item_code)


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


async def find_print_button(page):
    """Return the datasheet button on a product page, or None.

    Most product pages label it "Print to PDF", but some templates (for example
    the low-voltage SACE breakers) show only a printer icon whose accessible
    name differs, so fall back to any print-looking button or link.
    """
    candidates = [
        page.get_by_role("button", name="Print to PDF"),
        page.get_by_role("button", name=re.compile(r"print|pdf|datasheet", re.I)),
        page.get_by_role("link", name=re.compile(r"print to pdf|datasheet", re.I)),
        page.locator(
            "[aria-label*='print' i], [title*='print' i], "
            "button:has(svg[class*='print' i]), button:has(use[href*='print' i]), "
            "button[class*='print' i], a[class*='print' i]"
        ),
    ]

    for locator in candidates:
        try:
            if await locator.count() > 0:
                return locator.first
        except Exception:
            pass

    return None


async def describe_page_buttons(page) -> str:
    """Short list of the buttons on the page, to explain a missing print button."""
    try:
        buttons = await page.eval_on_selector_all(
            "button, [role='button']",
            """els => els.slice(0, 15).map(e =>
                (e.getAttribute('aria-label') || e.getAttribute('title') ||
                 e.innerText || e.className || e.tagName).toString().trim().slice(0, 40)
            )""",
        )
        return f"page '{await page.title()}' at {page.url}; buttons seen: {buttons}"
    except Exception as e:
        return f"could not inspect page ({e})"


def first_line(error: Exception) -> str:
    """Playwright errors carry a long call log; keep the message only."""
    return (str(error).strip().splitlines() or [type(error).__name__])[0][:1200]


class DownloadWatcher:
    """Everything that can deliver the datasheet after the print button is clicked.

    A download that starts on this page or on a tab the click opens, or a PDF
    response that arrives without being a download. It also keeps a short log
    of what the page did, so a failure can say what ABB answered.
    """

    def __init__(self, page):
        loop = asyncio.get_running_loop()
        self.download = loop.create_future()
        self.pdf_response = loop.create_future()
        self.log = []
        self._watched = []
        self._page = page
        self._watch(page)
        page.on("popup", self._on_popup)

    def _watch(self, page):
        page.on("download", self._on_download)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        self._watched.append(page)

    def _note(self, line: str):
        if len(self.log) < 12:
            self.log.append(line)

    def _on_popup(self, popup):
        self._note(f"the click opened a new tab ({popup.url[:100] or 'blank'})")
        self._watch(popup)

    def _on_download(self, download):
        if not self.download.done():
            self.download.set_result(download)

    def _on_response(self, response):
        try:
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        except Exception:
            content_type = ""

        url = response.url
        is_pdf = content_type == "application/pdf"

        if is_pdf or response.status >= 400 or any(word in url.lower() for word in ("pdf", "generate", "download")):
            self._note(f"{response.status} {content_type or '?'} {url[:110]}")

        if is_pdf and response.status == 200 and not self.pdf_response.done():
            self.pdf_response.set_result(response)

    def _on_request_failed(self, request):
        self._note(f"request failed: {request.url[:100]} ({request.failure})")

    def summary(self) -> str:
        if not self.log:
            return "The page made no request that looks like PDF generation."
        return "After the click: " + " | ".join(self.log[:6])

    async def wait(self, timeout_ms: int):
        """Return ("download", Download), ("pdf", Response) or (None, None) on timeout."""
        await asyncio.wait(
            {self.download, self.pdf_response},
            timeout=timeout_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if self.download.done():
            return "download", self.download.result()
        if self.pdf_response.done():
            return "pdf", self.pdf_response.result()
        return None, None

    def close(self):
        for page in self._watched:
            for event, handler in (
                ("download", self._on_download),
                ("response", self._on_response),
                ("requestfailed", self._on_request_failed),
            ):
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass

        try:
            self._page.remove_listener("popup", self._on_popup)
        except Exception:
            pass


async def describe_button(button) -> str:
    try:
        return await button.evaluate(
            """el => (el.getAttribute('aria-label') || el.title || el.innerText || el.className
                      || el.tagName).toString().trim().slice(0, 50)"""
        )
    except Exception:
        return "print button"


async def read_pdf_response(page, response) -> bytes:
    """The PDF behind a response that did not come as a download."""
    try:
        body = await response.body()
        if is_valid_pdf_bytes(body):
            return body
    except Exception:
        pass

    # Some bodies are not readable once the browser has handed them to its
    # viewer: ask for the same address again, with the page's own cookies.
    fetched = await page.context.request.get(response.url, timeout=60000)
    return await fetched.body()


async def download_abb_pdf_from_page(page, item_code: str, output_dir: str, click_attempts: int = 3):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    url = BASE_URL.format(item_code=item_code)
    await safe_goto(page, url)

    # ABB generates some product PDFs on demand (PisWebApi Pdf/Generate) and
    # the first generation can take longer than one wait window. The result
    # is cached server-side, so retrying the click usually succeeds even when
    # the first attempt times out.
    final_path = None
    pdf_bytes = None
    last_error = None

    for attempt in range(1, click_attempts + 1):
        watcher = None

        try:
            await neutralize_cookie_overlay(page)

            button = await find_print_button(page)
            if button is None:
                raise RuntimeError(
                    "print/datasheet button not found - "
                    + await describe_page_buttons(page)
                )

            watcher = DownloadWatcher(page)
            clicked = await describe_button(button)

            try:
                await button.click(timeout=10000)
            except Exception:
                # An overlay is still eating the click: trigger the
                # button's handler directly, which no overlay can block.
                await button.evaluate("el => el.click()")

            kind, delivered = await watcher.wait(DOWNLOAD_TIMEOUT_MS)

            if kind is None:
                raise RuntimeError(
                    f"Timeout {DOWNLOAD_TIMEOUT_MS}ms exceeded: nothing was downloaded after "
                    f"clicking '{clicked}'. {watcher.summary()} Now: {await describe_page_buttons(page)}"
                )

            if kind == "download":
                suggested_name = delivered.suggested_filename
                if not suggested_name.lower().endswith(".pdf"):
                    suggested_name = f"{item_code}.pdf"

                final_path = output_path / suggested_name
                await delivered.save_as(str(final_path))
                pdf_bytes = final_path.read_bytes()
            else:
                pdf_bytes = await read_pdf_response(page, delivered)
                final_path = output_path / f"{item_code}.pdf"
                final_path.write_bytes(pdf_bytes)

            if not is_valid_pdf_bytes(pdf_bytes):
                pdf_bytes = None
                raise ValueError(f"The file ABB sent for {item_code} is not a valid PDF.")

            break
        except Exception as e:
            last_error = e
            print(f"{item_code}: no download on attempt {attempt}/{click_attempts} ({first_line(e)})")

            if attempt < click_attempts:
                try:
                    await page.reload(wait_until="load", timeout=60000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
        finally:
            if watcher is not None:
                watcher.close()

    if pdf_bytes is None:
        raise RuntimeError(
            f"PDF was not generated after {click_attempts} attempt{'' if click_attempts == 1 else 's'}. The ABB PDF "
            f"generator can be slow for this product; running the same code again "
            f"usually works because the PDF gets cached. Last error: {first_line(last_error)}"
        )

    return final_path, pdf_bytes
