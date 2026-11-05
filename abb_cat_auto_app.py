import html
import re
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from item_codes import (
    EXCEL_EXAMPLE_HEADERS,
    EXCEL_EXAMPLE_ROWS,
    SUPPLIER_NAMES,
    build_excel_template,
    drop_untyped_duplicates,
    guess_excel_columns,
    items_from_excel,
    items_from_text,
    read_excel_file,
    supplier_guesses,
)
from pdf_pack import ensure_pdf_filename, get_front_cover_bytes
from phoenix_datasheets import (
    DEFAULT_SELECTED_BLOCK_LABELS,
    DEFAULT_TIMEOUT_SECONDS,
    PDF_BLOCK_OPTIONS,
)
from pipeline import build_pack, download_all, format_duration

APP_DIR = Path(__file__).resolve().parent

EXAMPLE_TYPES = "Breaker 1000A\nBreaker 800A\nPower supply\nEnergy meter"
EXAMPLE_CODES = "ABS-1SDA100720R1\nABS-1SDA100719R1\nPHX-3010110\nGAV-WM30AV53H"

# ---------------------------
# Page config + style
# ---------------------------
st.set_page_config(
    page_title="ABB·PHX·GAV Pack Builder",
    page_icon="📕",
    layout="centered",
)

st.markdown(
    f"<style>{(APP_DIR / 'app_style.css').read_text(encoding='utf-8')}</style>",
    unsafe_allow_html=True,
)


