import re
import csv
import time
import json
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

from modules.helpers import clean_text, clean_id


HR_API_URL = "https://handelsregister.ai/api/v1/fetch-organization"

REQUEST_TIMEOUT_SECONDS = 600
MAX_RETRIES_PER_COMPANY = 3
SLEEP_BETWEEN_RETRIES = 10
SLEEP_BETWEEN_COMPANIES = 1

MAX_WEBSITE_CHARS = 12000
WEBSITE_TIMEOUT = 20


SHAREHOLDER_CSV_HEADERS = [
    "source_row",
    "company_register_id",
    "company_name",
    "shareholder_name",
    "shareholder_type",
    "birth_dob",
    "age",
    "shareholder_address",
    "shareholder_country_code",
    "shareholder_registration_reference",
    "contribution_amount",
    "contribution_currency",
    "ownership_ratio",
    "ownership_percent",
    "matched_entity_id",
    "matched_company_name",
    "matched_status",
    "legal_form",
    "register_court",
    "register_type",
    "register_number",
    "register_match",
    "api_status",
    "notes",
    "retrieved_at",
]

NEWS_CSV_HEADERS = [
    "source_row",
    "company_register_id",
    "company_name",
    "source_type",
    "signal_type",
    "announcement_header",
    "date",
    "title",
    "summary_context",
    "court",
    "case_number",
    "register_reference",
    "url",
    "source_name",
    "api_status",
    "notes",
    "retrieved_at",
]

MODEL_CSV_HEADERS = [
    "source_row",
    "company_register_id",
    "company_name",
    "website",
    "model_provider",
    "model_name",
    "summary",
    "api_status",
    "notes",
    "created_at",
]


def now_iso():
    return datetime.utcnow().isoformat()


def safe(value):
    if value is None:
        return ""
    return str(value).strip()


def make_dedupe_key(*parts):
    raw = "|".join(safe(part).lower() for part in parts)
    raw = re.sub(r"\s+", " ", raw).strip()
    return hashlib_md5(raw)


def hashlib_md5(text):
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def clean_company_name(name):
    if not name:
        return ""

    name = str(name).strip()
    name = name.split(",")[0]
    name = name.replace("·", ".")
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def extract_register_number(register_id):
    if not register_id:
        return ""

    match = re.search(r"\d+", str(register_id))
    return match.group(0) if match else ""


def calc_age(value):
    if not value:
        return ""

    match = re.search(r"(19|20)\d{2}", str(value))

    if not match:
        return ""

    birth_year = int(match.group(0))
    return datetime.now().year - birth_year


def get_shareholder_name(shareholder):
    if not shareholder or not isinstance(shareholder, dict):
        return ""

    possible_names = [
        shareholder.get("entity_name"),
        shareholder.get("name"),
        shareholder.get("person_name"),
        shareholder.get("full_name"),
        shareholder.get("shareholder_name"),
        shareholder.get("company_name"),
    ]

    for name in possible_names:
        if name:
            return clean_text(name)

    first = shareholder.get("first_name", "")
    last = shareholder.get("last_name", "")

    return clean_text(f"{first} {last}".strip())


def get_birth_value(shareholder):
    if not shareholder or not isinstance(shareholder, dict):
        return ""

    return (
        shareholder.get("birth_date")
        or shareholder.get("date_of_birth")
        or shareholder.get("birth_year")
        or shareholder.get("dob")
        or ""
    )


def classify_shareholder(shareholder):
    if not shareholder or not isinstance(shareholder, dict):
        return "Unknown"

    if shareholder.get("entity_name") or shareholder.get("registration_reference"):
        return "Corporate"

    if get_birth_value(shareholder):
        return "Natural"

    name = get_shareholder_name(shareholder).lower()

    corporate_markers = [
        "gmbh", "ug", "ag", "kg", "ohg", "se",
        "limited", "ltd", "sarl", "holding", "beteiligung",
    ]

    if any(marker in name for marker in corporate_markers):
        return "Corporate"

    if name:
        return "Natural"

    return "Unknown"


