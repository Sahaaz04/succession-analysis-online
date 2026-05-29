from datetime import datetime, timezone

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


RAW_SHEETS = [
    {
        "sheet_name": "North Data Exports",
        "source": "companies",
        "exclude_columns": ["id", "raw_data", "input_row", "source_row"],
    },
    {
        "sheet_name": "Shareholders",
        "source": "shareholders",
        "exclude_columns": ["id", "raw_data", "dedupe_key", "input_row", "source_row"],
    },
    {
        "sheet_name": "News",
        "source": "company_news",
        "exclude_columns": [
            "id",
            "raw_data",
            "dedupe_key",
            "input_row",
            "source_row",
            "summary_context",
            "court",
            "case_number",
            "register_reference",
            "notes",
        ],
    },
    {
        "sheet_name": "Company Models",
        "source": "company_models",
        "exclude_columns": ["id", "raw_data", "input_row", "source_row"],
    },
    {
        "sheet_name": "Fit Scores",
        "source": "company_fit_scores",
        "exclude_columns": ["id", "raw_data", "input_row", "source_row"],
    },
    {
        "sheet_name": "Processing Logs",
        "source": "processing_logs",
        "exclude_columns": ["id", "input_row", "source_row"],
    },
]


HEADER_RENAMES = {
    "register_id": "Register ID",
    "company_register_id": "Register ID",
    "name": "Company Name",
    "company_name": "Company Name",
    "legal_form": "Legal Form",
    "country": "Country",
    "postal_code": "Postal Code",
    "city": "City",
    "street": "Street",
    "register_court": "Register Court",
    "status": "Status",
    "north_data_url": "North Data URL",
    "phone": "Phone",
    "fax": "Fax",
    "email": "Email",
    "website": "Website",
    "vat_id": "VAT ID",
    "industry_segment": "Industry Segment",
    "north_data_business_segment": "North Data Business Segment",
    "claude_business_segment": "Claude Business Segment",
    "wz_code": "WZ Code",
    "business_segment": "Business Segment",
    "subject": "Business Model",
    "detailed_business_model": "Detailed Business Model",
    "summary": "Detailed Business Model",
    "financials_date": "Financial Data Year",
    "base_share_capital_eur": "Base Share Capital EUR",
    "total_assets_eur": "Total Assets EUR",
    "earnings_eur": "Net Income EUR",
    "net_income_eur": "Net Income EUR",
    "earnings_cagr_percent": "Earnings CAGR %",
    "revenue_eur": "Revenue EUR",
    "revenue_cagr_percent": "Revenue CAGR %",
    "return_on_sales_percent": "Return on Sales %",
    "equity_eur": "Equity EUR",
    "equity_ratio_percent": "Equity Ratio %",
    "return_on_equity_percent": "Return on Equity %",
    "employee_number": "Number of Employees",
    "revenue_per_employee_eur": "Revenue per Employee EUR",
    "taxes_eur": "Taxes EUR",
    "tax_ratio_percent": "Tax Ratio %",
    "cash_on_hand_eur": "Cash on Hand EUR",
    "receivables_eur": "Receivables EUR",
    "liabilities_eur": "Liabilities EUR",
    "cost_of_materials_eur": "Cost of Materials EUR",
    "wages_and_salaries_eur": "Wages and Salaries EUR",
    "average_salaries_per_employee_eur": "Average Salaries per Employee EUR",
    "pension_provisions_eur": "Pension Provisions EUR",
    "real_estate_eur": "Real Estate EUR",
    "shareholder_name": "Shareholder Name",
    "shareholder_type": "Shareholder Type",
    "birth_dob": "Birth/DOB",
    "age": "Shareholder Age",
    "shareholder_address": "Shareholder Address",
    "shareholder_country_code": "Shareholder Country Code",
    "shareholder_registration_reference": "Shareholder Registration Reference",
    "contribution_amount": "Shareholder Contribution",
    "contribution_currency": "Contribution Currency",
    "ownership_ratio": "Ownership Ratio",
    "ownership_percent": "Shareholder Ownership %",
    "matched_entity_id": "Matched Entity ID",
    "matched_company_name": "Matched Company Name",
    "matched_status": "Matched Status",
    "register_type": "Register Type",
    "register_number": "Register Number",
    "register_match": "Register Match",
    "source_type": "Source Type",
    "signal_type": "News Type",
    "announcement_header": "Announcement Header",
    "date": "News Date",
    "title": "News Title",
    "url": "News URL",
    "source_name": "Source Name",
    "model_provider": "Model Provider",
    "model_name": "Model Name",
    "scoring_config": "Scoring Config",
    "fit_score": "Fit Score",
    "fit_label": "Fit Label",
    "fit_comment": "Fit Comment",
    "succession_signal": "Succession Signal",
    "financial_signal": "Financial Signal",
    "shareholder_signal": "Shareholder Signal",
    "risk_flags": "Risk Flags",
    "recommended_action": "Recommended Action",
    "api_status": "API Status",
    "notes": "Notes",
    "created_at": "Created At",
    "updated_at": "Updated At",
    "retrieved_at": "Retrieved At",
    "company_updated_at": "Company Updated At",
    "model_updated_at": "Model Updated At",
    "score_updated_at": "Score Updated At",
    "last_updated_at": "Last Updated At",
    "module": "Module",
    "message": "Message",
}


