import csv
import io
import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
from curl_cffi import requests
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
MAX_CLAUDE_SHAREHOLDER_JSON_CHARS = 30000


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


def to_int_or_none(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if pd.isna(value):
            return None
        return int(value)

    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null"}:
        return None

    try:
        return int(float(text))
    except Exception:
        return None


def to_float_or_none(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return float(value)

    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null"}:
        return None

    text = text.replace("%", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except Exception:
        return None


def strip_internal_fields(row):
    if not isinstance(row, dict):
        return row

    internal_keys = {
        "id",
        "batch_id",
    }

    return {k: v for k, v in row.items() if k not in internal_keys}


def sanitize_supabase_row(table_name, row):
    if not isinstance(row, dict):
        return row

    cleaned = strip_internal_fields(row)

    if table_name == "shareholders":
        if "source_row" in cleaned:
            cleaned["source_row"] = to_int_or_none(cleaned.get("source_row"))

        if "age" in cleaned:
            cleaned["age"] = to_int_or_none(cleaned.get("age"))

    return cleaned


def sanitize_supabase_rows(table_name, rows):
    return [sanitize_supabase_row(table_name, row) for row in rows]


def extract_register_number(value):
    text = clean_id(value)

    if not text:
        return ""

    match = re.search(
        r"\b(?:HRB|HRA|VR|GNR|PR)\s*([0-9]+[A-Z]?)\b",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1).upper()

    match = re.search(r"\b([0-9]+[A-Z]?)\b", text)
    return match.group(1).upper() if match else ""


def recursive_dicts(obj, max_depth=5, current_depth=0):
    if current_depth > max_depth:
        return

    if isinstance(obj, dict):
        yield obj

        for value in obj.values():
            yield from recursive_dicts(value, max_depth=max_depth, current_depth=current_depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            yield from recursive_dicts(item, max_depth=max_depth, current_depth=current_depth + 1)


def first_value_by_keys(obj, keys, max_depth=5):
    if not isinstance(obj, (dict, list)):
        return ""

    keys_lower = {k.lower() for k in keys}

    for d in recursive_dicts(obj, max_depth=max_depth):
        for key, value in d.items():
            if str(key).lower() in keys_lower and value not in (None, "", []):
                return value

    return ""


def extract_shareholder_entries(data):
    """
    Normalize shareholder structures from Handelsregister.ai into a list.

    Handles structures like:
    - shareholders: [ ... ]
    - shareholders: {"entries": [ ... ]}
    - shareholders: {"items": [ ... ]}
    - shareholders: {"data": [ ... ]}
    """
    if not isinstance(data, dict):
        return []

    shareholders_data = (
        data.get("shareholders")
        or data.get("shareholder")
        or data.get("ownership")
        or data.get("owners")
        or data.get("partners")
        or data.get("beneficial_owners")
        or []
    )

    if isinstance(shareholders_data, list):
        return shareholders_data

    if isinstance(shareholders_data, dict):
        for key in (
            "entries",
            "items",
            "data",
            "results",
            "shareholders",
            "owners",
            "partners",
            "beneficial_owners",
            "ubo",
            "ubos",
        ):
            value = shareholders_data.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                nested_entries = extract_shareholder_entries(value)
                if nested_entries:
                    return nested_entries

        return [shareholders_data]

    return []


def get_shareholder_candidate(entry):
    if not isinstance(entry, dict):
        return entry

    for key in (
        "shareholder",
        "person",
        "entity",
        "owner",
        "partner",
        "holder",
        "individual",
        "organization",
        "company",
        "legal_entity",
        "beneficial_owner",
        "ubo",
    ):
        value = entry.get(key)

        if isinstance(value, dict):
            return value

    return entry


def get_shareholder_name(shareholder):
    if shareholder is None:
        return ""

    if isinstance(shareholder, str):
        return clean_text(shareholder)

    if not isinstance(shareholder, dict):
        return clean_text(shareholder)

    name_keys = (
        "name",
        "full_name",
        "display_name",
        "shareholder_name",
        "person_name",
        "company_name",
        "legal_name",
        "organization_name",
        "entity_name",
        "owner_name",
        "holder_name",
        "partner_name",
        "beneficial_owner_name",
        "ubo_name",
        "firm",
        "firma",
        "title",
        "label",
        "caption",
        "denomination",
        "gesellschafter",
    )

    for key in name_keys:
        value = shareholder.get(key)
        if value:
            return clean_text(value)

    first_name = (
        shareholder.get("first_name")
        or shareholder.get("firstname")
        or shareholder.get("given_name")
        or shareholder.get("vorname")
    )

    last_name = (
        shareholder.get("last_name")
        or shareholder.get("lastname")
        or shareholder.get("family_name")
        or shareholder.get("surname")
        or shareholder.get("nachname")
    )

    combined_name = clean_text(f"{safe(first_name)} {safe(last_name)}")
    if combined_name:
        return combined_name

    for nested_key in (
        "shareholder",
        "person",
        "entity",
        "owner",
        "partner",
        "holder",
        "organization",
        "company",
        "legal_entity",
        "beneficial_owner",
        "ubo",
    ):
        nested = shareholder.get(nested_key)

        if isinstance(nested, dict):
            nested_name = get_shareholder_name(nested)
            if nested_name:
                return nested_name

    recursive_name = first_value_by_keys(shareholder, name_keys, max_depth=4)

    if recursive_name:
        return clean_text(recursive_name)

    return ""


def classify_shareholder(shareholder):
    if isinstance(shareholder, str):
        text = shareholder.lower()

    elif isinstance(shareholder, dict):
        explicit_type = safe(
            shareholder.get("type")
            or shareholder.get("entity_type")
            or shareholder.get("shareholder_type")
            or shareholder.get("legal_form")
            or shareholder.get("person_type")
            or shareholder.get("kind")
        ).lower()

        text = " ".join([explicit_type, get_shareholder_name(shareholder).lower()])

        if any(k in explicit_type for k in ("person", "natural", "individual", "private")):
            return "Natural"

        if any(k in explicit_type for k in ("company", "corporate", "legal", "organization", "entity")):
            return "Corporate"

    else:
        return "Unknown"

    corporate_markers = (
        "gmbh",
        "ug",
        "ag",
        "kg",
        "ohg",
        "se",
        "ltd",
        "limited",
        "holding",
        "s.a.",
        "sarl",
        "bv",
        "nv",
        "inc",
        "corp",
        "llc",
        "foundation",
        "stiftung",
        "verein",
        "eg",
        "kgaa",
        "gmbh & co",
    )

    if any(marker in text for marker in corporate_markers):
        return "Corporate"

    return "Natural" if text else "Unknown"


def get_birth_value(shareholder):
    if not isinstance(shareholder, dict):
        return ""

    birth_keys = (
        "date_of_birth",
        "birth_date",
        "birthdate",
        "dob",
        "birth",
        "born",
        "year_of_birth",
        "birth_year",
        "geburtsdatum",
        "geburtsjahr",
    )

    value = first_value_by_keys(shareholder, birth_keys, max_depth=4)
    return safe(value)


def calc_age(birth_value):
    text = safe(birth_value)

    if not text:
        return None

    year = None
    month = 1
    day = 1

    match = re.search(r"\b(19\d{2}|20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)

    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](19\d{2}|20\d{2})\b", text)

        if match:
            day, month, year = map(int, match.groups())
        else:
            match = re.search(r"\b(19\d{2}|20\d{2})\b", text)

            if match:
                year = int(match.group(1))

    if not year:
        return None

    today = datetime.utcnow().date()
    age = today.year - year

    if (today.month, today.day) < (month, day):
        age -= 1

    return age if 0 <= age <= 130 else None


def make_dedupe_key(*parts):
    cleaned_parts = [clean_text(part).lower() for part in parts if clean_text(part)]
    return "|".join(cleaned_parts)


def extract_amount_and_currency(entry):
    contribution = {}

    if isinstance(entry, dict):
        contribution = entry.get("contribution") or entry.get("share") or entry.get("capital") or {}

    contribution_amount = ""
    contribution_currency = ""

    if isinstance(contribution, dict):
        contribution_amount = (
            contribution.get("amount")
            or contribution.get("value")
            or contribution.get("nominal_value")
            or ""
        )

        contribution_currency = (
            contribution.get("currency")
            or contribution.get("currency_code")
            or ""
        )

    if isinstance(entry, dict):
        contribution_amount = (
            contribution_amount
            or entry.get("contribution_amount")
            or entry.get("amount")
            or entry.get("capital_amount")
            or entry.get("share_capital")
            or entry.get("nominal_amount")
            or ""
        )

        contribution_currency = (
            contribution_currency
            or entry.get("contribution_currency")
            or entry.get("currency")
            or entry.get("capital_currency")
            or entry.get("currency_code")
            or ""
        )

    return contribution_amount, contribution_currency


def extract_ownership_values(entry):
    if not isinstance(entry, dict):
        return "", ""

    ownership_ratio = (
        entry.get("contribution_ratio")
        or entry.get("ownership_ratio")
        or entry.get("share_ratio")
        or entry.get("ratio")
        or entry.get("fraction")
        or ""
    )

    ownership_percent = (
        entry.get("ownership_percent")
        or entry.get("ownership_percentage")
        or entry.get("ownership_%")
        or entry.get("percentage")
        or entry.get("percent")
        or entry.get("share_percent")
        or entry.get("share_percentage")
        or ""
    )

    if ownership_percent == "" and isinstance(ownership_ratio, (int, float)):
        ownership_percent = round(float(ownership_ratio) * 100, 2)

    return ownership_ratio, ownership_percent


def parse_json_from_text(text):
    text = safe(text).strip()

    if not text:
        raise ValueError("Empty JSON text.")

    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end >= 0:
        text = text[start:end + 1]

    return json.loads(text)


def build_claude_shareholder_prompt(company, hr_data, raw_shareholder_entries):
    register_id = clean_id(company.get("register_id", ""))
    company_name = clean_text(company.get("name", ""))

    registration = hr_data.get("registration", {}) if isinstance(hr_data, dict) else {}
    matched_payload = {
        "input_company_name": company_name,
        "input_register_id": register_id,
        "matched_company_name": safe(hr_data.get("name")) if isinstance(hr_data, dict) else "",
        "matched_entity_id": safe(hr_data.get("entity_id")) if isinstance(hr_data, dict) else "",
        "matched_legal_form": safe(hr_data.get("legal_form")) if isinstance(hr_data, dict) else "",
        "matched_status": safe(hr_data.get("status")) if isinstance(hr_data, dict) else "",
        "matched_registration": registration,
    }

    raw_json = json.dumps(raw_shareholder_entries, ensure_ascii=False, default=str)

    if len(raw_json) > MAX_CLAUDE_SHAREHOLDER_JSON_CHARS:
        raw_json = raw_json[:MAX_CLAUDE_SHAREHOLDER_JSON_CHARS]

    return f"""
You are a strict JSON transformation engine.

Task:
Extract shareholder rows from the provided raw Handelsregister shareholder JSON.

Rules:
- Use ONLY the provided JSON.
- Do NOT invent shareholder names, ownership percentages, contribution amounts, addresses, birth dates, or ages.
- If a field is missing, return "" or null.
- Return only current-looking shareholder entries from the provided shareholder JSON.
- Do not output markdown.
- Do not explain.
- Return valid JSON only.

Required output schema:
{{
  "shareholders": [
    {{
      "shareholder_name": "",
      "shareholder_type": "Natural/Corporate/Unknown",
      "birth_dob": "",
      "age": null,
      "shareholder_address": "",
      "shareholder_country_code": "",
      "shareholder_registration_reference": "",
      "contribution_amount": "",
      "contribution_currency": "",
      "ownership_ratio": "",
      "ownership_percent": "",
      "notes": ""
    }}
  ]
}}

Classification guidance:
- Corporate if the shareholder is a company/entity such as GmbH, UG, AG, KG, OHG, SE, Holding, Stiftung, Ltd, Inc, LLC, etc.
- Natural if the shareholder is a human person.
- Unknown if unclear.

Company / matched entity context:
{json.dumps(matched_payload, ensure_ascii=False, indent=2, default=str)}

Raw shareholder JSON:
{raw_json}
""".strip()


def parse_shareholders_with_claude(
    api_key,
    model_name,
    company,
    hr_data,
    raw_shareholder_entries,
    log_callback=None,
):
    if not api_key:
        return [], "CLAUDE_FALLBACK_SKIPPED", "Claude API key missing."

    if not raw_shareholder_entries:
        return [], "CLAUDE_FALLBACK_SKIPPED", "No raw shareholder entries."

    client = Anthropic(api_key=str(api_key).strip())
    prompt = build_claude_shareholder_prompt(
        company=company,
        hr_data=hr_data,
        raw_shareholder_entries=raw_shareholder_entries,
    )

    try:
        request_payload = {
            "model": model_name,
            "max_tokens": 1200,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        if "opus-4-7" not in str(model_name).lower():
            request_payload["temperature"] = 0

        response = client.messages.create(**request_payload)

        text_parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                text_parts.append(block.text)

        response_text = "\n".join(text_parts).strip()

        if not response_text:
            return [], "CLAUDE_FALLBACK_ERROR", "Empty Claude shareholder response."

        parsed = parse_json_from_text(response_text)
        shareholders = parsed.get("shareholders", [])

        if not isinstance(shareholders, list):
            return [], "CLAUDE_FALLBACK_ERROR", "Claude returned shareholders but it was not a list."

        cleaned = []
        for item in shareholders:
            if not isinstance(item, dict):
                continue

            name = clean_text(item.get("shareholder_name"))
            if not name:
                continue

            cleaned.append(item)

        return cleaned, "CLAUDE_FALLBACK_OK", ""

    except Exception as e:
        if log_callback:
            log_callback(f"Claude shareholder fallback error: {e}")

        return [], "CLAUDE_FALLBACK_ERROR", str(e)


def build_shareholder_rows_from_claude_items(company, data, claude_items, api_status, notes):
    register_id = clean_id(company.get("register_id", ""))
    company_name = clean_text(company.get("name", ""))
    source_row = to_int_or_none(company.get("source_row"))

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

    rows = []

    for item in claude_items:
        shareholder_name = clean_text(item.get("shareholder_name"))
        if not shareholder_name:
            continue

        shareholder_type = safe(item.get("shareholder_type")) or "Unknown"
        if shareholder_type not in {"Natural", "Corporate", "Unknown"}:
            shareholder_type = "Unknown"

        birth_value = safe(item.get("birth_dob"))
        age = to_int_or_none(item.get("age"))
        if age is None and birth_value:
            age = calc_age(birth_value)

        row_notes = safe(item.get("notes"))
        combined_notes = "Parsed by Claude shareholder JSON fallback."
        if notes:
            combined_notes += f" Original notes: {safe(notes)}"
        if row_notes:
            combined_notes += f" Claude notes: {row_notes}"

        rows.append(
            {
                "company_register_id": register_id,
                "company_name": company_name,
                "shareholder_name": shareholder_name,
                "shareholder_type": shareholder_type,
                "birth_dob": birth_value,
                "age": to_int_or_none(age),
                "shareholder_address": safe(item.get("shareholder_address")),
                "shareholder_country_code": safe(item.get("shareholder_country_code")),
                "shareholder_registration_reference": safe(item.get("shareholder_registration_reference")),
                "contribution_amount": safe(item.get("contribution_amount")),
                "contribution_currency": safe(item.get("contribution_currency")),
                "ownership_ratio": safe(item.get("ownership_ratio")),
                "ownership_percent": safe(item.get("ownership_percent")),
                "matched_entity_id": matched_entity_id,
                "matched_company_name": matched_name,
                "matched_status": matched_status,
                "legal_form": legal_form,
                "register_court": court,
                "register_type": register_type,
                "register_number": register_number,
                "register_match": register_match,
                "api_status": str(api_status),
                "notes": combined_notes[:1000],
                "retrieved_at": now_iso(),
                "raw_data": {
                    "source": "claude_shareholder_fallback",
                    "claude_item": item,
                },
                "source_row": to_int_or_none(source_row),
            }
        )

    return rows


def log_to_supabase(supabase, batch_id, register_id, module, status, message):
    try:
        supabase.table("processing_logs").insert(
            {
                "batch_id": batch_id,
                "company_register_id": register_id,
                "module": module,
                "status": status,
                "message": safe(message)[:1000],
            }
        ).execute()
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

    try:
        supabase.table("company_models").delete().eq("company_register_id", register_id).execute()
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    response = requests.get(
        url,
        headers=headers,
        impersonate="chrome",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

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
        if not url:
            return "", "NO_WEBSITE", "No website provided."

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

Use only information supported by the website text.
Do not invent facts.
The business_segment should be a short normalized label, not a sentence.
Keep it broad enough for filtering, but specific enough to be useful.
Return valid JSON only. No markdown. No explanation.
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
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()

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


def build_shareholder_rows(company, data, api_status, notes, log_callback=None):
    register_id = clean_id(company.get("register_id", ""))
    company_name = clean_text(company.get("name", ""))
    source_row = to_int_or_none(company.get("source_row"))

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

    entries = extract_shareholder_entries(data)

    rows = []

    if not entries:
        return rows

    skipped_no_name = 0
    skipped_non_dict = 0

    for entry_index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            skipped_non_dict += 1

            if log_callback:
                log_callback(f"[SH DEBUG] {company_name}: skipped shareholder entry {entry_index}; not a dict.")

            continue

        shareholder = get_shareholder_candidate(entry)
        shareholder_name = get_shareholder_name(shareholder)

        if not shareholder_name:
            skipped_no_name += 1

            if log_callback:
                raw_preview = json.dumps(entry, ensure_ascii=False, default=str)[:1200]
                log_callback(
                    f"[SH DEBUG] {company_name}: skipped shareholder entry {entry_index}; "
                    f"could not extract shareholder name. Entry keys: {list(entry.keys())}. "
                    f"Raw preview: {raw_preview}"
                )

            continue

        shareholder_type = classify_shareholder(shareholder)
        birth_value = get_birth_value(shareholder)
        age = calc_age(birth_value)

        contribution_amount, contribution_currency = extract_amount_and_currency(entry)
        ownership_ratio, ownership_percent = extract_ownership_values(entry)

        shareholder_address = ""
        shareholder_country_code = ""
        shareholder_registration_reference = ""

        if isinstance(shareholder, dict):
            shareholder_address = (
                shareholder.get("address")
                or first_value_by_keys(shareholder, ("address", "street_address", "full_address"), max_depth=3)
                or ""
            )

            shareholder_country_code = (
                shareholder.get("country_code")
                or shareholder.get("country")
                or first_value_by_keys(shareholder, ("country_code", "country"), max_depth=3)
                or ""
            )

            shareholder_registration_reference = (
                shareholder.get("registration_reference")
                or shareholder.get("register_reference")
                or shareholder.get("registration_number")
                or ""
            )

        rows.append(
            {
                "company_register_id": register_id,
                "company_name": company_name,
                "shareholder_name": shareholder_name,
                "shareholder_type": shareholder_type,
                "birth_dob": birth_value,
                "age": to_int_or_none(age),
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
                "source_row": to_int_or_none(source_row),
            }
        )

    if log_callback and (skipped_no_name or skipped_non_dict):
        log_callback(
            f"[SH DEBUG] {company_name}: raw shareholder entries={len(entries)}, "
            f"saved_by_code={len(rows)}, skipped_non_dict={skipped_non_dict}, "
            f"skipped_no_name={skipped_no_name}"
        )

    return rows


def build_news_rows_from_response(data, batch_id, register_id, company_name, api_status="OK", notes=""):
    rows = []
    news_items = data.get("news") or []

    if isinstance(news_items, dict):
        nested_news = (
            news_items.get("entries")
            or news_items.get("items")
            or news_items.get("data")
            or news_items.get("results")
        )

        if isinstance(nested_news, list):
            news_items = nested_news
        else:
            news_items = [news_items]

    for item in news_items:
        if not isinstance(item, dict):
            continue

        title = safe(item.get("title") or item.get("announcement_header"))

        if not title:
            continue

        rows.append(
            {
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
            }
        )

    return rows


def build_model_row(
    company_name,
    register_id,
    website,
    summary,
    business_segment,
    model_name,
    api_status="OK",
    notes="",
):
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

    safe_rows = sanitize_supabase_rows(table_name, rows)
    query = supabase.table(table_name).upsert(safe_rows)

    if conflict:
        query = query.on_conflict(conflict)

    query.execute()
    return len(rows)


def save_companies_to_master(supabase, company_rows, update_existing_companies=True, log_callback=None):
    inserted = 0
    updated = 0
    skipped = 0
    companies_for_enrichment = []

    for row_index, row in enumerate(company_rows, start=1):
        register_id = clean_id(row.get("register_id"))
        company_name = safe(row.get("company_name") or row.get("name"))

        if not register_id or not company_name:
            continue

        source_row = to_int_or_none(row.get("source_row")) or row_index

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

        companies_for_enrichment.append(
            {
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
                "source_row": source_row,
                "raw_data": row,
            }
        )

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
        source_row = to_int_or_none(company.get("source_row")) or idx

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

        company_shareholder_count = 0
        company_news_count = 0
        company_model_saved = 0

        hr_data = {}
        hr_status = "SKIPPED"
        hr_notes = ""

        if run_handelsregister:
            try:
                city = safe(company.get("city"))
                query = f"{company_name} {city}".strip() if city else company_name

                if log_callback:
                    log_callback(f"Trying Handelsregister query: {query}")

                api_status, hr_data, _, hr_notes = fetch_handelsregister_data(
                    query=query,
                    api_key=handelsregister_api_key,
                    log_callback=log_callback,
                )

                hr_status = "OK" if api_status == 200 else str(api_status)

                if isinstance(hr_data, dict):
                    if "shareholders" not in hr_data and "news" not in hr_data:
                        for wrapper_key in ("data", "result", "company", "organization"):
                            nested = hr_data.get(wrapper_key)

                            if isinstance(nested, dict) and (
                                "shareholders" in nested or "news" in nested
                            ):
                                if log_callback:
                                    log_callback(f"Unwrapping API response from key '{wrapper_key}'")

                                hr_data = nested
                                break

                if log_callback:
                    sh_entries = extract_shareholder_entries(hr_data) if isinstance(hr_data, dict) else []
                    news_raw = hr_data.get("news") if isinstance(hr_data, dict) else []

                    if isinstance(news_raw, dict):
                        nested_news = (
                            news_raw.get("entries")
                            or news_raw.get("items")
                            or news_raw.get("data")
                            or news_raw.get("results")
                        )
                        news_count = len(nested_news) if isinstance(nested_news, list) else 1
                    else:
                        news_count = len(news_raw or [])

                    top_keys = list(hr_data.keys())[:12] if isinstance(hr_data, dict) else type(hr_data).__name__

                    log_callback(
                        f"Handelsregister raw response — status: {api_status} | "
                        f"top-level keys: {top_keys} | "
                        f"shareholders found: {len(sh_entries)} | news found: {news_count}"
                    )

            except Exception as e:
                hr_status = "ERROR"
                hr_notes = str(e)
                hr_data = {}

        if run_handelsregister and isinstance(hr_data, dict):
            company_for_rows = dict(company)
            company_for_rows["source_row"] = source_row

            raw_shareholder_entries = extract_shareholder_entries(hr_data)

            shareholder_rows = build_shareholder_rows(
                company=company_for_rows,
                data=hr_data,
                api_status=hr_status,
                notes=hr_notes,
                log_callback=log_callback,
            )

            if raw_shareholder_entries and not shareholder_rows and claude_api_key:
                if log_callback:
                    log_callback(
                        f"Trying Claude shareholder fallback: raw shareholder entries found={len(raw_shareholder_entries)}, "
                        f"code parsed rows=0"
                    )

                claude_items, claude_parse_status, claude_parse_notes = parse_shareholders_with_claude(
                    api_key=claude_api_key,
                    model_name=claude_model_name,
                    company=company_for_rows,
                    hr_data=hr_data,
                    raw_shareholder_entries=raw_shareholder_entries,
                    log_callback=log_callback,
                )

                if claude_items:
                    shareholder_rows = build_shareholder_rows_from_claude_items(
                        company=company_for_rows,
                        data=hr_data,
                        claude_items=claude_items,
                        api_status=hr_status,
                        notes=claude_parse_status,
                    )

                    if log_callback:
                        log_callback(
                            f"Claude shareholder fallback parsed rows: {len(shareholder_rows)} | "
                            f"status: {claude_parse_status}"
                        )
                else:
                    if log_callback:
                        log_callback(
                            f"Claude shareholder fallback returned 0 rows | "
                            f"status: {claude_parse_status} | notes: {claude_parse_notes}"
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
                    sanitize_supabase_rows("shareholders", shareholder_rows)
                ).execute()

                company_shareholder_count = len(shareholder_rows)
                shareholder_rows_saved += company_shareholder_count
                all_shareholder_rows.extend(shareholder_rows)
                shareholders_csv_rows.extend(shareholder_rows)

            if news_rows:
                supabase.table("company_news").insert(
                    sanitize_supabase_rows("company_news", news_rows)
                ).execute()

                company_news_count = len(news_rows)
                news_rows_saved += company_news_count
                all_news_rows.extend(news_rows)
                news_csv_rows.extend(news_rows)

        if run_claude:
            try:
                website_text, scrape_status, scrape_notes = scrape_website(
                    website,
                    log_callback=log_callback,
                )

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
                    sanitize_supabase_row("company_models", model_row),
                    on_conflict="company_register_id,model_provider",
                ).execute()

                model_rows_saved += 1
                company_model_saved = 1
                all_model_rows.append(model_row)
                model_csv_rows.append(model_row)

                if log_callback:
                    log_callback(f"Saved Claude summary: {api_status}")

            except Exception as e:
                if log_callback:
                    log_callback(f"Claude model error: {e}")

        if log_callback:
            log_callback(
                f"Completed {company_name} | Shareholder rows saved: {company_shareholder_count} | "
                f"News rows saved: {company_news_count} | Model rows saved: {company_model_saved}"
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
        url = "https://handelsregister.ai/api/v1/fetch-organization"

        headers = {
            "x-api-key": str(api_key).strip(),
            "accept": "application/json",
        }

        params = [
            ("q", query),
            ("feature", "shareholders"),
            ("feature", "news"),
        ]

        for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    impersonate="chrome",
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
        if log_callback:
            log_callback(f"Fatal error in fetch_handelsregister_data: {e}")

        return "FATAL_ERROR", {}, "fatal_exception", str(e)