def normalize_list(value):
    if not value:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, dict):
        for key in [
            "items",
            "results",
            "data",
            "entries",
            "news",
            "articles",
            "publications",
            "insolvency_publications",
        ]:
            if isinstance(value.get(key), list):
                return value.get(key)

    return []


def classify_signal_type(text, source_type):
    text_lower = str(text).lower()

    if source_type == "insolvency_publication":
        return "Insolvency / Distress"

    succession_terms = [
        "nachfolge", "unternehmensnachfolge", "generationenwechsel",
        "übergabe", "familienunternehmen", "successor", "succession",
    ]

    ownership_terms = [
        "übernahme", "uebernahme", "acquisition", "acquired", "verkauft",
        "sale", "merger", "fusion", "beteiligung", "investor",
        "gesellschafterwechsel", "shareholder change",
    ]

    distress_terms = [
        "insolvenz", "insolvency", "bankruptcy", "restrukturierung",
        "restructuring", "sanierung", "liquidation", "auflösung",
    ]

    management_terms = [
        "geschäftsführerwechsel", "new managing director",
        "management change", "ceo", "appointed", "geschäftsführung",
    ]

    if any(term in text_lower for term in succession_terms):
        return "Succession / Handover"

    if any(term in text_lower for term in ownership_terms):
        return "Ownership Change / Sale / Acquisition"

    if any(term in text_lower for term in distress_terms):
        return "Insolvency / Distress"

    if any(term in text_lower for term in management_terms):
        return "Management Change"

    return "General News"


def parse_insolvency_items(data):
    raw_items = normalize_list(data.get("insolvency_publications"))
    parsed = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        announcement_header = (
            item.get("title")
            or item.get("headline")
            or item.get("type")
            or item.get("publication_type")
            or item.get("announcement_type")
            or item.get("notice_type")
            or item.get("category")
            or item.get("name")
            or ""
        )

        date = (
            item.get("date")
            or item.get("publication_date")
            or item.get("published_at")
            or item.get("created_at")
            or item.get("entry_date")
            or item.get("notice_date")
            or ""
        )

        text = (
            item.get("text")
            or item.get("content")
            or item.get("description")
            or item.get("body")
            or item.get("summary")
            or item.get("message")
            or item.get("notice")
            or item.get("announcement")
            or item.get("raw_text")
            or item.get("publication_text")
            or item.get("details")
            or ""
        )

        court = (
            item.get("court")
            or item.get("register_court")
            or item.get("insolvency_court")
            or item.get("amtsgericht")
            or ""
        )

        case_number = (
            item.get("case_number")
            or item.get("file_number")
            or item.get("reference")
            or item.get("reference_number")
            or item.get("aktenzeichen")
            or ""
        )

        register_reference = (
            item.get("register_reference")
            or item.get("register")
            or item.get("register_number")
            or item.get("registration_number")
            or ""
        )

        url = (
            item.get("url")
            or item.get("link")
            or item.get("source_url")
            or item.get("document_url")
            or ""
        )

        context = " ".join(
            str(x).strip()
            for x in [announcement_header, text, court, case_number, register_reference]
            if x
        )

        parsed.append({
            "source_type": "insolvency_publication",
            "signal_type": "Insolvency / Distress",
            "announcement_header": announcement_header,
            "date": date,
            "title": announcement_header,
            "summary_context": context,
            "court": court,
            "case_number": case_number,
            "register_reference": register_reference,
            "url": url,
            "source_name": "Handelsregister.ai / insolvency publication",
            "raw_data": item,
        })

    return parsed


