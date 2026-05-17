from datetime import datetime
import pandas as pd

from modules.helpers import (
    read_table,
    normalize_columns,
    find_col,
    clean_text,
    clean_id,
    extract_wz_code,
    extract_business_segment,
)


def now_iso():
    return datetime.utcnow().isoformat()


def safe_get(row, col):
    if not col:
        return ""
    value = row.get(col, "")
    if pd.isna(value):
        return ""
    return str(value).strip()


def get_existing_company(supabase, register_id):
    result = (
        supabase.table("companies")
        .select("*")
        .eq("register_id", register_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def read_northdata_companies(
    north_data_path,
    start_row=1,
    max_companies=3,
    register_id_column="Register ID",
    company_column="Name",
    city_column="City",
    website_column="Website",
):
    """
    Reads North Data file in exact uploaded-file order.

    source_row is kept only in memory/logs/CSV for the current run.
    It is not saved to Supabase as company identity.
    register_id is the permanent company identity.
    """

    df = normalize_columns(read_table(north_data_path))

    start_index = max(int(start_row) - 1, 0)
    df = df.iloc[start_index:].copy()

    if max_companies:
        df = df.head(int(max_companies)).copy()

    col_name = find_col(df, [company_column, "Name", "Company Name"])
    col_register_id = find_col(df, [register_id_column, "Register ID"])
    col_city = find_col(df, [city_column, "City"])
    col_website = find_col(df, [website_column, "Website"], required=False)

    col_legal_form = find_col(df, ["Legal form", "Legal Form"], required=False)
    col_country = find_col(df, ["Country"], required=False)
    col_postal_code = find_col(df, ["Postal code", "Postal Code"], required=False)
    col_street = find_col(df, ["Street"], required=False)
    col_register_court = find_col(df, ["Register court", "Register Court"], required=False)
    col_status = find_col(df, ["Status"], required=False)
    col_north_data_url = find_col(df, ["North Data URL"], required=False)
    col_phone = find_col(df, ["Phone"], required=False)
    col_fax = find_col(df, ["Fax"], required=False)
    col_email = find_col(df, ["Email"], required=False)
    col_vat_id = find_col(df, ["VAT Id", "VAT ID"], required=False)

    col_industry = find_col(
        df,
        ["Industry segment (UKSIC)", "Industry Segment", "Industry segment"],
        required=False,
    )
    col_subject = find_col(df, ["Subject"], required=False)

    col_financials_date = find_col(df, ["Financials date"], required=False)
    col_base_capital = find_col(df, ["Base/share capital EUR"], required=False)
    col_assets = find_col(df, ["Total assets EUR"], required=False)
    col_earnings = find_col(df, ["Earnings EUR"], required=False)
    col_earnings_cagr = find_col(df, ["Earnings CAGR %"], required=False)
    col_revenue = find_col(df, ["Revenue EUR"], required=False)
    col_revenue_cagr = find_col(df, ["Revenue CAGR %"], required=False)
    col_return_sales = find_col(df, ["Return on sales %"], required=False)
    col_equity = find_col(df, ["Equity EUR"], required=False)
    col_equity_ratio = find_col(df, ["Equity ratio %"], required=False)
    col_return_equity = find_col(df, ["Return on equity %"], required=False)
    col_employee = find_col(df, ["Employee number"], required=False)

    col_revenue_per_employee = find_col(df, ["Revenue per employee EUR"], required=False)
    col_taxes = find_col(df, ["Taxes EUR"], required=False)
    col_tax_ratio = find_col(df, ["Tax ratio %"], required=False)
    col_cash = find_col(df, ["Cash on hand EUR"], required=False)
    col_receivables = find_col(df, ["Receivables EUR"], required=False)
    col_liabilities = find_col(df, ["Liabilities EUR"], required=False)
    col_cost_materials = find_col(df, ["Cost of materials EUR"], required=False)
    col_wages = find_col(df, ["Wages and salaries EUR"], required=False)
    col_avg_salary = find_col(df, ["Average salaries per employee EUR"], required=False)
    col_pension = find_col(df, ["Pension provisions EUR"], required=False)
    col_real_estate = find_col(df, ["Real estate EUR"], required=False)

    company_rows = []

    for pandas_index, row in df.iterrows():
        source_row = int(pandas_index) + 1
        register_id = clean_id(row.get(col_register_id, ""))

        if not register_id:
            continue

        industry_segment = clean_text(row.get(col_industry, "")) if col_industry else ""

        company_rows.append({
            "_source_row": source_row,

            "register_id": register_id,
            "name": safe_get(row, col_name),
            "legal_form": safe_get(row, col_legal_form),
            "country": safe_get(row, col_country),
            "postal_code": safe_get(row, col_postal_code),
            "city": safe_get(row, col_city),
            "street": safe_get(row, col_street),
            "register_court": safe_get(row, col_register_court),
            "status": safe_get(row, col_status),
            "north_data_url": safe_get(row, col_north_data_url),
            "phone": safe_get(row, col_phone),
            "fax": safe_get(row, col_fax),
            "email": safe_get(row, col_email),
            "website": safe_get(row, col_website),
            "vat_id": safe_get(row, col_vat_id),

            "industry_segment": industry_segment,
            "wz_code": extract_wz_code(industry_segment),
            "business_segment": extract_business_segment(industry_segment),
            "subject": safe_get(row, col_subject),

            "financials_date": safe_get(row, col_financials_date),
            "base_share_capital_eur": safe_get(row, col_base_capital),
            "total_assets_eur": safe_get(row, col_assets),
            "earnings_eur": safe_get(row, col_earnings),
            "earnings_cagr_percent": safe_get(row, col_earnings_cagr),
            "revenue_eur": safe_get(row, col_revenue),
            "revenue_cagr_percent": safe_get(row, col_revenue_cagr),
            "return_on_sales_percent": safe_get(row, col_return_sales),
            "equity_eur": safe_get(row, col_equity),
            "equity_ratio_percent": safe_get(row, col_equity_ratio),
            "return_on_equity_percent": safe_get(row, col_return_equity),
            "employee_number": safe_get(row, col_employee),

            "revenue_per_employee_eur": safe_get(row, col_revenue_per_employee),
            "taxes_eur": safe_get(row, col_taxes),
            "tax_ratio_percent": safe_get(row, col_tax_ratio),
            "cash_on_hand_eur": safe_get(row, col_cash),
            "receivables_eur": safe_get(row, col_receivables),
            "liabilities_eur": safe_get(row, col_liabilities),
            "cost_of_materials_eur": safe_get(row, col_cost_materials),
            "wages_and_salaries_eur": safe_get(row, col_wages),
            "average_salaries_per_employee_eur": safe_get(row, col_avg_salary),
            "pension_provisions_eur": safe_get(row, col_pension),
            "real_estate_eur": safe_get(row, col_real_estate),

            "raw_data": {str(k): safe_get(row, k) for k in df.columns},
            "updated_at": now_iso(),
        })

    return company_rows


def strip_internal_fields(row):
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("_")
    }


def save_companies_to_master(
    supabase,
    company_rows,
    update_existing_companies=True,
    log_callback=None,
):
    """
    Saves companies to the master companies table by register_id.

    If update_existing_companies=True:
        existing register_id rows are updated with uploaded North Data fields.

    If update_existing_companies=False:
        existing register_id rows are not overwritten, but they can still be enriched.
    """

    inserted = 0
    updated = 0
    skipped = 0
    companies_for_enrichment = []

    for row in company_rows:
        register_id = row["register_id"]
        source_row = row.get("_source_row", "")
        existing_row = get_existing_company(supabase, register_id)

        if existing_row and not update_existing_companies:
            skipped += 1

            enriched_source = dict(existing_row)
            enriched_source["_source_row"] = source_row

            companies_for_enrichment.append(enriched_source)

            if log_callback:
                log_callback(
                    f"Company exists, skipped company update: "
                    f"Source row {source_row} | {existing_row.get('name')} | {register_id}"
                )

            continue

        db_row = strip_internal_fields(row)

        supabase.table("companies").upsert(
            db_row,
            on_conflict="register_id",
        ).execute()

        if existing_row:
            updated += 1
            action = "Updated"
        else:
            inserted += 1
            action = "Inserted"

        companies_for_enrichment.append(row)

        if log_callback:
            log_callback(
                f"{action}: Source row {source_row} | {row.get('name')} | {register_id}"
            )

    return {
        "companies_read": len(company_rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "companies_for_enrichment": companies_for_enrichment,
    }