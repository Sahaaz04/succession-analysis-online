import streamlit as st
from pathlib import Path
import tempfile

from modules.supabase_client import get_supabase_client
from modules.northdata_to_supabase import (
    read_northdata_companies,
    save_companies_to_master,
)
from modules.enrichment_to_supabase import run_combined_enrichment


st.set_page_config(
    page_title="Succession Analysis Online",
    layout="wide",
)

st.title("Succession Analysis Online Tool")
st.subheader("Master Company Enrichment")

north_data_file = st.file_uploader(
    "Upload North Data XLSX",
    type=["xlsx"],
    key="north_data_upload",
)

with st.expander("Column mapping", expanded=True):
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        register_id_column = st.text_input(
            "Register ID column",
            value="Register ID",
        )

    with col2:
        company_column = st.text_input(
            "Company name column",
            value="Name",
        )

    with col3:
        city_column = st.text_input(
            "City column",
            value="City",
        )

    with col4:
        website_column = st.text_input(
            "Website column",
            value="Website",
        )

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
        "If enrichment already exists for a Register ID",
        options=[
            "Skip existing enrichment",
            "Replace existing enrichment",
        ],
        index=0,
    )

    col7, col8 = st.columns(2)

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

with st.expander("API keys", expanded=True):
    col9, col10 = st.columns(2)

    with col9:
        handelsregister_api_key = st.text_input(
            "Handelsregister.ai API Key",
            type="password",
        )

    with col10:
        claude_api_key = st.text_input(
            "Claude / Anthropic API Key",
            type="password",
        )

    claude_model_name = st.text_input(
        "Claude model name",
        value="claude-sonnet-4-5",
    )

st.warning(
    "This will read the uploaded North Data file in order, update the master company database by Register ID, "
    "run selected enrichments, save results to Supabase, and provide optional CSV backups."
)

if st.button("Run Master Enrichment", type="primary"):
    if not north_data_file:
        st.error("Please upload a North Data XLSX file.")
    elif run_hr and not handelsregister_api_key:
        st.error("Please paste the Handelsregister.ai API key.")
    elif run_claude and not claude_api_key:
        st.error("Please paste the Claude / Anthropic API key.")
    elif not run_hr and not run_claude:
        st.error("Please select at least one enrichment module.")
    else:
        try:
            log_box = st.empty()
            logs = []

            def log_callback(message):
                logs.append(str(message))
                log_box.text("\n".join(logs[-45:]))

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

                log_callback("Starting enrichment...")

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

            st.session_state["last_save_result"] = save_result
            st.session_state["last_enrichment_result"] = enrichment_result

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


if "last_enrichment_result" in st.session_state:
    st.divider()
    st.subheader("Enrichment Result")

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