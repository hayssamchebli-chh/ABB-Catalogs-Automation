"""ABB datasheet downloads.

Opens https://new.abb.com/products/<code> in Firefox (Playwright) and clicks
the product page's print button, which makes ABB generate and download the
product datasheet PDF.
"""
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pdf_pack import is_valid_pdf_bytes

BASE_URL = "https://new.abb.com/products/{item_code}"


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

            button = await find_print_button(page)
            if button is None:
                raise RuntimeError(
                    "print/datasheet button not found - "
                    + await describe_page_buttons(page)
                )

            async with page.expect_download(timeout=90000) as download_info:
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