def step_header(number: int, title: str, subtitle: str = "") -> None:
    st.markdown(
        f"""
        <div class="step-head">
            <div class="step-num">{number}</div>
            <div>
                <div class="step-title">{title}</div>
                <div class="step-sub">{subtitle}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def short_reason(error: str, status: str) -> str:
    """One readable line for the failed-items table (the full text is kept below it)."""
    text = (error or "").lower()

    if "no datasheet link" in text:
        return "No datasheet link on the supplier's product page"
    if "datasheet link found but the download failed" in text:
        return "Datasheet link found, but the download failed"
    if "print/datasheet button not found" in text:
        return "No print button found on the ABB product page"
    if "pdf was not generated" in text:
        error_status = re.search(r"(?:after the click: |\| )([45]\d\d) \S+ http", text)
        if error_status:
            return f"ABB's PDF service answered with an error (HTTP {error_status.group(1)})"
        if "no request that looks like pdf generation" in text:
            return "Clicking the print button did nothing (no PDF request was sent)"
        if "opened a new tab" in text:
            return "The print button opened a new tab, but no PDF arrived"
        if "timeout" in text:
            return "ABB did not send the PDF in time (it was tried again at the end of the run)"
        return "ABB gave no PDF for this item"
    if status == "Auth required" or "sign-in" in text:
        return "Phoenix Contact asked for a login"
    if status == "Not found" or "page-not-found" in text:
        return "Product not found on the supplier's site"
    if "did not finish within" in text:
        return "Phoenix Contact download timed out (try a longer timeout under Advanced)"
    if "firefox could not run" in text or "chrome/selenium failed" in text:
        return "The download browser could not start"

    first_line = (error or status or "Unknown error").strip().splitlines()[0]
    return first_line if len(first_line) <= 90 else first_line[:87] + "..."


def sample_sheet_html() -> str:
    """A small spreadsheet picture: column letters, row numbers, header row, example rows."""
    header_cells = "".join(f"<td>{html.escape(name)}</td>" for name in EXCEL_EXAMPLE_HEADERS)
    body_rows = "".join(
        f"<tr><th>{number}</th>"
        + "".join(f"<td>{html.escape(value)}</td>" for value in row)
        + "</tr>"
        for number, row in enumerate(EXCEL_EXAMPLE_ROWS, start=2)
    )
    return (
        '<div class="sheet-example-wrap"><table class="sheet-example">'
        "<thead><tr><th></th><th>A</th><th>B</th></tr></thead>"
        f'<tbody><tr class="head"><th>1</th>{header_cells}</tr>{body_rows}</tbody>'
        "</table></div>"
    )


def use_example() -> None:
    st.session_state["types_text"] = EXAMPLE_TYPES
    st.session_state["codes_text"] = EXAMPLE_CODES


def clear_pasted() -> None:
    st.session_state["types_text"] = ""
    st.session_state["codes_text"] = ""


# ---------------------------
# Header
# ---------------------------
st.markdown(
    """
    <div class="hero">
        <div class="hero-title">ABB·PHX·GAV Pack Builder</div>
        <div class="hero-subtitle">
            Paste or upload item codes and get one merged datasheet PDF, in your order.
        </div>
        <div class="supplier-chips">
            <div class="supplier-chip"><span class="dot abb"></span>ABB <code>ABB-</code><code>ABS-</code></div>
            <div class="supplier-chip"><span class="dot phoenix"></span>Phoenix Contact <code>PHX-</code></div>
            <div class="supplier-chip"><span class="dot gavazzi"></span>Carlo Gavazzi <code>GAV-</code></div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------
# Step 1: add items
# ---------------------------
manual_items = []
excel_items = []
excel_df = None

with st.container(border=True, key="step_add"):
    step_header(
        1,
        "Add your items",
        "Start each code with its supplier prefix: ABB-, PHX- or GAV-.",
    )

    paste_tab, excel_tab = st.tabs([":material/edit_note: Paste codes", ":material/table_chart: Upload Excel"])

    with paste_tab:
        type_col, code_col = st.columns(2)

        with type_col:
            types_text = st.text_area(
                "Type (optional)",
                key="types_text",
                height=220,
                placeholder="Breaker 1000A\nBreaker 800A\nPower supply",
                help="Printed on the cover page before the datasheet. Line 1 of Type goes with line 1 of Code.",
            )

        with code_col:
            codes_text = st.text_area(
                "Code",
                key="codes_text",
                height=220,
                placeholder="ABB-1SDA100720R1\nABB-1SDA100719R1\nPHX-3010110",
                help="One item per line.",
            )

        manual_items = items_from_text(types_text, codes_text)

        example_col, clear_col, _ = st.columns([1.2, 1, 3])
        example_col.button("Fill an example", on_click=use_example, width="stretch")
        clear_col.button("Clear", on_click=clear_pasted, width="stretch")

    with excel_tab:
        with st.expander(
            "What should the Excel file look like?",
            expanded=st.session_state.get("excel_file") is None,
        ):
            st.markdown(sample_sheet_html(), unsafe_allow_html=True)
            st.markdown(
                """
- **Row 1 holds the column names**, and only the first sheet is read.
- **Name the columns `Type` and `Code`.** Type is optional: it is printed on the item's cover page and in the table of contents.
- **Other names work too.** The app looks for a code column named like `Code`, `Item`, `Order No`, `Material`, `Part Number` or `Product Number`, and a type column containing `Type`. If none match, column 1 is the Type and column 2 the Code. You can change the choice after uploading.
- **Start every code with its supplier prefix** (`ABB-`, `PHX-`, `GAV-`), one item per row. The pack follows the row order.
                """
            )
            st.download_button(
                "Download an Excel template",
                data=build_excel_template(),
                file_name="datasheet_items_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                icon=":material/download:",
                on_click="ignore",
            )

        uploaded_excel = st.file_uploader(
            "Excel file",
            type=["xlsx", "xls"],
            key="excel_file",
            help="One column with the codes, and optionally one with the Type.",
        )

        if uploaded_excel is not None:
            try:
                excel_df = read_excel_file(uploaded_excel)

                if excel_df.empty:
                    st.warning("The uploaded Excel file is empty.")
                else:
                    columns = list(excel_df.columns)
                    guessed_type_col, guessed_code_col = guess_excel_columns(columns)
                    type_options = [None] + columns

                    pick_type_col, pick_code_col = st.columns(2)
                    type_column = pick_type_col.selectbox(
                        "Type column",
                        options=type_options,
                        index=type_options.index(guessed_type_col),
                        format_func=lambda column: "(none)" if column is None else str(column),
                    )
                    code_column = pick_code_col.selectbox(
                        "Code column",
                        options=columns,
                        index=columns.index(guessed_code_col),
                        format_func=str,
                    )

                    excel_items = items_from_excel(excel_df, type_column, code_column)
                    st.caption(f"{len(excel_items)} item(s) read from the Excel file.")

                    with st.expander("Preview the first rows"):
                        st.dataframe(excel_df.head(10), width="stretch")
            except Exception as e:
                st.error(f"Could not read Excel file: {e}")

    with st.expander("How codes are read"):
        st.markdown(
            """
- **The prefix picks the supplier:** `ABB-` or `ABS-` for ABB, `PHX-` for Phoenix Contact, `GAV-` for Carlo Gavazzi.
- **No prefix:** a bare number goes to Phoenix Contact, anything else to ABB. The app warns you when that happens in a list that otherwise has prefixes.
- **Separators:** one item per line, or separate codes with commas or semicolons. Spaces stay inside a code, so `GAV-MOR 05` is one item.
- **Order:** the pack follows the order you entered the items, pasted codes first, then the Excel file.
- **Duplicates:** a repeated code with a Type keeps its own cover and datasheet; a repeated code without a Type appears once.
            """
        )

all_items = drop_untyped_duplicates(manual_items + excel_items)


# ---------------------------
# Step 2: check the pack
# ---------------------------
with st.container(border=True, key="step_review"):
    step_header(
        2,
        "Check your pack",
        "Make sure every code went to the right supplier before you build.",
    )

    if not all_items:
        st.markdown(
            '<div class="empty-hint">Nothing here yet. Add codes or upload a file above.</div>',
            unsafe_allow_html=True,
        )
    else:
        counts = Counter(item["supplier"] for item in all_items)
        tiles = "".join(
            f'<div class="tile"><div class="tile-num">{counts[supplier]}</div>'
            f'<div class="tile-label"><span class="dot {supplier}"></span>{name}</div></div>'
            for supplier, name in SUPPLIER_NAMES.items()
            if counts[supplier]
        )
        total_label = "datasheet" if len(all_items) == 1 else "datasheets"
        st.markdown(
            f'<div class="tile-row"><div class="tile total"><div class="tile-num">{len(all_items)}</div>'
            f'<div class="tile-label">{total_label} in the pack</div></div>{tiles}</div>',
            unsafe_allow_html=True,
        )

        guessed_items = supplier_guesses(all_items)
        if guessed_items:
            shown = "\n".join(
                f"- `{item['input']}` will be looked up on **{SUPPLIER_NAMES[item['supplier']]}**"
                for item in guessed_items[:10]
            )
            more = f"\n- ...and {len(guessed_items) - 10} more" if len(guessed_items) > 10 else ""
            st.warning(
                f"{len(guessed_items)} code(s) have no supplier prefix, so the supplier was guessed. "
                f"If a guess is wrong, add `ABB-`, `PHX-` or `GAV-` in front of the code.\n\n{shown}{more}"
            )

        with st.expander(f"Review all {len(all_items)} item(s)", expanded=len(all_items) <= 12):
            st.dataframe(
                [
                    {
                        "#": position,
                        "Supplier": SUPPLIER_NAMES[item["supplier"]],
                        "Code": item["code"],
                        "Type": item["type"],
                        "Entered as": item["input"],
                    }
                    for position, item in enumerate(all_items, start=1)
                ],
                hide_index=True,
                width="stretch",
                height=min(38 + 35 * len(all_items), 330),
            )


# ---------------------------
# Step 3: options
# ---------------------------
has_phoenix_items = any(item["supplier"] == "phoenix" for item in all_items)

block_label_to_key = {label: key for key, label, _ in PDF_BLOCK_OPTIONS}
all_block_labels = [label for _, label, _ in PDF_BLOCK_OPTIONS]

with st.container(border=True, key="step_options"):
    step_header(3, "Choose options", "The defaults work for most packs.")

    name_col, cover_col = st.columns([3, 2], vertical_alignment="bottom")
    output_name = name_col.text_input("Output file name", value="datasheet_pack.pdf")
    include_cover = cover_col.toggle(
        "Add front cover",
        value=True,
        help="A cover page before the table of contents.",
    )

    with st.expander("Phoenix Contact: what to include in each datasheet", expanded=has_phoenix_items):
        st.caption(
            "Applies to PHX- codes only. ABB and Carlo Gavazzi datasheets are downloaded as published."
        )
        selected_block_labels = st.multiselect(
            "Sections to include",
            options=all_block_labels,
            default=DEFAULT_SELECTED_BLOCK_LABELS,
            help="These sections are added to the blocks=... part of both the AE and PC PDF URLs.",
        )
        st.caption(
            "\n".join(f"- **{label}**: {description}" for _, label, description in PDF_BLOCK_OPTIONS)
        )

    with st.expander("Advanced"):
        uploaded_cover = st.file_uploader(
            "Use another front cover (optional)",
            type=["pdf"],
            help="Leave empty to use front_cover.pdf from the repository when the front cover is on.",
        )

        speed_col, timeout_col, workers_col = st.columns(3)
        max_pages = speed_col.selectbox(
            "Parallel pages",
            options=[2, 3, 4, 5],
            index=1,
            help="For ABB and Carlo Gavazzi downloads.",
        )
        timeout_seconds = timeout_col.slider(
            "Chrome timeout (s)",
            min_value=30,
            max_value=180,
            value=DEFAULT_TIMEOUT_SECONDS,
            step=10,
            help="Phoenix Contact downloads only.",
        )
        browser_workers = workers_col.slider(
            "Chrome workers",
            min_value=1,
            max_value=4,
            value=1,
            help="Phoenix Contact downloads only. Use 1 on Streamlit Cloud; 2 only if the host has enough RAM.",
        )
        headless = st.checkbox(
            "Run Chrome headless",
            value=True,
            help="Keep on for Streamlit Cloud. Turn off locally to watch Chrome.",
        )

selected_blocks = [
    block_label_to_key[label]
    for label in all_block_labels
    if label in selected_block_labels
]


# ---------------------------
# Step 4: build and download
# ---------------------------
def render_results(pack: dict) -> None:
    downloaded, submitted = pack["downloaded_count"], pack["submitted_count"]
    seconds = pack.get("build_seconds")
    took = f" in {format_duration(seconds)}" if seconds is not None else ""

    if pack["merged_pdf"] is None:
        after = f" after {format_duration(seconds)}" if seconds is not None and seconds >= 1 else ""
        st.error(f"No datasheets could be downloaded{after}, so no pack was created. See the reasons below.")
    elif pack["failed_count"]:
        st.warning(
            f"Your pack is ready with {downloaded} of {submitted} datasheets, built{took}. "
            f"{pack['failed_count']} could not be downloaded (see below)."
        )
    else:
        st.success(f"Your pack is ready: all {submitted} datasheets downloaded and built{took}.")

    pdf_col, csv_col = st.columns([3, 2])

    if pack["merged_pdf"] is not None:
        pdf_col.download_button(
            "Download merged PDF",
            data=pack["merged_pdf"],
            file_name=pack["file_name"],
            mime="application/pdf",
            type="primary",
            icon=":material/download:",
            width="stretch",
            on_click="ignore",
        )

    report_rows = pack["success_rows"] + pack["failed_rows"]
    if report_rows:
        report_df = pd.DataFrame(report_rows).sort_values("#")
        csv_col.download_button(
            "Download report (CSV)",
            data=report_df.to_csv(index=False).encode("utf-8"),
            file_name="datasheet_download_report.csv",
            mime="text/csv",
            icon=":material/table_view:",
            width="stretch",
            on_click="ignore",
        )

    if pack["failed_rows"]:
        with st.expander(f"{pack['failed_count']} item(s) not downloaded", expanded=True):
            st.dataframe(
                [
                    {
                        "#": row["#"],
                        "Supplier": row["Supplier"],
                        "Code": row["Code"],
                        "Type": row["Type"],
                        "Reason": short_reason(row["Error"], row["Status"]),
                    }
                    for row in pack["failed_rows"]
                ],
                hide_index=True,
                width="stretch",
                height=min(38 + 35 * len(pack["failed_rows"]), 330),
            )
            st.caption("To retry only these, fix any typos and paste them back into step 1:")
            st.code("\n".join(pack["retry_codes"]), language=None)

        with st.expander("Full error messages"):
            st.dataframe(
                [
                    {"#": row["#"], "Code": row["Code"], "Error": row["Error"], "Source": row["Source"]}
                    for row in pack["failed_rows"]
                ],
                hide_index=True,
                width="stretch",
            )

    if pack["success_rows"]:
        with st.expander(f"{downloaded} item(s) downloaded"):
            st.dataframe(pack["success_rows"], hide_index=True, width="stretch")


with st.container(border=True, key="step_build"):
    step_header(4, "Build and download", "Downloading takes a few seconds per datasheet.")

    item_word = "datasheet" if len(all_items) == 1 else "datasheets"
    run_clicked = st.button(
        f"Build pack · {len(all_items)} {item_word}" if all_items else "Build pack",
        type="primary",
        width="stretch",
        disabled=not all_items,
        key="build_button",
    )

    if not all_items:
        st.caption("Add at least one code in step 1 to enable this button.")

    if run_clicked:
        st.session_state.pop("pack", None)
        front_cover_bytes, cover_problem = get_front_cover_bytes(uploaded_cover, include_cover)

        if has_phoenix_items and not selected_blocks:
            st.error("Pick at least one Phoenix Contact section in step 3 (Phoenix Contact options).")
        elif cover_problem and uploaded_cover is not None:
            st.error(cover_problem)
        else:
            if cover_problem:
                st.warning(cover_problem)

            progress_bar = st.progress(0.0)
            status_text = st.empty()
            started = time.perf_counter()

            def show_progress(finished: int, total: int, message: str) -> None:
                elapsed = format_duration(time.perf_counter() - started)
                progress_bar.progress(finished / total if total else 1.0)
                status_text.info(f"{message} ({finished}/{total} done, {elapsed} so far)")

            with st.spinner("Downloading datasheets and building the pack..."):
                try:
                    results = download_all(
                        all_items,
                        phoenix_blocks=selected_blocks,
                        chrome_settings={
                            "headless": headless,
                            "timeout_seconds": timeout_seconds,
                            "browser_workers": browser_workers,
                        },
                        max_concurrent_pages=min(max_pages, len(all_items)),
                        on_progress=show_progress,
                    )
                    pack = build_pack(all_items, results, front_cover_bytes)
                    pack["file_name"] = ensure_pdf_filename(output_name)
                    pack["build_seconds"] = time.perf_counter() - started
                    st.session_state["pack"] = pack
                except Exception as e:
                    st.error(f"Processing failed: {e}")

            progress_bar.empty()
            status_text.empty()

    # Results stay on screen across reruns, so a download button never wipes them
    pack = st.session_state.get("pack")
    if pack:
        render_results(pack)

st.markdown(
    '<div class="footer-note">Built for fast retrieval and packaging of ABB, Phoenix Contact '
    "and Carlo Gavazzi product documentation.</div>",
    unsafe_allow_html=True,
)