def parse_news_items(data):
    possible_news = (
        data.get("news")
        or data.get("web_news")
        or data.get("company_news")
        or data.get("articles")
        or []
    )

    raw_items = normalize_list(possible_news)
    parsed = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        title = (
            item.get("title")
            or item.get("headline")
            or item.get("name")
            or item.get("article_title")
            or ""
        )

        date = (
            item.get("date")
            or item.get("published_at")
            or item.get("published_date")
            or item.get("publication_date")
            or item.get("created_at")
            or ""
        )

        url = (
            item.get("url")
            or item.get("link")
            or item.get("source_url")
            or item.get("article_url")
            or ""
        )

        summary = (
            item.get("content")
            or item.get("snippet")
            or item.get("summary")
            or item.get("description")
            or item.get("text")
            or ""
        )

        source_name = (
            item.get("source_name")
            or item.get("publisher")
            or item.get("domain")
            or item.get("source")
            or ""
        )

        combined_text = f"{title} {summary}"

        parsed.append({
            "source_type": "news",
            "signal_type": classify_signal_type(combined_text, "news"),
            "announcement_header": "",
            "date": date,
            "title": title,
            "summary_context": summary,
            "court": "",
            "case_number": "",
            "register_reference": "",
            "url": url,
            "source_name": source_name,
            "raw_data": item,
        })

    return parsed


def fetch_handelsregister_data(query, api_key, log_callback=None):
    headers = {
        "x-api-key": str(api_key).strip(),
        "Accept": "application/json",
    }

    params = {
        "q": query,
        "feature": [
            "shareholders",
            "insolvency_publications",
            "news",
        ],
    }

    for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
        if log_callback:
            log_callback(f"Handelsregister API attempt {attempt}/{MAX_RETRIES_PER_COMPANY}")

        try:
            response = requests.get(
                HR_API_URL,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code == 200:
                return 200, response.json(), ""

            error_text = response.text[:500]

            if response.status_code in [408, 429, 500, 502, 503, 504]:
                if attempt < MAX_RETRIES_PER_COMPANY:
                    time.sleep(SLEEP_BETWEEN_RETRIES)
                    continue

            return response.status_code, {}, error_text

        except requests.exceptions.Timeout:
            error_text = f"Python request timeout after {REQUEST_TIMEOUT_SECONDS} seconds"

            if attempt < MAX_RETRIES_PER_COMPANY:
                time.sleep(SLEEP_BETWEEN_RETRIES)
                continue

            return "PYTHON_TIMEOUT", {}, error_text

        except Exception as e:
            error_text = str(e)

            if attempt < MAX_RETRIES_PER_COMPANY:
                time.sleep(SLEEP_BETWEEN_RETRIES)
                continue

            return "ERROR", {}, error_text

    return "ERROR", {}, "Failed after retries"


def get_shareholder_entries_from_response(data):
    shareholders_block = data.get("shareholders")

    if not shareholders_block:
        return []

    if isinstance(shareholders_block, list):
        return shareholders_block

    if isinstance(shareholders_block, dict):
        for key in [
            "entries",
            "items",
            "data",
            "results",
            "shareholders",
            "owners",
            "beneficial_owners",
        ]:
            value = shareholders_block.get(key)
            if isinstance(value, list):
                return value

    return []


def build_shareholder_rows(company, data, api_status, notes):
    register_id = clean_id(company.get("register_id", ""))
    company_name = clean_text(company.get("name", ""))
    source_row = company.get("_source_row", "")

    matched_entity_id = safe(data.get("entity_id"))
    matched_name = safe(data.get("name"))
    matched_status = safe(data.get("status"))
    legal_form = safe(data.get("legal_form"))

    registration = data.get("registration", {}) or {}
    court = safe(registration.get("court"))
    register_type = safe(registration.get("register_type"))
    register_number = safe(registration.get("register_number"))

    input_register_number = extract_register_number(register_id)
    register_match = "Yes" if input_register_number and str(register_number) == input_register_number else "Review"

    entries = get_shareholder_entries_from_response(data)

    rows = []

    if not entries:
        return rows

    seen = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        shareholder = (
            entry.get("shareholder")
            or entry.get("person")
            or entry.get("entity")
            or entry.get("owner")
            or entry
            or {}
        )

        contribution = (
            entry.get("contribution")
            or entry.get("share")
            or {}
        )

        shareholder_name = get_shareholder_name(shareholder)

        if not shareholder_name:
            continue

        shareholder_type = classify_shareholder(shareholder)
        birth_value = get_birth_value(shareholder)
        age = calc_age(birth_value)

        contribution_amount = ""
        contribution_currency = ""

        if isinstance(contribution, dict):
            contribution_amount = contribution.get("amount") or ""
            contribution_currency = contribution.get("currency") or ""

        contribution_amount = (
            contribution_amount
            or entry.get("contribution_amount")
            or entry.get("amount")
            or entry.get("capital_amount")
            or ""
        )

        contribution_currency = (
            contribution_currency
            or entry.get("contribution_currency")
            or entry.get("currency")
            or entry.get("capital_currency")
            or ""
        )

        ownership_ratio = (
            entry.get("contribution_ratio")
            or entry.get("ownership_ratio")
            or entry.get("share_ratio")
            or ""
        )

        ownership_percent = (
            entry.get("ownership_percent")
            or entry.get("ownership_percentage")
            or entry.get("ownership_%")
            or ""
        )

        if ownership_percent == "" and isinstance(ownership_ratio, (int, float)):
            ownership_percent = round(ownership_ratio * 100, 2)

        shareholder_address = ""
        shareholder_country_code = ""
        shareholder_registration_reference = ""

        if isinstance(shareholder, dict):
            shareholder_address = shareholder.get("address") or ""
            shareholder_country_code = shareholder.get("country_code") or ""
            shareholder_registration_reference = shareholder.get("registration_reference") or ""

        dedupe_key = make_dedupe_key(
            register_id,
            shareholder_name,
            contribution_amount,
            ownership_percent,
            ownership_ratio,
        )

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)

        rows.append({
            "company_register_id": register_id,
            "company_name": company_name,
            "shareholder_name": shareholder_name,
            "shareholder_type": shareholder_type,
            "birth_dob": birth_value,
            "age": str(age),
            "shareholder_address": safe(shareholder_address),
            "shareholder_country_code": safe(shareholder_country_code),
            "shareholder_registration_reference": safe(shareholder_registration_reference),
            "contribution_amount": safe(contribution_amount),
            "contribution_currency": safe(contribution_currency),
            "ownership_ratio": safe(ownership_ratio),
            "ownership_percent": safe(ownership_percent),
            "matched_entity_id": matched_entity_id,
            "matched_company_name": matched_name,
            "matched_status": matched_status,
            "legal_form": legal_form,
            "register_court": court,
            "register_type": register_type,
            "register_number": register_number,
            "register_match": register_match,
            "api_status": str(api_status),
            "notes": notes or "",
            "retrieved_at": now_iso(),
            "raw_data": entry,
            "dedupe_key": dedupe_key,
            "_source_row": source_row,
        })

    return rows


