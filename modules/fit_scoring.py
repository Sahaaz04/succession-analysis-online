import json
from datetime import datetime

from anthropic import Anthropic

from modules.helpers import clean_text, clean_id


DEFAULT_FIT_CONFIG = {
    "revenue_min": 4000000,
    "revenue_max": 8000000,
    "employees_min": 20,
    "equity_ratio_min": 15,
    "equity_ratio_good": 30,
    "older_shareholder_age_from": 55,
    "older_shareholder_age_high_from": 65,
    "preferred_business_type": "B2B industrial company",
    "preferred_industries": "cosmetics, food, contract manufacturing",
    "profit_proxy_target": "around EUR 400k positive earnings/EBITDA proxy if EBITDA is unavailable",
}


def now_iso():
    return datetime.utcnow().isoformat()


def safe(value):
    if value is None:
        return ""
    return str(value).strip()


def fetch_all_rows(supabase, table_name, filters=None, limit=1000):
    query = supabase.table(table_name).select("*")

    if filters:
        for col, value in filters.items():
            query = query.eq(col, value)

    result = query.limit(limit).execute()
    return result.data or []


def get_latest_company_model(supabase, register_id):
    rows = fetch_all_rows(
        supabase,
        "company_models",
        filters={
            "company_register_id": register_id,
            "model_provider": "claude",
        },
        limit=20,
    )

    if not rows:
        return {}

    return sorted(
        rows,
        key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "")),
        reverse=True,
    )[0]


def get_company_shareholders(supabase, register_id):
    return fetch_all_rows(
        supabase,
        "shareholders",
        filters={"company_register_id": register_id},
        limit=200,
    )


def get_company_news(supabase, register_id):
    rows = fetch_all_rows(
        supabase,
        "company_news",
        filters={"company_register_id": register_id},
        limit=50,
    )

    return sorted(
        rows,
        key=lambda x: str(x.get("date", "") or x.get("retrieved_at", "")),
        reverse=True,
    )[:10]


def summarize_shareholders(shareholders):
    total = 0
    natural = 0
    corporate = 0
    shareholder_lines = []

    for row in shareholders:
        name = safe(row.get("shareholder_name"))
        if not name:
            continue

        total += 1

        sh_type = safe(row.get("shareholder_type"))
        sh_type_lower = sh_type.lower()

        if "natural" in sh_type_lower:
            natural += 1
        elif "corporate" in sh_type_lower:
            corporate += 1

        shareholder_lines.append({
            "name": name,
            "type": sh_type,
            "age": safe(row.get("age")),
            "contribution": safe(row.get("contribution_amount")),
            "ownership_percent": safe(row.get("ownership_percent")),
        })

    return {
        "total_shareholders": total,
        "natural_shareholders": natural,
        "corporate_shareholders": corporate,
        "shareholders": shareholder_lines[:10],
    }


def summarize_news(news_rows):
    output = []

    for row in news_rows:
        title = safe(row.get("title"))
        if not title:
            continue

        output.append({
            "date": safe(row.get("date")),
            "type": safe(row.get("signal_type")),
            "title": title,
            "url": safe(row.get("url")),
        })

    return output[:10]