OVERVIEW_HEADERS = [
    "Register ID",
    "Company Name",
    "Legal Form",
    "WZ Code",
    "North Data Business Segment",
    "Claude Business Segment",
    "Business Model",
    "Detailed Business Model",
    "City",
    "Revenue EUR",
    "Net Income EUR",
    "Total Assets EUR",
    "Equity EUR",
    "Equity Ratio %",
    "Financial Data Year",
    "Number of Employees",
    "Total Shareholders",
    "Natural Shareholders",
    "Corporate Shareholders",
    "Shareholder Name",
    "Shareholder Age",
    "Shareholder Type",
    "Shareholder Contribution",
    "Shareholder Ownership %",
    "News Title",
    "News Date",
    "News Type",
    "News URL",
    "Website",
    "Fit Score",
    "Fit Label",
    "Fit Comment",
    "Succession Signal",
    "Financial Signal",
    "Shareholder Signal",
    "Risk Flags",
    "Recommended Action",
    "Scoring Model",
    "Score Updated At",
    "Company Updated At",
    "Model Updated At",
    "Last Updated At",
]


def get_google_client():
    service_account_info = dict(st.secrets["GOOGLE_SERVICE_ACCOUNT"])

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


def clean_cell_value(value, allow_formulas=False):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        value = str(value)
    else:
        value = str(value)

    value = value.strip()

    if not allow_formulas and value.startswith(("=", "+", "-", "@")):
        return "'" + value

    return value


def nice_header(column_name):
    return HEADER_RENAMES.get(
        column_name,
        str(column_name).replace("_", " ").title(),
    )


def rows_to_sheet_values(rows, exclude_columns=None):
    exclude_columns = set(exclude_columns or [])

    if not rows:
        return [["No data"]]

    df = pd.DataFrame(rows)

    for col in exclude_columns:
        if col in df.columns:
            df = df.drop(columns=[col])

    df = df.fillna("")

    headers = [nice_header(col) for col in df.columns]

    values = []
    for _, row in df.iterrows():
        values.append([
            clean_cell_value(row[col], allow_formulas=False)
            for col in df.columns
        ])

    return [headers] + values


def index_by_register_id(rows, key="company_register_id"):
    result = {}

    for row in rows:
        register_id = row.get(key) or row.get("register_id")
        if not register_id:
            continue

        result.setdefault(str(register_id).strip(), []).append(row)

    return result


def get_latest_model_for_register(company_models, register_id):
    matches = [
        row for row in company_models
        if str(row.get("company_register_id", "")).strip() == register_id
        and row.get("model_provider") == "claude"
    ]

    if not matches:
        return {}

    return sorted(
        matches,
        key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")),
        reverse=True,
    )[0]


def get_latest_fit_score_for_register(fit_scores, register_id):
    matches = [
        row for row in fit_scores
        if str(row.get("company_register_id", "")).strip() == register_id
        and row.get("model_provider") == "claude"
    ]

    if not matches:
        return {}

    return sorted(
        matches,
        key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")),
        reverse=True,
    )[0]


def get_default_shareholder(shareholders_by_register, register_id):
    rows = shareholders_by_register.get(register_id, [])

    rows = [
        row for row in rows
        if str(row.get("shareholder_name", "")).strip()
    ]

    if not rows:
        return {}

    return sorted(
        rows,
        key=lambda x: str(x.get("retrieved_at", "")),
        reverse=True,
    )[0]


def get_default_news(news_by_register, register_id):
    rows = news_by_register.get(register_id, [])

    rows = [
        row for row in rows
        if str(row.get("title", "")).strip()
    ]

    if not rows:
        return {}

    return sorted(
        rows,
        key=lambda x: (
            str(x.get("date", "")),
            str(x.get("retrieved_at", "")),
        ),
        reverse=True,
    )[0]