def build_news_rows(company, data, api_status, notes):
    register_id = clean_id(company.get("register_id", ""))
    company_name = clean_text(company.get("name", ""))
    source_row = company.get("_source_row", "")

    all_items = parse_insolvency_items(data) + parse_news_items(data)

    rows = []

    if not all_items:
        return rows

    seen = set()

    for item in all_items:
        title = safe(item.get("title"))
        url = safe(item.get("url"))
        date = safe(item.get("date"))
        source_type = safe(item.get("source_type"))

        dedupe_key = make_dedupe_key(
            register_id,
            source_type,
            date,
            title,
            url,
        )

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)

        rows.append({
            "company_register_id": register_id,
            "company_name": company_name,
            "source_type": source_type,
            "signal_type": safe(item.get("signal_type")),
            "announcement_header": safe(item.get("announcement_header")),
            "date": date,
            "title": title,
            "summary_context": safe(item.get("summary_context")),
            "court": safe(item.get("court")),
            "case_number": safe(item.get("case_number")),
            "register_reference": safe(item.get("register_reference")),
            "url": url,
            "source_name": safe(item.get("source_name")),
            "api_status": str(api_status),
            "notes": notes or "",
            "retrieved_at": now_iso(),
            "raw_data": item.get("raw_data", item),
            "dedupe_key": dedupe_key,
            "_source_row": source_row,
        })

    return rows


