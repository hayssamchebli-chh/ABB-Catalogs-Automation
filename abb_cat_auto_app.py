from collections import Counter

import pandas as pd
import streamlit as st

from item_codes import (
    SUPPLIER_NAMES,
    drop_untyped_duplicates,
    guess_excel_columns,
    items_from_excel,
    items_from_text,
    read_excel_file,
)
from pdf_pack import ensure_pdf_filename, get_front_cover_bytes
from phoenix_datasheets import (
    DEFAULT_SELECTED_BLOCK_LABELS,
    DEFAULT_TIMEOUT_SECONDS,
    PDF_BLOCK_OPTIONS,
)
from pipeline import build_pack, download_all

# ---------------------------
# Page config
# ---------------------------
st.set_page_config(
    page_title="ABB·PHX·GAV Pack Builder",
    page_icon="📕",
    layout="centered",
)


# ---------------------------
# Custom CSS (Harb Electric blue)
# ---------------------------
st.markdown(
    """
    <style>
        :root {
            --brand-blue: #1f4ea1;
            --brand-blue-dark: #163a7a;
            --brand-charcoal: #1f1f1f;
            --brand-bg: #f5f6f8;
            --brand-card: #ffffff;
            --brand-border: #d9dde3;
            --brand-muted: #5f6670;
            --brand-soft-blue: #eef3fb;
        }

        .stApp {
            background-color: var(--brand-bg);
        }

        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 1000px;
        }

        .hero-card {
            background: linear-gradient(135deg, var(--brand-blue) 0%, var(--brand-blue-dark) 100%);
            color: white;
            border-radius: 18px;
            padding: 1.6rem 1.6rem 1.4rem 1.6rem;
            box-shadow: 0 10px 25px rgba(31, 78, 161, 0.18);
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
            color: #e6edf9;
            line-height: 1.55;
            margin-bottom: 0;
        }

        .supplier-chips {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 1rem;
        }

        .supplier-chip {
            background: rgba(255,255,255,0.14);
            border: 1px solid rgba(255,255,255,0.24);
            border-radius: 999px;
            padding: 0.35rem 0.75rem;
            font-size: 0.85rem;
            font-weight: 700;
        }

        .supplier-chip code {
            background: rgba(0,0,0,0.18);
            color: white;
            border-radius: 6px;
            padding: 0.05rem 0.35rem;
            margin-left: 0.3rem;
            font-size: 0.8rem;
        }

        .section-card {
            background: var(--brand-card);
            border: 1px solid var(--brand-border);
            border-radius: 16px;
            padding: 1.2rem 1.2rem 1rem 1.2rem;
            box-shadow: 0 6px 18px rgba(0, 0, 0, 0.04);
            margin: 0.6rem 0 1rem 0;
        }

        .section-title {
            font-size: 1.05rem;
            font-weight: 700;
            color: var(--brand-charcoal);
            margin-bottom: 0.2rem;
        }

        .section-subtitle {
            font-size: 0.93rem;
            color: var(--brand-muted);
            margin-bottom: 0.2rem;
        }

        .panel-title {
            color: var(--brand-charcoal);
            font-size: 1rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }

        .panel-subtitle {
            color: var(--brand-muted);
            font-size: 0.88rem;
            margin-bottom: 0.8rem;
        }

        div[data-testid="stTextArea"] textarea {
            background-color: #fbfbfb !important;
            border: 1px solid var(--brand-border) !important;
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

        .st-key-excel_upload div[data-testid="stFileUploader"] {
            background: #fbfbfb !important;
            border: 2px dashed #c8c8c8 !important;
            border-radius: 12px !important;
            padding: 24px !important;
            min-height: 220px !important;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .st-key-excel_upload div[data-testid="stFileUploader"] section {
            width: 100%;
        }

        div[data-testid="stTextArea"] label,
        div[data-testid="stTextInput"] label,
        div[data-testid="stCheckbox"] label,
        div[data-testid="stSelectbox"] label,
        div[data-testid="stMultiSelect"] label,
        div[data-testid="stFileUploader"] label {
            color: var(--brand-charcoal) !important;
            font-weight: 700 !important;
        }

        .stButton > button,
        div[data-testid="stDownloadButton"] > button {
            background: var(--brand-blue) !important;
            color: white !important;
            border: none !important;
            border-radius: 12px !important;
            font-weight: 700 !important;
            padding: 0.7rem 1rem !important;
            box-shadow: 0 8px 18px rgba(31, 78, 161, 0.14);
        }

        .stButton > button:hover,
        div[data-testid="stDownloadButton"] > button:hover {
            background: var(--brand-blue-dark) !important;
        }

        .metric-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.9rem;
            margin: 1rem 0 1rem 0;
        }

        .metric-card {
            background: white;
            border: 1px solid var(--brand-border);
            border-radius: 14px;
            padding: 1rem;
            box-shadow: 0 6px 16px rgba(0, 0, 0, 0.04);
        }

        .metric-label {
            color: var(--brand-muted);
            font-size: 0.85rem;
            margin-bottom: 0.35rem;
        }

        .metric-value {
            color: var(--brand-charcoal);
            font-size: 1.7rem;
            font-weight: 800;
            line-height: 1.1;
        }

        .info-note {
            background: var(--brand-soft-blue);
            border: 1px solid #cbd8ef;
            color: var(--brand-charcoal);
            border-radius: 12px;
            padding: 0.85rem 1rem;
            font-size: 0.93rem;
            margin: 0.5rem 0 1rem 0;
        }

        .footer-note {
            text-align: center;
            color: var(--brand-muted);
            font-size: 0.85rem;
            margin-top: 1rem;
        }

        div[data-testid="stExpander"] {
            background: white;
            border: 1px solid var(--brand-border);
            border-radius: 12px;
        }

        @media (max-width: 640px) {
            .metric-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def section_card(title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="section-card">
            <div class="section-title">{title}</div>
            <div class="section-subtitle">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------
# Header
# ---------------------------
st.markdown(
    """
    <div class="hero-card">
        <div class="hero-badge">DATASHEET AUTOMATION TOOL</div>
        <div class="hero-title">ABB·PHX·GAV Pack Builder</div>
        <div class="hero-subtitle">
            Enter item codes from ABB, Phoenix Contact and Carlo Gavazzi, retrieve their
            datasheets automatically, and generate one consolidated PDF pack in your order.
        </div>
        <div class="supplier-chips">
            <div class="supplier-chip">ABB <code>ABB-</code><code>ABS-</code></div>
            <div class="supplier-chip">Phoenix Contact <code>PHX-</code></div>
            <div class="supplier-chip">Carlo Gavazzi <code>GAV-</code></div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------
# Input section
# ---------------------------
section_card(
    "Build your PDF pack",
    "Add codes manually or upload an Excel file with a Type column and a Code column. "
    "The code prefix picks the supplier, and the Type is written on the cover page "
    "inserted before each item's datasheet.",
)

manual_items = []
excel_items = []
excel_df = None

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
            height=50,
            placeholder="Example:\nMotion Sensor\nPower Supply\nEnergy Meter",
        )

    with manual_code_col:
        codes_text = st.text_area(
            "Code",
            height=50,
            placeholder="Example:\nABB-TZW510\nPHX-3010110\nGAV-WM30AV53H",
        )

    manual_items = items_from_text(types_text, codes_text)

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

    with st.container(key="excel_upload"):
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
                columns = list(excel_df.columns)
                guessed_type_col, guessed_code_col = guess_excel_columns(columns)

                type_col = st.selectbox(
                    "Type column",
                    options=[None] + columns,
                    index=([None] + columns).index(guessed_type_col),
                    format_func=lambda column: "(no Type column)" if column is None else str(column),
                )
                code_col = st.selectbox(
                    "Code column",
                    options=columns,
                    index=columns.index(guessed_code_col),
                    format_func=str,
                )

                excel_items = items_from_excel(excel_df, type_col, code_col)
                st.caption(f"{len(excel_items)} item(s) detected from Excel.")
        except Exception as e:
            st.error(f"Could not read Excel file: {e}")

