import csv
import io
import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup


REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES_PER_COMPANY = 3
SLEEP_BETWEEN_RETRIES = 2
MAX_WEBSITE_CHARS = 24000
MAX_EXTRA_PAGES = 2
MAX_TEXT_PAGES = 3
MAX_MODEL_SUMMARY_CHARS = 6000
MAX_NEWS_ROWS = 10
MAX_SHAREHOLDER_ROWS = 20


def now_iso():
    return datetime.utcnow().isoformat()


def safe(value):
    if value is None:
        return ""
    return str(value).strip()


def clean_text(value):
    return re.sub(r"\s+", " ", safe(value)).strip()


def clean_id(value):
    return clean_text(value).upper()


def strip_internal_fields(row):
    if not isinstance(row, dict):
        return row

    internal_keys = {
        "id",
    }
    return {k: v for k, v in row.items() if k not in internal_keys}


def log_to_supabase(supabase, batch_id, register_id, module, status, message):
    try:
        supabase.table("processing_logs").insert({
            "batch_id": batch_id,
            "company_register_id": register_id,
            "module": module,
            "status": status,
            "message": safe(message)[:1000],
        }).execute()
    except Exception:
        pass


def table_has_row(supabase, table_name, filters):
    query = supabase.table(table_name).select("id")
    for col, value in filters.items():
        query = query.eq(col, value)
    result = query.limit(1).execute()
    return bool(result.data)


def delete_existing_company_rows(supabase, batch_id, register_id):
    def delete_from(table_name):
        query = supabase.table(table_name).delete().eq("company_register_id", register_id)
        if batch_id is not None:
            query = query.eq("batch_id", batch_id)
        query.execute()

    try:
        delete_from("shareholders")
    except Exception:
        pass

    try:
        delete_from("company_news")
    except Exception:
        pass

    # Fix: Isolated company_models deletion to bypass the batch_id constraint check
    try:
        supabase.table("company_models") \
            .delete() \
            .eq("company_register_id", register_id) \
            .execute()
    except Exception:
        pass

    try:
        supabase.table("company_fit_scores").delete().eq("company_register_id", register_id).execute()
    except Exception:
        pass


def delete_existing_enrichment_rows(supabase, batch_id, register_id):
    delete_existing_company_rows(supabase, batch_id, register_id)


def fetch_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SuccessionAnalysisBot/1.0)"
    }
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    return clean_text(text)


def find_internal_links(base_url, html, max_links=MAX_EXTRA_PAGES):
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc.lower()

    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue

        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)

        if parsed.scheme not in {"http", "https"}:
            continue

        if parsed.netloc.lower() != base_domain:
            continue

        absolute = absolute.split("#")[0].rstrip("/")
        if absolute == base_url.rstrip("/"):
            continue

        if absolute in seen:
            continue

        seen.add(absolute)
        links.append(absolute)

        if len(links) >= max_links:
            break

    return links


def scrape_website(url, log_callback=None):
    try:
        homepage_html = fetch_html(url)
        homepage_text = extract_text_from_html(homepage_html)

        all_text_parts = [homepage_text]

        internal_links = find_internal_links(url, homepage_html, max_links=MAX_EXTRA_PAGES)

        for link in internal_links:
            try:
                if log_callback:
                    log_callback(f"Scraping extra page: {link}")

                html = fetch_html(link)
                page_text = extract_text_from_html(html)
                if page_text:
                    all_text_parts.append(f"\nPage: {link}\n{page_text}")
                time.sleep(0.5)
            except Exception as e:
                if log_callback:
                    log_callback(f"Could not scrape extra page: {link} | {e}")
                continue

        combined_text = "\n\n".join(all_text_parts)
        return combined_text[:MAX_WEBSITE_CHARS], "OK", ""

    except Exception as e:
        return "", "SCRAPE_ERROR", str(e)