def clean_url(url):
    if not url:
        return ""

    url = str(url).strip()

    if not url or url.lower() in ["nan", "none", "null"]:
        return ""

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def fetch_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=WEBSITE_TIMEOUT)
    response.raise_for_status()

    return response.text


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup([
        "script", "style", "noscript", "svg",
        "nav", "footer", "header", "form",
    ]):
        tag.decompose()

    return normalize_text(soup.get_text(separator=" "))


def find_relevant_internal_links(base_url, html, max_links=2):
    soup = BeautifulSoup(html, "html.parser")

    keywords = [
        "about", "unternehmen", "über-uns", "ueber-uns", "wir-ueber-uns",
        "company", "services", "leistungen", "produkte", "products",
        "angebot", "kompetenzen", "profil", "geschichte", "team", "impressum",
    ]

    links = []
    base_domain = urlparse(base_url).netloc.lower()

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        link_text = normalize_text(a.get_text(" ")).lower()

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.scheme not in ["http", "https"]:
            continue

        if parsed.netloc.lower() != base_domain:
            continue

        combined = f"{href} {link_text}".lower()

        if any(keyword in combined for keyword in keywords):
            clean_link = full_url.split("#")[0]

            if clean_link not in links and clean_link != base_url:
                links.append(clean_link)

        if len(links) >= max_links:
            break

    return links


def scrape_website(url, log_callback=None):
    try:
        homepage_html = fetch_html(url)
        homepage_text = extract_text_from_html(homepage_html)

        all_text_parts = [f"Homepage text:\n{homepage_text}"]

        internal_links = find_relevant_internal_links(
            base_url=url,
            html=homepage_html,
            max_links=2,
        )

        for link in internal_links:
            try:
                if log_callback:
                    log_callback(f"Scraping extra page: {link}")

                html = fetch_html(link)
                page_text = extract_text_from_html(html)
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
You are a business research analyst.

Analyze the website text below and return ONLY valid JSON with these keys:

{{
  "summary": "50-100 word concise company profile",
  "business_segment_claude": "one short line like food products - meat or food products - chocolate"
}}

Rules:
- Use only information supported by the website text.
- Do not invent facts.
- If unclear, say so in the summary.
- Keep business_segment_claude short and practical.

Company name:
{company_name}

Website:
{url}

