import io
import re
from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st

from modules.filtered_workbook_export import build_filtered_workbook_bytes
from modules.supabase_client import get_supabase_client
from modules.northdata_to_supabase import (
    read_northdata_companies,
    save_companies_to_master,
)
from modules.enrichment_to_supabase import run_combined_enrichment
from modules.fit_scoring import run_fit_scoring
from modules.google_sheets_sync import (
    sync_supabase_to_google_sheet,
    HEADER_RENAMES as SHEET_HEADER_RENAMES,
)


st.set_page_config(
    page_title="Succession Analysis Online",
    layout="wide",
)

st.title("Succession Analysis Online Tool")
st.subheader("Master Company Enrichment")


def nowish_log_box():
    return st.empty()


def to_float(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("€", "").replace("%", "").replace(",", "")
    match = re.search(r"-?\d+(\.\d+)?", text)
    if not match:
        return None

    try:
        return float(match.group())
    except Exception:
        return None


def apply_numeric_filter(df, column, operator, value1=None, value2=None):
    if operator == "Ignore":
        return df

    series = df[column].apply(to_float)

    if operator == "=":
        return df[series == value1]
    if operator == ">":
        return df[series > value1]
    if operator == ">=":
        return df[series >= value1]
    if operator == "<":
        return df[series < value1]
    if operator == "<=":
        return df[series <= value1]
    if operator == "Between":
        if value1 is None or value2 is None:
            return df
        return df[(series >= value1) & (series <= value2)]

    return df


def fetch_all_rows_paginated(supabase, table_name, chunk_size=1000):
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


def pretty_export_dataframe(df):
    def rename_col(col):
        return SHEET_HEADER_RENAMES.get(col, str(col).replace("_", " ").title())

    return df.rename(columns={c: rename_col(c) for c in df.columns})


north_data_file = st.file_uploader(
    "Upload North Data XLSX",
    type=["xlsx"],
    key="north_data_upload",
)

with st.expander("Column mapping", expanded=True):
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        register_id_column = st.text_input("Register ID column", value="Register ID")

    with col2:
        company_column = st.text_input("Company name column", value="Name")

    with col3:
        city_column = st.text_input("City column", value="City")

    with col4:
        website_column = st.text_input("Website column", value="Website")

with st.expander("Run settings", expanded=True):
    col5, col6 = st.columns(2)

    with col5:
        start_row = st.number_input(
            "Start from data row",
            min_value=1,
            value=1,
            help="1 means the first company row after the header.",
        )

    with col6:
        max_companies = st.number_input(
            "Max companies to process",
            min_value=1,
            max_value=10000,
            value=3,
        )

    update_existing_companies = st.radio(
        "If Register ID already exists in master database",
        options=[
            "Update existing company info",
            "Skip existing company info",
        ],
        index=0,
    )

    enrichment_behavior = st.radio(
        "If enrichment / scoring already exists for a Register ID",
        options=[
            "Skip existing enrichment",
            "Replace existing enrichment",
        ],
        index=0,
    )

    col7, col8, col9 = st.columns(3)

    with col7:
        run_hr = st.checkbox(
            "Run Handelsregister shareholder/news enrichment",
            value=True,
        )

    with col8:
        run_claude = st.checkbox(
            "Run Claude business model enrichment",
            value=True,
        )

    with col9:
        run_fit_score = st.checkbox(
            "Run Claude fit scoring",
            value=False,
        )

with st.expander("Fit score settings", expanded=False):
    c1, c2, c3 = st.columns(3)

    with c1:
        revenue_min = st.number_input(
            "Revenue minimum EUR",
            min_value=0.0,
            value=4000000.0,
            step=100000.0,
        )
        revenue_max = st.number_input(
            "Revenue maximum EUR",
            min_value=0.0,
            value=8000000.0,
            step=100000.0,
        )
        employees_min = st.number_input(
            "Minimum employees",
            min_value=0,
            value=20,
            step=1,
        )

    with c2:
        equity_ratio_min = st.number_input(
            "Minimum equity ratio %",
            min_value=0.0,
            value=15.0,
            step=1.0,
        )
        equity_ratio_good = st.number_input(
            "Good equity ratio %",
            min_value=0.0,
            value=30.0,
            step=1.0,
        )
        older_shareholder_age_from = st.number_input(
            "Older shareholder age from",
            min_value=0,
            value=55,
            step=1,
        )

    with c3:
        older_shareholder_age_high_from = st.number_input(
            "High succession age from",
            min_value=0,
            value=65,
            step=1,
        )
        preferred_business_type = st.text_input(
            "Preferred business type",
            value="B2B industrial company",
        )
        preferred_industries = st.text_input(
            "Preferred industries",
            value="cosmetics, food, contract manufacturing",
        )

with st.expander("API keys", expanded=True):
    col10, col11 = st.columns(2)

    with col10:
        handelsregister_api_key = st.text_input(
            "Handelsregister.ai API Key",
            type="password",
        )

    with col11:
        claude_api_key = st.text_input(
            "Claude / Anthropic API Key",
            type="password",
        )

    claude_model_name = st.text_input(
        "Claude business model name",
        value="claude-sonnet-4-5",
    )

    scoring_model_name = st.text_input(
        "Claude fit scoring model name",
        value="claude-sonnet-4-5",
        help="Recommended: claude-sonnet-4-5.",
    )

fit_config = {
    "revenue_min": revenue_min,
    "revenue_max": revenue_max,
    "employees_min": employees_min,
    "equity_ratio_min": equity_ratio_min,
    "equity_ratio_good": equity_ratio_good,
    "older_shareholder_age_from": older_shareholder_age_from,
    "older_shareholder_age_high_from": older_shareholder_age_high_from,
    "preferred_business_type": preferred_business_type,
    "preferred_industries": preferred_industries,
}

st.warning(
    "This reads the uploaded North Data file in order, updates the master company database by Register ID, "
    "runs selected enrichment/scoring modules, saves results to Supabase, and provides optional CSV backups."
)

if st.button("Run Master Enrichment", type="primary"):
    if not north_data_file:
        st.error("Please upload a North Data XLSX file.")
    elif run_hr and not handelsregister_api_key:
        st.error("Please paste the Handelsregister.ai API key.")
    elif (run_claude or run_fit_score) and not claude_api_key:
        st.error("Please paste the Claude / Anthropic API key.")
    elif not run_hr and not run_claude and not run_fit_score:
        st.error("Please select at least one enrichment/scoring module.")
    else:
        try:
            log_box = nowish_log_box()
            logs = []

            def log_callback(message):
                logs.append(str(message))
                log_box.text("\n".join(logs[-60:]))

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir = Path(temp_dir)
                north_path = temp_dir / north_data_file.name
                north_path.write_bytes(north_data_file.getbuffer())

                supabase = get_supabase_client()

                log_callback("Reading North Data file...")

                company_rows = read_northdata_companies(
                    north_data_path=north_path,
                    start_row=start_row,
                    max_companies=max_companies,
                    register_id_column=register_id_column,
                    company_column=company_column,
                    city_column=city_column,
                    website_column=website_column,
                )

                log_callback(f"Companies selected from file: {len(company_rows)}")

                save_result = save_companies_to_master(
                    supabase=supabase,
                    company_rows=company_rows,
                    update_existing_companies=(
                        update_existing_companies == "Update existing company info"
                    ),
                    log_callback=log_callback,
                )

                enrichment_result = None
                fit_score_result = None

                if run_hr or run_claude:
                    log_callback("Starting Handelsregister / Claude business enrichment...")

                    enrichment_result = run_combined_enrichment(
                        supabase=supabase,
                        companies=save_result["companies_for_enrichment"],
                        handelsregister_api_key=handelsregister_api_key,
                        claude_api_key=claude_api_key,
                        claude_model_name=claude_model_name,
                        run_handelsregister=run_hr,
                        run_claude=run_claude,
                        skip_existing_enrichment=(
                            enrichment_behavior == "Skip existing enrichment"
                        ),
                        replace_existing_enrichment=(
                            enrichment_behavior == "Replace existing enrichment"
                        ),
                        log_callback=log_callback,
                    )

                    log_callback(
                        "Business enrichment completed. "
                        f"Processed: {enrichment_result.get('processed_companies', 0)} | "
                        f"Shareholders: {enrichment_result.get('shareholder_rows', 0)} | "
                        f"News: {enrichment_result.get('news_rows', 0)} | "
                        f"Models: {enrichment_result.get('model_rows', 0)}"
                    )

                if run_fit_score:
                    log_callback("Starting Claude fit scoring...")

                    fit_score_result = run_fit_scoring(
                        supabase=supabase,
                        companies=save_result["companies_for_enrichment"],
                        claude_api_key=claude_api_key,
                        scoring_model_name=scoring_model_name,
                        fit_config=fit_config,
                        skip_existing_score=(
                            enrichment_behavior == "Skip existing enrichment"
                        ),
                        replace_existing_score=(
                            enrichment_behavior == "Replace existing enrichment"
                        ),
                        log_callback=log_callback,
                    )

                    log_callback(
                        "Fit scoring completed. "
                        f"Processed: {fit_score_result.get('processed', 0)} | "
                        f"Scored: {fit_score_result.get('scored', 0)} | "
                        f"Skipped: {fit_score_result.get('skipped', 0)} | "
                        f"Errors: {fit_score_result.get('errors', 0)}"
                    )

            st.session_state["last_save_result"] = save_result
            st.session_state["last_enrichment_result"] = enrichment_result
            st.session_state["last_fit_score_result"] = fit_score_result

            st.success("Master enrichment completed.")

        except Exception as e:
            st.error("Pipeline failed.")
            st.exception(e)


if "last_save_result" in st.session_state:
    st.divider()
    st.subheader("Company Save Result")

    save_result = st.session_state["last_save_result"]

    st.write("Companies read from file:", save_result.get("companies_read", ""))
    st.write("Inserted into master database:", save_result.get("inserted", ""))
    st.write("Updated in master database:", save_result.get("updated", ""))
    st.write("Skipped existing companies:", save_result.get("skipped", ""))


if "last_enrichment_result" in st.session_state and st.session_state["last_enrichment_result"]:
    st.divider()
    st.subheader("Business Enrichment Result")

    result = st.session_state["last_enrichment_result"]

    st.write("Processed companies:", result.get("processed_companies", ""))
    st.write("Shareholder rows saved:", result.get("shareholder_rows", ""))
    st.write("News rows saved:", result.get("news_rows", ""))
    st.write("Claude model rows saved:", result.get("model_rows", ""))

    st.subheader("Optional CSV Downloads")

    st.caption(
        "Main data has already been saved to Supabase. "
        "These CSVs are optional backup/export files from this run."
    )

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.download_button(
            "Download shareholders CSV",
            data=result["shareholders_csv"],
            file_name="shareholders_backup.csv",
            mime="text/csv",
            key="download_shareholders_backup",
        )

    with col_b:
        st.download_button(
            "Download news CSV",
            data=result["news_csv"],
            file_name="news_backup.csv",
            mime="text/csv",
            key="download_news_backup",
        )

    with col_c:
        st.download_button(
            "Download Claude models CSV",
            data=result["models_csv"],
            file_name="claude_models_backup.csv",
            mime="text/csv",
            key="download_claude_models_backup",
        )


if "last_fit_score_result" in st.session_state and st.session_state["last_fit_score_result"]:
    st.divider()
    st.subheader("Fit Scoring Result")

    fit_result = st.session_state["last_fit_score_result"]

    st.write("Processed:", fit_result.get("processed", ""))
    st.write("Scored:", fit_result.get("scored", ""))
    st.write("Skipped:", fit_result.get("skipped", ""))
    st.write("Errors:", fit_result.get("errors", ""))


st.divider()
st.subheader("Google Sheet Sync")

st.caption(
    "This updates the fixed Google Sheet from Supabase. "
    "It writes Overview, North Data Exports, Shareholders, News, Company Models, Fit Scores, Processing Logs, and Cockpit."
)

if st.button("Sync Supabase to Google Sheet", type="secondary"):
    try:
        sync_log_box = st.empty()
        sync_logs = []

        def sync_log(message):
            sync_logs.append(str(message))
            sync_log_box.text("\n".join(sync_logs[-40:]))

        supabase = get_supabase_client()

        result = sync_supabase_to_google_sheet(
            supabase=supabase,
            log_callback=sync_log,
        )

        st.success("Google Sheet sync completed.")
        st.write("Workbook:", result["spreadsheet_title"])
        st.link_button("Open Google Sheet", result["spreadsheet_url"])

        st.write("Rows synced:")
        st.json(result["table_counts"])

    except Exception as e:
        st.error("Google Sheet sync failed.")
        st.exception(e)


st.divider()
st.subheader("Filtered Database Export")

st.caption(
    "Create a filtered workbook from the whole database using master_overview. "
    "The download keeps the same sheet structure as the main workbook."
)

with st.form("filtered_export_form"):
    seg_mode = st.selectbox(
        "North Data segment filter",
        ["Ignore", "Contains", "Equals"],
        index=0,
    )
    seg_value = st.text_input("North Data segment value")

    fields = [
        ("Revenue EUR", "revenue_eur", 100000.0),
        ("Net Income EUR", "net_income_eur", 100000.0),
        ("Total Assets EUR", "total_assets_eur", 100000.0),
        ("Equity EUR", "equity_eur", 100000.0),
        ("Equity Ratio %", "equity_ratio_percent", 1.0),
        ("Total Shareholders", "total_shareholders", 1.0),
        ("Natural Shareholders", "natural_shareholders", 1.0),
        ("Corporate Shareholders", "corporate_shareholders", 1.0),
    ]

    filter_specs = {}
    for label, key, step in fields:
        st.markdown(f"**{label}**")
        c1, c2, c3 = st.columns(3)
        with c1:
            op = st.selectbox(
                "Operator",
                ["Ignore", "=", ">", ">=", "<", "<=", "Between"],
                key=f"{key}_op",
            )
        with c2:
            v1 = st.number_input(
                "Value",
                value=0.0,
                step=step,
                key=f"{key}_v1",
            )
        with c3:
            v2 = st.number_input(
                "Upper value",
                value=0.0,
                step=step,
                key=f"{key}_v2",
            )
        filter_specs[key] = {
            "operator": op,
            "value1": v1,
            "value2": v2,
        }

    generate_export = st.form_submit_button("Generate filtered workbook")

if generate_export:
    try:
        export_log_box = st.empty()
        export_logs = []

        def export_log(message):
            export_logs.append(str(message))
            export_log_box.text("\n".join(export_logs[-40:]))

        supabase = get_supabase_client()
        rows = fetch_all_rows_paginated(supabase, "master_overview")
        df = pd.DataFrame(rows)

        if df.empty:
            st.warning("No data found in master_overview.")
        else:
            if seg_mode != "Ignore" and seg_value.strip():
                seg_col = "north_data_business_segment"
                if seg_col in df.columns:
                    if seg_mode == "Contains":
                        df = df[df[seg_col].fillna("").astype(str).str.contains(seg_value, case=False, na=False)]
                    elif seg_mode == "Equals":
                        df = df[df[seg_col].fillna("").astype(str).str.lower() == seg_value.strip().lower()]

            for col_key, spec in filter_specs.items():
                operator = spec["operator"]
                value1 = spec["value1"]
                value2 = spec["value2"]

                if operator == "Ignore":
                    continue

                if col_key in df.columns:
                    df = apply_numeric_filter(df, col_key, operator, value1, value2)

            if df.empty:
                st.warning("No companies matched the selected filters.")
            else:
                sort_cols = [c for c in ["company_name", "register_id"] if c in df.columns]
                if sort_cols:
                    df = df.sort_values(by=sort_cols)

                if "register_id" not in df.columns:
                    raise KeyError("master_overview does not contain register_id.")

                register_ids = [
                    str(v).strip()
                    for v in df["register_id"].dropna().astype(str).tolist()
                    if str(v).strip()
                ]
                register_ids = list(dict.fromkeys(register_ids))

                if not register_ids:
                    st.warning("No register IDs were found after filtering.")
                else:
                    export_result = build_filtered_workbook_bytes(
                        supabase=supabase,
                        register_ids=register_ids,
                        log_callback=export_log,
                    )

                    st.success(f"Filtered workbook created for {len(register_ids)} companies.")
                    st.write("Rows per sheet:")
                    st.json(export_result["table_counts"])

                    st.download_button(
                        "Download filtered workbook",
                        data=export_result["workbook_bytes"],
                        file_name="filtered_master_workbook.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="download_filtered_workbook",
                    )

                    st.caption(
                        "If you upload this file into Google Sheets, run the existing Apps Script once "
                        "to reapply the dropdown behavior in Cockpit and Overview."
                    )

    except Exception as e:
        st.error("Filtered export failed.")
        st.exception(e)