# Full-width Excel preview below the input row
if excel_df is not None and not excel_df.empty:
    st.caption("Excel preview")
    st.dataframe(excel_df.head(10), width="stretch")

all_items = drop_untyped_duplicates(manual_items + excel_items)

if all_items:
    counts = Counter(item["supplier"] for item in all_items)
    summary = " · ".join(
        f"{name}: {counts[supplier]}" for supplier, name in SUPPLIER_NAMES.items() if counts[supplier]
    )

    with st.expander(f"Items in your pack ({len(all_items)}) — {summary}", expanded=False):
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
        )

st.markdown(
    """
    <div class="info-note">
        Codes are routed by prefix: <b>PHX-</b> to Phoenix Contact, <b>GAV-</b> to Carlo Gavazzi,
        and <b>ABB-</b>, <b>ABS-</b> or any other prefix to ABB. The merged PDF starts with a
        table of contents, and each datasheet is preceded by a cover page showing its Type, in
        the order you entered the codes. Repeated codes with a Type each keep their own cover and
        datasheet; repeated codes without a Type are included only once.
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------
# Phoenix Contact PDF content
# ---------------------------
section_card(
    "Phoenix Contact PDF content",
    "Choose what to include in Phoenix Contact datasheets (PHX- codes). By default, "
    "Technical data and Drawings are selected. ABB and Carlo Gavazzi datasheets are "
    "downloaded as published.",
)

block_label_to_key = {label: key for key, label, _ in PDF_BLOCK_OPTIONS}
all_block_labels = [label for _, label, _ in PDF_BLOCK_OPTIONS]

selected_block_labels = st.multiselect(
    "PDF sections",
    options=all_block_labels,
    default=DEFAULT_SELECTED_BLOCK_LABELS,
    help="These selected sections are added to the blocks=... part of both AE and PC PDF URLs.",
)

selected_blocks = [
    block_label_to_key[label]
    for label in all_block_labels
    if label in selected_block_labels
]

with st.expander("Section details", expanded=False):
    for key, label, description in PDF_BLOCK_OPTIONS:
        st.markdown(f"**{label}** (`{key}`): {description}")


# ---------------------------
# Pack settings
# ---------------------------
section_card(
    "Pack settings",
    "Front cover, output file name and download speed.",
)

settings_col1, settings_col2 = st.columns(2)

with settings_col1:
    output_name = st.text_input("Output file name", value="datasheet_pack.pdf")
    include_cover = st.checkbox("Add front cover", value=True)

with settings_col2:
    max_pages = st.selectbox(
        "Parallel pages (ABB and Carlo Gavazzi)",
        options=[2, 3, 4, 5],
        index=1,
    )
    uploaded_cover = st.file_uploader(
        "Use another front cover (optional)",
        type=["pdf"],
        help="Leave empty to use front_cover.pdf from the repository when the front cover is enabled.",
    )

with st.expander("Advanced Chrome settings (Phoenix Contact)", expanded=False):
    c1, c2 = st.columns(2)

    with c1:
        headless = st.checkbox(
            "Run Chrome headless",
            value=True,
            help="Use True on Streamlit Cloud. Use False locally to see Chrome.",
        )

        timeout_seconds = st.slider(
            "Timeout",
            min_value=30,
            max_value=180,
            value=DEFAULT_TIMEOUT_SECONDS,
            step=10,
        )

    with c2:
        browser_workers = st.slider(
            "Chrome workers",
            min_value=1,
            max_value=4,
            value=1,
            help="Use one worker on Streamlit Cloud. Use two only if your host has enough RAM.",
        )

run_clicked = st.button("Build PDF Pack", type="primary", width="stretch")


# ---------------------------
# Action / Processing
# ---------------------------
if run_clicked:
    st.session_state.pop("pack", None)
    has_phoenix_items = any(item["supplier"] == "phoenix" for item in all_items)

    if not all_items:
        st.error("Please enter item codes manually or upload an Excel file.")
        st.stop()

    if has_phoenix_items and not selected_blocks:
        st.error("Please select at least one Phoenix Contact PDF section.")
        st.stop()

    front_cover_bytes, cover_problem = get_front_cover_bytes(uploaded_cover, include_cover)

    if cover_problem and uploaded_cover is not None:
        st.error(cover_problem)
        st.stop()

    if cover_problem:
        st.warning(cover_problem)

    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def show_progress(finished: int, total: int, message: str) -> None:
        progress_bar.progress(finished / total if total else 1.0)
        status_text.info(f"{message} ({finished}/{total} done)")

    with st.spinner("Downloading datasheets and building the merged pack..."):
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
            st.session_state["pack"] = pack
        except Exception as e:
            st.error(f"Processing failed: {e}")

    progress_bar.empty()
    status_text.empty()


# ---------------------------
# Results (kept across reruns so every download button keeps working)
# ---------------------------
pack = st.session_state.get("pack")

if pack:
    st.markdown(
        f"""
        <div class="metric-grid">
            <div class="metric-card">
                <div class="metric-label">Submitted</div>
                <div class="metric-value">{pack["submitted_count"]}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Downloaded</div>
                <div class="metric-value">{pack["downloaded_count"]}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Failed</div>
                <div class="metric-value">{pack["failed_count"]}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if pack["merged_pdf"] is None:
        st.error("No PDFs were downloaded, so no merged file could be created.")
    else:
        st.success("Your consolidated datasheet pack is ready.")

        st.download_button(
            label="Download Merged PDF",
            data=pack["merged_pdf"],
            file_name=pack["file_name"],
            mime="application/pdf",
            width="stretch",
            on_click="ignore",
        )

    if pack["failed_rows"]:
        with st.expander("Items not downloaded", expanded=True):
            st.dataframe(pack["failed_rows"], hide_index=True, width="stretch")

    if pack["success_rows"]:
        with st.expander("Downloaded items", expanded=False):
            st.dataframe(pack["success_rows"], hide_index=True, width="stretch")

    report_rows = pack["success_rows"] + pack["failed_rows"]
    if report_rows:
        report_df = pd.DataFrame(report_rows).sort_values("#")
        st.download_button(
            "Download Report CSV",
            data=report_df.to_csv(index=False).encode("utf-8"),
            file_name="datasheet_download_report.csv",
            mime="text/csv",
            width="stretch",
            on_click="ignore",
        )

st.markdown(
    """
    <div class="footer-note">
        Built for fast retrieval and packaging of ABB, Phoenix Contact and Carlo Gavazzi product documentation.
    </div>
    """,
    unsafe_allow_html=True,
)