Website text:
{website_text}
""".strip()


def parse_claude_json(text):
    text = text.strip()

    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end >= 0:
        text = text[start:end + 1]

    return json.loads(text)


def summarize_with_claude(api_key, model_name, company_name, url, website_text, log_callback=None):
    if not url:
        return "No website provided.", "", "NO_WEBSITE", "No website provided."

    if not website_text:
        return "No website text extracted.", "", "NO_TEXT", "No website text extracted."

    client = Anthropic(api_key=str(api_key).strip())
    prompt = build_claude_prompt(company_name, url, website_text)

    try:
        request_payload = {
            "model": model_name,
            "max_tokens": 300,
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
            return "CLAUDE_ERROR: Empty response.", "", "CLAUDE_ERROR", "Empty response"

        parsed = parse_claude_json(response_text)
        summary = parsed.get("summary", "").strip() or response_text
        business_segment_claude = parsed.get("business_segment_claude", "").strip()

        return summary, business_segment_claude, "OK", ""

    except Exception as e:
        if log_callback:
            log_callback(f"Claude error: {e}")

        return f"CLAUDE_ERROR: {e}", "", "CLAUDE_ERROR", str(e)


def table_has_row(supabase, table_name, filters):
    query = supabase.table(table_name).select("id")

    for col, value in filters.items():
        query = query.eq(col, value)

    result = query.limit(1).execute()

    return bool(result.data)


def delete_existing_handelsregister_rows(supabase, register_id):
    supabase.table("shareholders").delete().eq("company_register_id", register_id).execute()
    supabase.table("company_news").delete().eq("company_register_id", register_id).execute()


def delete_existing_model_rows(supabase, register_id, model_provider, model_name):
    supabase.table("company_models").delete() \
        .eq("company_register_id", register_id) \
        .eq("model_provider", model_provider) \
        .eq("model_name", model_name) \
        .execute()


def strip_internal_fields(row):
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("_")
    }


def insert_rows(supabase, table_name, rows, chunk_size=100):
    if not rows:
        return

    cleaned_rows = [strip_internal_fields(row) for row in rows]

    for i in range(0, len(cleaned_rows), chunk_size):
        chunk = cleaned_rows[i:i + chunk_size]

        if table_name in ["shareholders", "company_news"]:
            supabase.table(table_name).upsert(
                chunk,
                on_conflict="company_register_id,dedupe_key",
            ).execute()
        else:
            supabase.table(table_name).insert(chunk).execute()


def upsert_company_model(supabase, model_row):
    cleaned = strip_internal_fields(model_row)

    supabase.table("company_models").upsert(
        cleaned,
        on_conflict="company_register_id,model_provider,model_name",
    ).execute()


def log_to_supabase(supabase, register_id, company_name, module, status, message):
    try:
        supabase.table("processing_logs").insert({
            "company_register_id": register_id,
            "company_name": company_name,
            "module": module,
            "status": status,
            "message": str(message)[:1000],
        }).execute()
    except Exception:
        pass


def rows_for_csv(rows):
    csv_rows = []

    for row in rows:
        clean_row = strip_internal_fields(row)
        clean_row["source_row"] = row.get("_source_row", "")
        csv_rows.append(clean_row)

    return csv_rows


def dataframe_csv_bytes(rows, headers):
    df = pd.DataFrame(rows_for_csv(rows))

    for col in headers:
        if col not in df.columns:
            df[col] = ""

    df = df[headers]

    return df.to_csv(index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL).encode("utf-8-sig")


def run_combined_enrichment(
    supabase,
    companies,
    handelsregister_api_key,
    claude_api_key,
    claude_model_name="claude-sonnet-4-5",
    run_handelsregister=True,
    run_claude=True,
    skip_existing_enrichment=True,
    replace_existing_enrichment=False,
    log_callback=None,
):
    if run_handelsregister and not handelsregister_api_key:
        raise ValueError("Handelsregister.ai API key is missing.")

    if run_claude and not claude_api_key:
        raise ValueError("Claude API key is missing.")

    processed_companies = 0

    all_shareholder_rows = []
    all_news_rows = []
    all_model_rows = []

    for company in companies:
        register_id = clean_id(company.get("register_id", ""))
        company_name = clean_text(company.get("name", ""))
        source_row = company.get("_source_row", "")

        if not register_id or not company_name:
            continue

        did_anything = False

        if log_callback:
            log_callback(f"Processing source row {source_row}: {company_name} | {register_id}")

        if run_handelsregister:
            has_shareholders = table_has_row(
                supabase,
                "shareholders",
                {"company_register_id": register_id},
            )

            has_news = table_has_row(
                supabase,
                "company_news",
                {"company_register_id": register_id},
            )

            if skip_existing_enrichment and not replace_existing_enrichment and (has_shareholders or has_news):
                if log_callback:
                    log_callback("Skipping Handelsregister: existing shareholder/news rows found.")
            else:
                if replace_existing_enrichment:
                    delete_existing_handelsregister_rows(supabase, register_id)

                query = clean_company_name(company_name)

                api_status, data, notes = fetch_handelsregister_data(
                    query=query,
                    api_key=handelsregister_api_key,
                    log_callback=log_callback,
                )

                if api_status == 200:
                    shareholder_rows = build_shareholder_rows(
                        company=company,
                        data=data,
                        api_status=api_status,
                        notes="",
                    )

                    news_rows = build_news_rows(
                        company=company,
                        data=data,
                        api_status=api_status,
                        notes="",
                    )

                    insert_rows(supabase, "shareholders", shareholder_rows)
                    insert_rows(supabase, "company_news", news_rows)

                    all_shareholder_rows.extend(shareholder_rows)
                    all_news_rows.extend(news_rows)

                    if log_callback:
                        log_callback(f"Saved shareholders: {len(shareholder_rows)} | news: {len(news_rows)}")

                    log_to_supabase(
                        supabase=supabase,
                        register_id=register_id,
                        company_name=company_name,
                        module="handelsregister",
                        status="OK",
                        message=f"Saved {len(shareholder_rows)} shareholder rows and {len(news_rows)} news rows.",
                    )

                else:
                    log_to_supabase(
                        supabase=supabase,
                        register_id=register_id,
                        company_name=company_name,
                        module="handelsregister",
                        status="ERROR",
                        message=f"{api_status}: {notes}",
                    )

                    if log_callback:
                        log_callback(f"Handelsregister failed: {api_status} | {notes}")

                did_anything = True

        if run_claude:
            has_model = table_has_row(
                supabase,
                "company_models",
                {
                    "company_register_id": register_id,
                    "model_provider": "claude",
                    "model_name": claude_model_name,
                },
            )

            if skip_existing_enrichment and not replace_existing_enrichment and has_model:
                if log_callback:
                    log_callback("Skipping Claude: existing model summary found.")
            else:
                if replace_existing_enrichment:
                    delete_existing_model_rows(
                        supabase,
                        register_id,
                        "claude",
                        claude_model_name,
                    )

                website = clean_url(company.get("website", ""))

                if not website:
                    summary = "No website provided."
                    business_segment_claude = ""
                    api_status = "NO_WEBSITE"
                    notes = "No website provided in North Data export."

                else:
                    website_text, scrape_status, scrape_notes = scrape_website(
                        website,
                        log_callback=log_callback,
                    )

                    if scrape_status != "OK":
                        summary = f"SCRAPE_ERROR: {scrape_notes}"
                        business_segment_claude = ""
                        api_status = "SCRAPE_ERROR"
                        notes = scrape_notes

                    else:
                        summary, business_segment_claude, api_status, notes = summarize_with_claude(
                            api_key=claude_api_key,
                            model_name=claude_model_name,
                            company_name=company_name,
                            url=website,
                            website_text=website_text,
                            log_callback=log_callback,
                        )

                model_row = {
                    "company_register_id": register_id,
                    "company_name": company_name,
                    "website": website,
                    "model_provider": "claude",
                    "model_name": claude_model_name,
                    "summary": summary,
                    "api_status": api_status,
                    "notes": notes,
                    "created_at": now_iso(),
                    "raw_data": {
                        "website": website,
                        "company_name": company_name,
                        "model": claude_model_name,
                        "business_segment_claude": business_segment_claude,
                    },
                    "_source_row": source_row,
                }

                upsert_company_model(supabase, model_row)
                all_model_rows.append(model_row)

                log_to_supabase(
                    supabase=supabase,
                    register_id=register_id,
                    company_name=company_name,
                    module="claude",
                    status=api_status,
                    message=notes or "Claude summary saved.",
                )

                if log_callback:
                    log_callback(f"Saved Claude summary: {api_status}")

                did_anything = True

        if did_anything:
            processed_companies += 1
            time.sleep(SLEEP_BETWEEN_COMPANIES)

    shareholders_csv = dataframe_csv_bytes(all_shareholder_rows, SHAREHOLDER_CSV_HEADERS)
    news_csv = dataframe_csv_bytes(all_news_rows, NEWS_CSV_HEADERS)
    models_csv = dataframe_csv_bytes(all_model_rows, MODEL_CSV_HEADERS)

    return {
        "processed_companies": processed_companies,
        "shareholder_rows": len(all_shareholder_rows),
        "news_rows": len(all_news_rows),
        "model_rows": len(all_model_rows),
        "shareholders_csv": shareholders_csv,
        "news_csv": news_csv,
        "models_csv": models_csv,
    }