def build_claude_prompt(company_name, url, website_text):
    return f"""
You are a business analyst and classification assistant.

Analyze the website text below and return ONLY valid JSON with exactly these keys:
{{
  "detailed_business_model": "50-100 word concise business summary",
  "business_segment": "short standardized segment like 'Food products - meat' or 'Industrial manufacturing - machinery'"
}}

Rules:
- Use only information supported by the website text.
- Do not invent facts.
- The business_segment should be a short normalized label, not a sentence.
- Keep it broad enough for filtering, but specific enough to be useful.
- Return valid JSON only. No markdown. No explanation.

Company name:
{company_name}

Website:
{url}

Website text:
{website_text}
""".strip()


def parse_claude_model_response(response_text):
    text = safe(response_text).strip()

    if not text:
        return "", "", "CLAUDE_ERROR", "Empty response."

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\s*
```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()

    candidates = [text]

    json_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if json_match:
        candidates.append(json_match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                detailed_business_model = safe(parsed.get("detailed_business_model"))
                business_segment = safe(parsed.get("business_segment"))

                if detailed_business_model or business_segment:
                    if not detailed_business_model:
                        detailed_business_model = text[:MAX_MODEL_SUMMARY_CHARS]
                    return detailed_business_model, business_segment, "OK", ""
        except Exception:
            pass

    def extract_field(field_name):
        pattern = rf'"?{field_name}"?\s*:\s*"((?:\\.|[^"\\])*)"'
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        raw_value = match.group(1)
        try:
            return bytes(raw_value, "utf-8").decode("unicode_escape")
        except Exception:
            return raw_value

    detailed_business_model = extract_field("detailed_business_model")
    business_segment = extract_field("business_segment")

    if not detailed_business_model:
        detailed_business_model = text[:MAX_MODEL_SUMMARY_CHARS]

    return detailed_business_model, business_segment, "OK", ""


def summarize_with_claude(api_key, model_name, company_name, url, website_text, log_callback=None):
    if not url:
        return "", "", "NO_WEBSITE", "No website provided."

    if not website_text:
        return "", "", "NO_TEXT", "No website text extracted."

    client = Anthropic(api_key=str(api_key).strip())
    prompt = build_claude_prompt(company_name, url, website_text)

    try:
        request_payload = {
            "model": model_name,
            "max_tokens": 450,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        if "opus-4-7" not in str(model_name).lower():
            request_payload["temperature"] = 0.2

        response = client.messages.create(**request_payload)

        text_parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                text_parts.append(block.text)

        response_text = "\n".join(text_parts).strip()

        if not response_text:
            return "", "", "CLAUDE_ERROR", "Empty response."

        detailed_business_model, business_segment, api_status, notes = parse_claude_model_response(response_text)

        return detailed_business_model, business_segment, api_status, notes

    except Exception as e:
        if log_callback:
            log_callback(f"Claude error: {e}")
        return "", "", "CLAUDE_ERROR", str(e)


def build_shareholder_rows_from_response(data, batch_id, register_id, company_name, api_status="OK", notes=""):
    rows = []
    shareholders = data.get("shareholders") or []

    if isinstance(shareholders, dict):
        shareholders = [shareholders]

    for item in shareholders:
        if not isinstance(item, dict):
            continue

        name = safe(item.get("shareholder_name") or item.get("name"))
        if not name:
            continue

        rows.append({
            "batch_id": batch_id,
            "company_register_id": register_id,
            "company_name": company_name,
            "shareholder_name": name,
            "shareholder_type": safe(item.get("shareholder_type") or item.get("type")),
            "birth_dob": safe(item.get("birth_dob") or item.get("dob")),
            "age": safe(item.get("age")),
            "shareholder_address": safe(item.get("shareholder_address") or item.get("address")),
            "shareholder_country_code": safe(item.get("shareholder_country_code") or item.get("country_code")),
            "shareholder_registration_reference": safe(item.get("shareholder_registration_reference") or item.get("registration_reference")),
            "contribution_amount": safe(item.get("contribution_amount") or item.get("contribution")),
            "contribution_currency": safe(item.get("contribution_currency") or item.get("currency")),
            "ownership_ratio": safe(item.get("ownership_ratio") or item.get("ownership")),
            "ownership_percent": safe(item.get("ownership_percent")),
            "matched_entity_id": safe(item.get("matched_entity_id")),
            "matched_company_name": safe(item.get("matched_company_name")),
            "matched_status": safe(item.get("matched_status")),
            "register_type": safe(item.get("register_type")),
            "register_number": safe(item.get("register_number")),
            "register_match": safe(item.get("register_match")),
            "api_status": api_status,
            "notes": notes,
            "raw_data": item,
            "retrieved_at": now_iso(),
        })

    return rows


def build_news_rows_from_response(data, batch_id, register_id, company_name, api_status="OK", notes=""):
    rows = []
    news_items = data.get("news") or []

    if isinstance(news_items, dict):
        news_items = [news_items]

    for item in news_items:
        if not isinstance(item, dict):
            continue

        title = safe(item.get("title") or item.get("announcement_header"))
        if not title:
            continue

        rows.append({
            "batch_id": batch_id,
            "company_register_id": register_id,
            "company_name": company_name,
            "source_type": safe(item.get("source_type") or item.get("source")),
            "signal_type": safe(item.get("signal_type") or item.get("type")),
            "announcement_header": safe(item.get("announcement_header")),
            "date": safe(item.get("publication_date") or item.get("date")),
            "title": title,
            "summary_context": safe(item.get("summary_context") or item.get("summary")),
            "court": safe(item.get("court")),
            "case_number": safe(item.get("case_number")),
            "register_reference": safe(item.get("register_reference")),
            "url": safe(item.get("url")),
            "source_name": safe(item.get("source_name") or item.get("source")),
            "api_status": api_status,
            "notes": notes,
            "raw_data": item,
            "retrieved_at": now_iso(),
        })

    return rows


# Fix: Removed "batch_id": None so company_models schemas with no batch_id column do not crash
def build_model_row(company_name, register_id, website, summary, business_segment, model_name, api_status="OK", notes=""):
    timestamp = now_iso()

    return {
        "company_register_id": register_id,
        "company_name": company_name,
        "website": website,
        "model_provider": "claude",
        "model_name": model_name,
        "summary": summary,
        "business_segment": business_segment,
        "api_status": api_status,
        "notes": notes,
        "updated_at": timestamp,
        "created_at": timestamp,
        "raw_data": {
            "website": website,
            "company_name": company_name,
            "summary": summary,
            "business_segment": business_segment,
            "model_name": model_name,
        },
    }


def upsert_rows(supabase, table_name, rows, conflict=None):
    if not rows:
        return 0

    query = supabase.table(table_name).upsert(rows)
    if conflict:
        query = query.on_conflict(conflict)
    query.execute()
    return len(rows)


def save_companies_to_master(supabase, company_rows, update_existing_companies=True, log_callback=None):
    inserted = 0
    updated = 0
    skipped = 0
    companies_for_enrichment = []

    for row in company_rows:
        register_id = clean_id(row.get("register_id"))
        company_name = safe(row.get("company_name") or row.get("name"))
        if not register_id or not company_name:
            continue

        payload = {
            "register_id": register_id,
            "name": company_name,
            "legal_form": safe(row.get("legal_form")),
            "country": safe(row.get("country")),
            "postal_code": safe(row.get("postal_code")),
            "city": safe(row.get("city")),
            "street": safe(row.get("street")),
            "register_court": safe(row.get("register_court")),
            "status": safe(row.get("status")),
            "north_data_url": safe(row.get("north_data_url") or row.get("url")),
            "phone": safe(row.get("phone")),
            "fax": safe(row.get("fax")),
            "email": safe(row.get("email")),
            "website": safe(row.get("website")),
            "vat_id": safe(row.get("vat_id")),
            "industry_segment": safe(row.get("industry_segment")),
            "wz_code": safe(row.get("wz_code")),
            "business_segment": safe(row.get("business_segment")),
            "subject": safe(row.get("subject")),
            "revenue_eur": row.get("revenue_eur"),
            "earnings_eur": row.get("earnings_eur"),
            "total_assets_eur": row.get("total_assets_eur"),
            "equity_eur": row.get("equity_eur"),
            "equity_ratio_percent": row.get("equity_ratio_percent"),
            "financials_date": safe(row.get("financials_date")),
            "employee_number": row.get("employee_number"),
            "updated_at": now_iso(),
            "raw_data": row,
        }

        exists = table_has_row(
            supabase,
            "companies",
            {"register_id": register_id},
        )

        if exists:
            if update_existing_companies:
                supabase.table("companies").upsert(
                    strip_internal_fields(payload),
                    on_conflict="register_id",
                ).execute()
                updated += 1
                if log_callback:
                    log_callback(f"Company exists, updated company info: {company_name} | {register_id}")
            else:
                skipped += 1
                if log_callback:
                    log_callback(f"Company exists, skipped company update: {company_name} | {register_id}")
        else:
            supabase.table("companies").insert(strip_internal_fields(payload)).execute()
            inserted += 1
            if log_callback:
                log_callback(f"Company inserted: {company_name} | {register_id}")

        companies_for_enrichment.append({
            "register_id": register_id,
            "name": company_name,
            "website": safe(row.get("website")),
            "city": safe(row.get("city")),
            "legal_form": safe(row.get("legal_form")),
            "status": safe(row.get("status")),
            "business_segment": safe(row.get("business_segment")),
            "subject": safe(row.get("subject")),
            "wz_code": safe(row.get("wz_code")),
            "employee_number": row.get("employee_number"),
            "revenue_eur": row.get("revenue_eur"),
            "earnings_eur": row.get("earnings_eur"),
            "total_assets_eur": row.get("total_assets_eur"),
            "equity_eur": row.get("equity_eur"),
            "equity_ratio_percent": row.get("equity_ratio_percent"),
            "financials_date": safe(row.get("financials_date")),
            "raw_data": row,
        })

    return {
        "companies_read": len(company_rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "companies_for_enrichment": companies_for_enrichment,
    }


def run_combined_enrichment(
    supabase,
    companies,
    handelsregister_api_key,
    claude_api_key,
    claude_model_name,
    run_handelsregister=True,
    run_claude=True,
    skip_existing_enrichment=True,
    replace_existing_enrichment=False,
    log_callback=None,
):
    processed_companies = 0
    shareholder_rows_saved = 0
    news_rows_saved = 0
    model_rows_saved = 0

    all_shareholder_rows = []
    all_news_rows = []
    all_model_rows = []

    shareholders_csv_rows = []
    news_csv_rows = []
    model_csv_rows = []

    for idx, company in enumerate(companies, start=1):
        register_id = clean_id(company.get("register_id"))
        company_name = safe(company.get("name"))
        website = safe(company.get("website"))

        if not register_id or not company_name:
            continue

        processed_companies += 1

        if log_callback:
            log_callback(f"Processing source row {idx}: {company_name} | {register_id}")

        existing_shareholders = table_has_row(
            supabase,
            "shareholders",
            {"company_register_id": register_id},
        )
        existing_news = table_has_row(
            supabase,
            "company_news",
            {"company_register_id": register_id},
        )
        existing_model = table_has_row(
            supabase,
            "company_models",
            {"company_register_id": register_id, "model_provider": "claude"},
        )

        if skip_existing_enrichment and not replace_existing_enrichment and existing_shareholders and existing_news and existing_model:
            if log_callback:
                log_callback("Skipping enrichment: existing shareholder/news/model rows found.")
            continue

        if replace_existing_enrichment:
            delete_existing_enrichment_rows(supabase, None, register_id)

        hr_data = {}
        hr_status = "SKIPPED"
        hr_notes = ""

        if run_handelsregister:
            try:
                query = company_name
                api_status, hr_data, _, hr_notes = fetch_handelsregister_data(
                    query=query,
                    api_key=handelsregister_api_key,
                    log_callback=log_callback,
                )
                hr_status = api_status
            except Exception as e:
                hr_status = "ERROR"
                hr_notes = str(e)
                hr_data = {}

        if run_handelsregister and isinstance(hr_data, dict):
            shareholder_rows = build_shareholder_rows_from_response(
                hr_data,
                batch_id=None,
                register_id=register_id,
                company_name=company_name,
                api_status=hr_status,
                notes=hr_notes,
            )
            news_rows = build_news_rows_from_response(
                hr_data,
                batch_id=None,
                register_id=register_id,
                company_name=company_name,
                api_status=hr_status,
                notes=hr_notes,
            )

            if shareholder_rows:
                supabase.table("shareholders").insert(
                    [strip_internal_fields(r) for r in shareholder_rows]
                ).execute()
                shareholder_rows_saved += len(shareholder_rows)
                all_shareholder_rows.extend(shareholder_rows)
                shareholders_csv_rows.extend(shareholder_rows)

            if news_rows:
                supabase.table("company_news").insert(
                    [strip_internal_fields(r) for r in news_rows]
                ).execute()
                news_rows_saved += len(news_rows)
                all_news_rows.extend(news_rows)
                news_csv_rows.extend(news_rows)

        if run_claude:
            try:
                website_text, scrape_status, scrape_notes = scrape_website(website, log_callback=log_callback)
                if scrape_status != "OK" and log_callback:
                    log_callback(f"Website scrape status: {scrape_status} | {scrape_notes}")

                detailed_business_model, business_segment, api_status, notes = summarize_with_claude(
                    api_key=claude_api_key,
                    model_name=claude_model_name,
                    company_name=company_name,
                    url=website,
                    website_text=website_text,
                    log_callback=log_callback,
                )

                model_row = build_model_row(
                    company_name=company_name,
                    register_id=register_id,
                    website=website,
                    summary=detailed_business_model,
                    business_segment=business_segment,
                    model_name=claude_model_name,
                    api_status=api_status,
                    notes=notes,
                )

                supabase.table("company_models").upsert(
                    strip_internal_fields(model_row),
                    on_conflict="company_register_id,model_provider",
                ).execute()

                model_rows_saved += 1
                all_model_rows.append(model_row)
                model_csv_rows.append(model_row)

                if log_callback:
                    log_callback(f"Saved Claude summary: {api_status}")

            except Exception as e:
                if log_callback:
                    log_callback(f"Claude model error: {e}")

        if log_callback:
            log_callback(
                f"Completed {company_name} | HR rows saved: {len(all_shareholder_rows)} | "
                f"News rows saved: {len(all_news_rows)} | Model rows saved: {len(all_model_rows)}"
            )

    shareholders_csv = pd.DataFrame(shareholders_csv_rows).to_csv(index=False).encode("utf-8")
    news_csv = pd.DataFrame(news_csv_rows).to_csv(index=False).encode("utf-8")
    models_csv = pd.DataFrame(model_csv_rows).to_csv(index=False).encode("utf-8")

    return {
        "processed_companies": processed_companies,
        "shareholder_rows": shareholder_rows_saved,
        "news_rows": news_rows_saved,
        "model_rows": model_rows_saved,
        "shareholders_csv": shareholders_csv,
        "news_csv": news_csv,
        "models_csv": models_csv,
    }


def fetch_handelsregister_data(query, api_key, log_callback=None):
    try:
        url = "https://api.handelsregister.ai/v1/search"

        headers = {
            "x-api-key": str(api_key).strip(),
            "accept": "application/json",
        }

        params = {
            "query": query,
        }

        for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )

                if response.status_code == 200:
                    data = response.json()
                    return 200, data, "OK", ""

                error_text = response.text

                if log_callback:
                    log_callback(f"Handelsregister error attempt {attempt}: {response.status_code} | {error_text}")

                if attempt < MAX_RETRIES_PER_COMPANY:
                    time.sleep(SLEEP_BETWEEN_RETRIES)
                    continue

                return response.status_code, {}, f"combined_failed_attempt_{attempt}", error_text

            except requests.exceptions.Timeout:
                error_text = f"Python request timeout after {REQUEST_TIMEOUT_SECONDS} seconds"

                if attempt < MAX_RETRIES_PER_COMPANY:
                    time.sleep(SLEEP_BETWEEN_RETRIES)
                    continue

                return "PYTHON_TIMEOUT", {}, f"python_timeout_after_{attempt}_attempts", error_text

            except Exception as e:
                error_text = str(e)

                if attempt < MAX_RETRIES_PER_COMPANY:
                    time.sleep(SLEEP_BETWEEN_RETRIES)
                    continue

                return "ERROR", {}, f"exception_after_{attempt}_attempts", error_text

        return "ERROR", {}, "failed_after_retries", "Failed after retries"

    except Exception as e:
        return "ERROR", {}, "fetch_exception", str(e)