def build_overview_values(
    companies,
    shareholders,
    company_news,
    company_models,
    fit_scores,
):
    shareholders_by_register = index_by_register_id(shareholders)
    news_by_register = index_by_register_id(company_news)

    sorted_companies = sorted(
        companies,
        key=lambda x: (
            str(x.get("name", "")).lower(),
            str(x.get("register_id", "")),
        ),
    )

    values = [OVERVIEW_HEADERS]

    for company in sorted_companies:
        register_id = str(company.get("register_id", "")).strip()

        if not register_id:
            continue

        model = get_latest_model_for_register(company_models, register_id)
        fit_score = get_latest_fit_score_for_register(fit_scores, register_id)
        shareholder = get_default_shareholder(shareholders_by_register, register_id)
        news = get_default_news(news_by_register, register_id)

        next_row_number = len(values) + 1

        equity_ratio_formula = f'=IFERROR(M{next_row_number}/L{next_row_number},"")'

        company_updated_at = str(company.get("updated_at", ""))
        model_updated_at = str(model.get("updated_at", "") or model.get("created_at", ""))
        score_updated_at = str(fit_score.get("updated_at", "") or fit_score.get("created_at", ""))

        last_updated_at = max(company_updated_at, model_updated_at, score_updated_at)

        row = [
            register_id,
            company.get("name", ""),
            company.get("legal_form", ""),
            company.get("wz_code", ""),
            company.get("business_segment", ""),  # North Data business segment
            model.get("business_segment", ""),    # Claude business segment
            company.get("subject", ""),
            model.get("summary", ""),
            company.get("city", ""),
            company.get("revenue_eur", ""),
            company.get("earnings_eur", ""),
            company.get("total_assets_eur", ""),
            company.get("equity_eur", ""),
            equity_ratio_formula,
            company.get("financials_date", ""),
            company.get("employee_number", ""),
            len(shareholders_by_register.get(register_id, [])),
            sum(
                1 for sh in shareholders_by_register.get(register_id, [])
                if "natural" in str(sh.get("shareholder_type", "")).lower()
            ),
            sum(
                1 for sh in shareholders_by_register.get(register_id, [])
                if "corporate" in str(sh.get("shareholder_type", "")).lower()
            ),
            shareholder.get("shareholder_name", ""),
            shareholder.get("age", ""),
            shareholder.get("shareholder_type", ""),
            shareholder.get("contribution_amount", ""),
            shareholder.get("ownership_percent", ""),
            news.get("title", ""),
            news.get("date", ""),
            news.get("signal_type", ""),
            news.get("url", ""),
            company.get("website", ""),
            fit_score.get("fit_score", ""),
            fit_score.get("fit_label", ""),
            fit_score.get("fit_comment", ""),
            fit_score.get("succession_signal", ""),
            fit_score.get("financial_signal", ""),
            fit_score.get("shareholder_signal", ""),
            fit_score.get("risk_flags", ""),
            fit_score.get("recommended_action", ""),
            fit_score.get("model_name", ""),
            score_updated_at,
            company_updated_at,
            model_updated_at,
            last_updated_at,
        ]

        values.append([clean_cell_value(value, allow_formulas=True) for value in row])

    return values


def build_cockpit_values():
    return [
        ["Succession Analysis Cockpit"],
        [""],
        ["Select / Enter Register ID", ""],
        [""],
        ["Field", "Value"],
        ["Company Name", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$B:$B),"")'],
        ["Legal Form", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$C:$C),"")'],
        ["WZ Code", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$D:$D),"")'],
        ["North Data Business Segment", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$E:$E),"")'],
        ["Claude Business Segment", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$F:$F),"")'],
        ["Business Model", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$G:$G),"")'],
        ["Detailed Business Model", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$H:$H),"")'],
        ["City", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$I:$I),"")'],
        ["Revenue EUR", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$J:$J),"")'],
        ["Net Income EUR", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$K:$K),"")'],
        ["Total Assets EUR", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$L:$L),"")'],
        ["Equity EUR", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$M:$M),"")'],
        ["Equity Ratio %", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$N:$N),"")'],
        ["Financial Data Year", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$O:$O),"")'],
        ["Number of Employees", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$P:$P),"")'],
        ["Total Shareholders", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$Q:$Q),"")'],
        ["Natural Shareholders", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$R:$R),"")'],
        ["Corporate Shareholders", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$S:$S),"")'],
        ["Shareholder Name", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$T:$T),"")'],
        ["Shareholder Age", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$U:$U),"")'],
        ["Shareholder Type", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$V:$V),"")'],
        ["Shareholder Contribution", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$W:$W),"")'],
        ["Shareholder Ownership %", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$X:$X),"")'],
        ["News Title", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$Y:$Y),"")'],
        ["News Date", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$Z:$Z),"")'],
        ["News Type", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AA:$AA),"")'],
        ["News URL", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AB:$AB),"")'],
        ["Website", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AC:$AC),"")'],
        ["Fit Score", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AD:$AD),"")'],
        ["Fit Label", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AE:$AE),"")'],
        ["Fit Comment", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AF:$AF),"")'],
        ["Succession Signal", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AG:$AG),"")'],
        ["Financial Signal", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AH:$AH),"")'],
        ["Shareholder Signal", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AI:$AI),"")'],
        ["Risk Flags", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AJ:$AJ),"")'],
        ["Recommended Action", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AK:$AK),"")'],
        ["Scoring Model", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AL:$AL),"")'],
        ["Score Updated At", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AM:$AM),"")'],
        ["Company Updated At", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AN:$AN),"")'],
        ["Model Updated At", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AO:$AO),"")'],
        ["Last Updated At", '=IFERROR(XLOOKUP($B$3,Overview!$A:$A,Overview!$AP:$AP),"")'],
    ]


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


