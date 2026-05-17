from pathlib import Path
import re
import csv
import pandas as pd


def clean_text(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_id(value):
    return clean_text(value).upper()


def clean_header(value):
    return clean_text(value).lower()


def strip_outer_quotes(line):
    """
    If entire row is wrapped in one pair of quotes, remove them.
    Example:
    "Name,Legal form,Country" -> Name,Legal form,Country
    """
    line = str(line).strip()

    if len(line) >= 2 and line[0] == '"' and line[-1] == '"':
        line = line[1:-1]

    return line


def split_csv_line_force(line, delimiter=","):
    """
    Aggressive CSV line splitter for messy North Data exports.

    It handles:
    - whole row wrapped in quotes
    - doubled quotes
    - regular comma splitting
    """
    line = strip_outer_quotes(line)
    line = line.replace('""', '"')

    reader = csv.reader([line], delimiter=delimiter, quotechar='"')
    return next(reader)


def read_csv_force(file_path):
    """
    Reads broken CSVs where the whole row/header is stored as one quoted text field.
    """
    file_path = Path(file_path)

    raw_text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [line for line in raw_text.split("\n") if line.strip()]

    if not lines:
        return pd.DataFrame()

    # North Data export uses commas according to your pasted header.
    delimiter = ","

    header = split_csv_line_force(lines[0], delimiter=delimiter)
    header = [clean_text(h) for h in header]

    rows = []

    for line in lines[1:]:
        row = split_csv_line_force(line, delimiter=delimiter)

        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))

        if len(row) > len(header):
            row = row[:len(header) - 1] + [delimiter.join(row[len(header) - 1:])]

        rows.append(row)

    return pd.DataFrame(rows, columns=header).fillna("")


def needs_force_split(df):
    """
    Detects if dataframe has one column containing a comma-separated header.
    """
    if df.empty:
        return False

    if len(df.columns) == 1 and "," in str(df.columns[0]):
        return True

    # Handles case:
    # ['Name,Legal form,...', 'Unnamed: 1', 'Unnamed: 2']
    first_col = str(df.columns[0])
    other_cols = [str(c).strip().lower() for c in df.columns[1:]]

    if "," in first_col and all(c.startswith("unnamed") or c == "" for c in other_cols):
        return True

    return False


def read_table(file_path):
    """
    Reads CSV/XLSX safely.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        df = None

        # Try normal pandas first
        try:
            df = pd.read_csv(
                file_path,
                dtype=str,
                encoding="utf-8-sig",
                sep=",",
                engine="python",
                on_bad_lines="skip",
            ).fillna("")
        except Exception:
            df = None

        # If pandas reads it as one giant column, force split manually
        if df is None or df.empty or needs_force_split(df):
            df = read_csv_force(file_path)

    elif suffix in [".xlsx", ".xlsm", ".xls"]:
        df = pd.read_excel(file_path, dtype=str).fillna("")

    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return normalize_columns(df)


def normalize_columns(df):
    """
    Cleans column names and drops empty/Unnamed columns.
    """
    df = df.copy()
    df.columns = [clean_text(c) for c in df.columns]

    keep_cols = []

    for col in df.columns:
        cleaned = clean_text(col)
        cleaned_lower = cleaned.lower()

        if not cleaned:
            continue

        if cleaned_lower.startswith("unnamed"):
            continue

        keep_cols.append(col)

    df = df[keep_cols]
    df = df.dropna(how="all").fillna("")

    return df


def find_col(df, possible_names, required=True):
    """
    Finds a column by flexible possible names.
    """
    df = normalize_columns(df)

    lookup = {clean_header(c): c for c in df.columns}

    for name in possible_names:
        key = clean_header(name)
        if key in lookup:
            return lookup[key]

    if required:
        raise KeyError(
            f"Column not found. Tried: {possible_names}. Available columns: {list(df.columns)}"
        )

    return None


def extract_wz_code(industry_text):
    """
    Example:
    '10.89 Manufacture of other food products n.e.c.' -> '10.89'
    """
    text = clean_text(industry_text)
    match = re.search(r"^\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def extract_business_segment(industry_text):
    """
    Example:
    '10.89 Manufacture of other food products n.e.c.' -> 'food products n.e.c.'
    """
    text = clean_text(industry_text)

    text = re.sub(r"^\d+(?:\.\d+)?\s*", "", text)
    text = re.sub(r"(?i)^manufacture of\s+(other\s+)?", "", text)

    return clean_text(text)


def safe_number(value):
    """
    Converts text numbers into float when possible.
    """
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace("€", "").replace(",", "").replace("%", "").strip()

    try:
        return float(text)
    except Exception:
        return None


def first_non_empty(series):
    for value in series:
        value = clean_text(value)
        if value:
            return value
    return ""