def build_fit_score_prompt(company, model_row, shareholders, news_rows, fit_config):
    shareholder_summary = summarize_shareholders(shareholders)
    news_summary = summarize_news(news_rows)

    company_payload = {
        "identity": {
            "register_id": safe(company.get("register_id")),
            "company_name": safe(company.get("name")),
            "legal_form": safe(company.get("legal_form")),
            "city": safe(company.get("city")),
            "website": safe(company.get("website")),
            "status": safe(company.get("status")),
        },
        "industry_and_business_model": {
            "wz_code": safe(company.get("wz_code")),
            "industry_segment": safe(company.get("industry_segment")),
            "north_data_business_segment": safe(company.get("business_segment")),
            "claude_business_segment": safe(model_row.get("business_segment")),
            "north_data_subject": safe(company.get("subject")),
            "claude_detailed_business_model": safe(model_row.get("summary")),
        },
        "financials": {
            "revenue_eur": safe(company.get("revenue_eur")),
            "earnings_eur": safe(company.get("earnings_eur")),
            "revenue_cagr_percent": safe(company.get("revenue_cagr_percent")),
            "earnings_cagr_percent": safe(company.get("earnings_cagr_percent")),
            "total_assets_eur": safe(company.get("total_assets_eur")),
            "equity_eur": safe(company.get("equity_eur")),
            "equity_ratio_percent": safe(company.get("equity_ratio_percent")),
            "cash_on_hand_eur": safe(company.get("cash_on_hand_eur")),
            "liabilities_eur": safe(company.get("liabilities_eur")),
            "employee_number": safe(company.get("employee_number")),
            "financials_date": safe(company.get("financials_date")),
        },
        "shareholders": shareholder_summary,
        "news": news_summary,
        "target_criteria": fit_config,
    }

    return f"""
You are scoring German companies for acquisition / succession fit.

Use ONLY the provided company data. Do not invent facts.

Score from 1 to 5:
5 = Very high fit: most criteria fulfilled, strong succession/acquisition potential, healthy company.
4 = High fit: key criteria fulfilled, succession/acquisition potential visible.
3 = Medium fit: some criteria fit, but important gaps or uncertainty.
2 = Low fit: major criteria missing, weak fit.
1 = No fit: clearly outside target profile or high-risk.

Important scoring guidance:
- Revenue target is driven by the user config.
- Employee target is driven by the user config.
- Positive earnings / profitability improves score.
- Equity ratio above the configured threshold is good; lower values are weaker.
- Older natural-person shareholders increase succession signal.
- All natural-person ownership is stronger than mixed ownership; all corporate ownership is weaker.
- Preferred industries and business type are driven by the user config.
- Penalize unrelated sectors, distress/insolvency, negative earnings, very small size, and unclear model.

Return ONLY valid JSON. No markdown. No explanation outside JSON.

Required JSON schema:
{{
  "fit_score": 1,
  "fit_label": "No Fit / Low Fit / Medium Fit / High Fit / Very High Fit",
  "fit_comment": "2-4 sentence explanation",
  "succession_signal": "short explanation",
  "financial_signal": "short explanation",
  "shareholder_signal": "short explanation",
  "risk_flags": ["flag 1", "flag 2"],
  "recommended_action": "Reject / Monitor / Manual Review / Prioritize"
}}

Company data:
{json.dumps(company_payload, ensure_ascii=False, indent=2)}
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


def score_company_with_claude(
    api_key,
    model_name,
    company,
    model_row,
    shareholders,
    news_rows,
    fit_config,
):
    client = Anthropic(api_key=str(api_key).strip())

    prompt = build_fit_score_prompt(
        company=company,
        model_row=model_row,
        shareholders=shareholders,
        news_rows=news_rows,
        fit_config=fit_config,
    )

    request_payload = {
        "model": model_name,
        "max_tokens": 700,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }

    if "opus-4-7" not in str(model_name).lower():
        request_payload["temperature"] = 0.1

    response = client.messages.create(**request_payload)

    text_parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text_parts.append(block.text)

    response_text = "\n".join(text_parts).strip()

    if not response_text:
        raise ValueError("Empty Claude response")

    return parse_claude_json(response_text), response_text


def existing_score_exists(supabase, register_id, model_name):
    result = (
        supabase.table("company_fit_scores")
        .select("id")
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .eq("model_name", model_name)
        .limit(1)
        .execute()
    )

    return bool(result.data)


def delete_existing_score(supabase, register_id, model_name):
    (
        supabase.table("company_fit_scores")
        .delete()
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .eq("model_name", model_name)
        .execute()
    )


def upsert_fit_score(supabase, row):
    (
        supabase.table("company_fit_scores")
        .upsert(
            row,
            on_conflict="company_register_id,model_provider,model_name",
        )
        .execute()
    )


def run_fit_scoring(
    supabase,
    companies,
    claude_api_key,
    scoring_model_name="claude-sonnet-4-5",
    fit_config=None,
    skip_existing_score=True,
    replace_existing_score=False,
    log_callback=None,
):
    if not claude_api_key:
        raise ValueError("Claude API key is missing.")

    fit_config = {**DEFAULT_FIT_CONFIG, **(fit_config or {})}

    processed = 0
    scored = 0
    skipped = 0
    errors = 0

    for company in companies:
        register_id = clean_id(company.get("register_id", ""))
        company_name = clean_text(company.get("name", ""))

        if not register_id or not company_name:
            continue

        if log_callback:
            log_callback(f"Scoring: {company_name} | {register_id}")

        exists = existing_score_exists(
            supabase=supabase,
            register_id=register_id,
            model_name=scoring_model_name,
        )

        if exists and skip_existing_score and not replace_existing_score:
            skipped += 1
            if log_callback:
                log_callback("Skipping score: existing fit score found.")
            continue

        if replace_existing_score:
            delete_existing_score(
                supabase=supabase,
                register_id=register_id,
                model_name=scoring_model_name,
            )

        model_row = get_latest_company_model(supabase, register_id)
        shareholders = get_company_shareholders(supabase, register_id)
        news_rows = get_company_news(supabase, register_id)

        try:
            parsed, raw_response = score_company_with_claude(
                api_key=claude_api_key,
                model_name=scoring_model_name,
                company=company,
                model_row=model_row,
                shareholders=shareholders,
                news_rows=news_rows,
                fit_config=fit_config,
            )

            fit_score = parsed.get("fit_score")

            try:
                fit_score = int(fit_score)
            except Exception:
                fit_score = None

            risk_flags = parsed.get("risk_flags", [])

            if isinstance(risk_flags, list):
                risk_flags_text = "; ".join(str(x) for x in risk_flags)
            else:
                risk_flags_text = str(risk_flags)

            row = {
                "company_register_id": register_id,
                "company_name": company_name,
                "fit_score": fit_score,
                "fit_label": safe(parsed.get("fit_label")),
                "fit_comment": safe(parsed.get("fit_comment")),
                "succession_signal": safe(parsed.get("succession_signal")),
                "financial_signal": safe(parsed.get("financial_signal")),
                "shareholder_signal": safe(parsed.get("shareholder_signal")),
                "risk_flags": risk_flags_text,
                "recommended_action": safe(parsed.get("recommended_action")),
                "model_provider": "claude",
                "model_name": scoring_model_name,
                "api_status": "OK",
                "notes": "",
                "scoring_config": fit_config,
                "raw_data": {
                    "parsed": parsed,
                    "raw_response": raw_response,
                },
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

            upsert_fit_score(supabase, row)

            scored += 1

            if log_callback:
                log_callback(f"Saved fit score: {fit_score} | {row['fit_label']}")

        except Exception as e:
            errors += 1

            error_row = {
                "company_register_id": register_id,
                "company_name": company_name,
                "fit_score": None,
                "fit_label": "ERROR",
                "fit_comment": "",
                "succession_signal": "",
                "financial_signal": "",
                "shareholder_signal": "",
                "risk_flags": "",
                "recommended_action": "",
                "model_provider": "claude",
                "model_name": scoring_model_name,
                "api_status": "ERROR",
                "notes": str(e)[:1000],
                "scoring_config": fit_config,
                "raw_data": {
                    "error": str(e),
                },
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

            upsert_fit_score(supabase, error_row)

            if log_callback:
                log_callback(f"Fit scoring failed: {e}")

        processed += 1

    return {
        "processed": processed,
        "scored": scored,
        "skipped": skipped,
        "errors": errors,
    }