def format_worksheet_basic(worksheet):
    try:
        worksheet.format(
            "1:1",
            {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
                "backgroundColor": {
                    "red": 0.85,
                    "green": 0.90,
                    "blue": 1.0,
                },
            },
        )
    except Exception:
        pass

    try:
        worksheet.format(
            "A:AZ",
            {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "MIDDLE",
            },
        )
    except Exception:
        pass


def format_overview_sheet(worksheet):
    format_worksheet_basic(worksheet)

    try:
        worksheet.format(
            "N:N",
            {
                "numberFormat": {
                    "type": "PERCENT",
                    "pattern": "0.0%",
                },
            },
        )
    except Exception:
        pass


def format_cockpit_sheet(worksheet):
    try:
        worksheet.format(
            "A1:B1",
            {
                "textFormat": {"bold": True, "fontSize": 14},
                "backgroundColor": {
                    "red": 0.85,
                    "green": 0.90,
                    "blue": 1.0,
                },
            },
        )
        worksheet.format(
            "A5:B5",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {
                    "red": 0.90,
                    "green": 0.90,
                    "blue": 0.90,
                },
            },
        )
        worksheet.format(
            "A:A",
            {
                "textFormat": {"bold": True},
            },
        )
        worksheet.format(
            "A:B",
            {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "MIDDLE",
            },
        )
    except Exception:
        pass


def sync_supabase_to_google_sheet(supabase, log_callback=None):
    sheet_id = st.secrets["GOOGLE_SHEET_ID"]

    google_client = get_google_client()
    spreadsheet = google_client.open_by_key(sheet_id)

    if log_callback:
        log_callback("Fetching Supabase data...")

    companies = fetch_all_rows(supabase, "companies")
    shareholders = fetch_all_rows(supabase, "shareholders")
    company_news = fetch_all_rows(supabase, "company_news")
    company_models = fetch_all_rows(supabase, "company_models")
    fit_scores = fetch_all_rows(supabase, "company_fit_scores")
    processing_logs = fetch_all_rows(supabase, "processing_logs")

    table_counts = {
        "Overview": len(companies),
        "North Data Exports": len(companies),
        "Shareholders": len(shareholders),
        "News": len(company_news),
        "Company Models": len(company_models),
        "Fit Scores": len(fit_scores),
        "Processing Logs": len(processing_logs),
    }

    if log_callback:
        log_callback("Writing Overview...")

    overview_sheet = get_or_create_worksheet(spreadsheet, "Overview")
    overview_values = build_overview_values(
        companies=companies,
        shareholders=shareholders,
        company_news=company_news,
        company_models=company_models,
        fit_scores=fit_scores,
    )
    write_values_to_worksheet(overview_sheet, overview_values)
    format_overview_sheet(overview_sheet)

    if log_callback:
        log_callback("Writing Cockpit...")

    cockpit_sheet = get_or_create_worksheet(spreadsheet, "Cockpit")
    cockpit_values = build_cockpit_values()
    write_values_to_worksheet(cockpit_sheet, cockpit_values)
    format_cockpit_sheet(cockpit_sheet)

    raw_data_map = {
        "North Data Exports": companies,
        "Shareholders": shareholders,
        "News": company_news,
        "Company Models": company_models,
        "Fit Scores": fit_scores,
        "Processing Logs": processing_logs,
    }

    for config in RAW_SHEETS:
        sheet_name = config["sheet_name"]

        if log_callback:
            log_callback(f"Writing {sheet_name}...")

        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
        rows = raw_data_map.get(sheet_name, [])

        values = rows_to_sheet_values(
            rows,
            exclude_columns=config.get("exclude_columns", []),
        )

        write_values_to_worksheet(worksheet, values)
        format_worksheet_basic(worksheet)

    return {
        "spreadsheet_title": spreadsheet.title,
        "spreadsheet_url": spreadsheet.url,
        "table_counts": table_counts,
    }
