import json
from datetime import datetime, timezone

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


SHEET_CONFIG = [
    {
        "sheet_name": "Cockpit",
        "source": "cockpit",
    },
    {
        "sheet_name": "Overview",
        "source": "master_overview",
        "exclude_columns": [],
    },
    {
        "sheet_name": "North Data Exports",
        "source": "companies",
        "exclude_columns": ["id", "raw_data"],
    },
    {
        "sheet_name": "Shareholders",
        "source": "shareholders",
        "exclude_columns": ["id", "raw_data", "dedupe_key"],
    },
    {
        "sheet_name": "News",
        "source": "company_news",
        "exclude_columns": ["id", "raw_data", "dedupe_key"],
    },
    {
        "sheet_name": "Company Models",
        "source": "company_models",
        "exclude_columns": ["id", "raw_data"],
    },
    {
        "sheet_name": "Processing Logs",
        "source": "processing_logs",
        "exclude_columns": ["id"],
    },
]


def get_google_client():
    service_account_raw = st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"]

    if isinstance(service_account_raw, str):
        service_account_info = json.loads(service_account_raw)
    else:
        service_account_info = dict(service_account_raw)

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )

    return gspread.authorize(credentials)


def get_or_create_worksheet(spreadsheet, title, rows=1000, cols=50):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=title,
            rows=rows,
            cols=cols,
        )


def fetch_all_rows(supabase, table_name, chunk_size=1000):
    all_rows = []
    start = 0

    while True:
        end = start + chunk_size - 1

        response = (
            supabase.table(table_name)
            .select("*")
            .range(start, end)
            .execute()
        )

        rows = response.data or []

        if not rows:
            break

        all_rows.extend(rows)

        if len(rows) < chunk_size:
            break

        start += chunk_size

    return all_rows


def clean_cell_value(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)

    return str(value)


def rows_to_sheet_values(rows, exclude_columns=None):
    exclude_columns = set(exclude_columns or [])

    if not rows:
        return [["No data"]]

    df = pd.DataFrame(rows)

    for col in exclude_columns:
        if col in df.columns:
            df = df.drop(columns=[col])

    df = df.fillna("")

    headers = list(df.columns)
    values = []

    for _, row in df.iterrows():
        values.append([clean_cell_value(row[col]) for col in headers])

    return [headers] + values


def write_values_to_worksheet(worksheet, values):
    worksheet.clear()

    if not values:
        values = [["No data"]]

    row_count = max(len(values) + 20, 100)
    col_count = max(len(values[0]) + 5, 20)

    worksheet.resize(rows=row_count, cols=col_count)
    worksheet.update(
        values=values,
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

    try:
        worksheet.freeze(rows=1)
    except Exception:
        pass

    try:
        worksheet.format(
            "1:1",
            {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            },
        )
    except Exception:
        pass


def build_cockpit_values(table_counts):
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    values = [
        ["Succession Analysis Master Workbook"],
        [""],
        ["Last synced", now_text],
        [""],
        ["Table", "Rows"],
    ]

    for table_name, count in table_counts.items():
        values.append([table_name, count])

    values.extend([
        [""],
        ["Notes"],
        ["This workbook is synced from Supabase by Register ID."],
        ["Companies are stored once in the master database."],
        ["Shareholders, news, and model summaries are linked by Register ID."],
    ])

    return values


def sync_supabase_to_google_sheet(supabase, log_callback=None):
    sheet_id = st.secrets["GOOGLE_SHEET_ID"]

    google_client = get_google_client()
    spreadsheet = google_client.open_by_key(sheet_id)

    table_counts = {}

    for config in SHEET_CONFIG:
        sheet_name = config["sheet_name"]
        source = config["source"]

        if log_callback:
            log_callback(f"Syncing sheet: {sheet_name}")

        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)

        if source == "cockpit":
            continue

        rows = fetch_all_rows(supabase, source)
        table_counts[source] = len(rows)

        values = rows_to_sheet_values(
            rows,
            exclude_columns=config.get("exclude_columns", []),
        )

        write_values_to_worksheet(worksheet, values)

    cockpit_sheet = get_or_create_worksheet(spreadsheet, "Cockpit")
    cockpit_values = build_cockpit_values(table_counts)
    write_values_to_worksheet(cockpit_sheet, cockpit_values)

    return {
        "spreadsheet_title": spreadsheet.title,
        "spreadsheet_url": spreadsheet.url,
        "table_counts": table_counts,
    }
