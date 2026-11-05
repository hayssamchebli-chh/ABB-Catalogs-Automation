"""Turn pasted lines and Excel rows into supplier-tagged items.

Every item code carries a supplier prefix, e.g. ABS-1SDA100720R1, PHX-3010110
or GAV-WM30AV53H. The prefix decides which supplier website the datasheet is
downloaded from; the rest is the supplier's own product code.
"""
import re
from typing import Iterable, List, Optional, Tuple

import pandas as pd

from phoenix_datasheets import clean_phoenix_code

SUPPLIER_NAMES = {
    "abb": "ABB",
    "phoenix": "Phoenix Contact",
    "gavazzi": "Carlo Gavazzi",
}

# Code prefix -> supplier. Any other prefix is treated as ABB, which is what
# the ABB-only version of the app did with every code.
PREFIX_TO_SUPPLIER = {
    "ABB": "abb",
    "ABS": "abb",
    "PHX": "phoenix",
    "GAV": "gavazzi",
}
DEFAULT_SUPPLIER = "abb"

# Prefix that sends a code back to its supplier when it is typed again
SUPPLIER_PREFIX = {"abb": "ABB-", "phoenix": "PHX-", "gavazzi": "GAV-"}

EMPTY_VALUES = {"", "nan", "none", "null"}


def parse_item_code(value) -> Optional[Tuple[str, str, bool]]:
    """Return (supplier, code, assumed) for one input code, or None when it is empty.

    assumed is True when the code carried no supplier prefix, so the supplier
    was guessed (ABB for text, Phoenix Contact for a bare number).

    - ABS-1SDA100720R1 -> ("abb", "1SDA100720R1", False)
    - PHX-3010110      -> ("phoenix", "3010110", False)
    - GAV-WM30AV53H    -> ("gavazzi", "WM30AV53H", False)
    - GAVWM30AV53H     -> ("gavazzi", "WM30AV53H", False)  hyphen forgotten
    - GAV-MOR 05       -> ("gavazzi", "MOR 05", False)     spaces belong to the code
    - 3010110          -> ("phoenix", "3010110", True)
    - TZW510           -> ("abb", "TZW510", True)
    """
    if value is None:
        return None

    text = str(value).strip()
    if text.lower() in EMPTY_VALUES:
        return None

    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\s*-\s*", "-", text)

    if re.fullmatch(r"(%s)-?" % "|".join(PREFIX_TO_SUPPLIER), text, flags=re.IGNORECASE):
        return None  # a prefix with no code after it

    assumed = False
    match = re.match(r"^([A-Za-z]{2,5})-(.+)$", text)
    unhyphenated = re.match(r"^(GAV|PHX)(?=[A-Za-z0-9])(.+)$", text, flags=re.IGNORECASE)

    if match:
        rest = match.group(2).strip()
        supplier = PREFIX_TO_SUPPLIER.get(match.group(1).upper())
        if supplier is None:
            supplier, assumed = DEFAULT_SUPPLIER, True
    elif unhyphenated:
        supplier = PREFIX_TO_SUPPLIER[unhyphenated.group(1).upper()]
        rest = unhyphenated.group(2).strip()
    elif re.fullmatch(r"[\d,]+(\.\d+)?", text):
        supplier, rest, assumed = "phoenix", text, True
    else:
        supplier, rest, assumed = DEFAULT_SUPPLIER, text, True

    if supplier == "phoenix":
        code = clean_phoenix_code(rest)
    elif supplier == "gavazzi":
        code = re.sub(r"\s+", " ", rest).upper()
    else:
        code = rest

    if not code:
        return None

    return supplier, code, assumed


KNOWN_PREFIX = re.compile(r"^(%s)-" % "|".join(PREFIX_TO_SUPPLIER), flags=re.IGNORECASE)
PHOENIX_LIKE = re.compile(r"^(PHX-)?[\d.]+$", flags=re.IGNORECASE)


def split_codes(cell_text: str) -> List[str]:
    """Split one line or cell into codes.

    Commas, semicolons and line breaks always separate codes. A space only
    does when the next word starts a new code (it has a supplier prefix such
    as PHX-, or it follows a Phoenix Contact number), because some codes
    contain spaces: "GAV-MOR 05" is one code, "PHX-3010110 PHX-3201853" is two.
    """
    text = str(cell_text or "").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\s*-\s*", "-", text)

    codes: List[str] = []

    for segment in re.split(r"[,;\r\n]+", text):
        current = None

        for token in segment.split():
            starts_new_code = (
                current is None
                or KNOWN_PREFIX.match(token)
                or (PHOENIX_LIKE.match(current) and PHOENIX_LIKE.match(token))
            )

            if starts_new_code:
                if current is not None:
                    codes.append(current)
                current = token
            else:
                current = f"{current} {token}"

        if current is not None:
            codes.append(current)

    return codes


def build_items(rows: Iterable[Tuple[str, str]]) -> List[dict]:
    """rows: (type_text, code_text) pairs in pack order.

    A line or cell holding several codes becomes several items that share
    the same Type.
    """
    items = []

    for type_text, code_text in rows:
        for raw_code in split_codes(code_text):
            parsed = parse_item_code(raw_code)
            if parsed is None:
                continue

            supplier, code, assumed = parsed
            items.append(
                {
                    "type": (type_text or "").strip(),
                    "supplier": supplier,
                    "code": code,
                    "input": raw_code,
                    "assumed": assumed,
                }
            )

    return items


def items_from_text(types_text: str, codes_text: str) -> List[dict]:
    """Line N of the Type box belongs to line N of the Code box."""
    type_lines = (types_text or "").splitlines()
    rows = []

    for line_index, code_line in enumerate((codes_text or "").splitlines()):
        type_text = type_lines[line_index] if line_index < len(type_lines) else ""
        rows.append((type_text, code_line))

    return build_items(rows)


def read_excel_file(uploaded_file) -> pd.DataFrame:
    # dtype=str keeps item numbers such as 3010110 from turning into 3010110.0
    return pd.read_excel(uploaded_file, dtype=str)


CODE_COLUMN_KEYWORDS = (
    "code",
    "item",
    "order no",
    "material",
    "part number",
    "product number",
)


def guess_excel_columns(columns: List) -> Tuple[Optional[object], Optional[object]]:
    """Pick (type_column, code_column) from the column names.

    Columns are matched by name; without a match the first two columns are
    taken as Type and Code. A single-column file has codes only.
    """
    def find(keywords: tuple):
        for column in columns:
            name = str(column).strip().lower()
            if any(keyword in name for keyword in keywords):
                return column
        return None

    if not columns:
        return None, None

    if len(columns) == 1:
        return None, columns[0]

    code_col = find(CODE_COLUMN_KEYWORDS)
    if code_col is None:
        code_col = columns[1]

    type_col = find(("type",))
    if type_col is None and columns[0] != code_col:
        type_col = columns[0]

    if type_col == code_col:
        type_col = None

    return type_col, code_col


def items_from_excel(df: pd.DataFrame, type_col, code_col) -> List[dict]:
    if code_col is None or code_col not in df.columns:
        return []

    def cell_text(row, column) -> str:
        if column is None:
            return ""
        value = row.get(column)
        if value is None or pd.isna(value):
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    rows = [(cell_text(row, type_col), cell_text(row, code_col)) for _, row in df.iterrows()]
    return build_items(rows)


def item_key(item: dict) -> Tuple[str, str]:
    return item["supplier"], item["code"].casefold()


def drop_untyped_duplicates(items: List[dict]) -> List[dict]:
    """Apply the duplicates rule to the item list.

    Items WITH a Type always keep their own cover page + datasheet, even
    when several items share the same code. Items WITHOUT a Type are
    included only once: repeated untyped occurrences are dropped, and an
    untyped occurrence is also dropped when the same code appears elsewhere
    with a Type (its datasheet is already in the pack).
    """
    typed_keys = {item_key(item) for item in items if (item.get("type") or "").strip()}

    seen_untyped = set()
    result = []

    for item in items:
        key = item_key(item)

        if not (item.get("type") or "").strip():
            if key in typed_keys or key in seen_untyped:
                continue
            seen_untyped.add(key)

        result.append(item)

    return result


def supplier_guesses(items: List[dict]) -> List[dict]:
    """Items whose supplier was guessed, when other items carry explicit prefixes.

    A list where nothing has a prefix (the old ABB-only way of working) is
    left alone; once some codes have prefixes, an unprefixed one is probably
    a typo such as GAVEM24... instead of GAV-EM24...
    """
    has_explicit = any(not item.get("assumed") for item in items)
    return [item for item in items if item.get("assumed")] if has_explicit else []
