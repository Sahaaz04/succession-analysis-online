create table if not exists batches (
    id uuid primary key default gen_random_uuid(),
    batch_name text,
    uploaded_file_name text,
    company_count integer default 0,
    status text default 'uploaded',
    notes text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists companies (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid references batches(id) on delete cascade,

    register_id text not null,
    name text,
    legal_form text,
    country text,
    postal_code text,
    city text,
    street text,
    register_court text,
    status text,
    north_data_url text,
    phone text,
    fax text,
    email text,
    website text,
    vat_id text,

    industry_segment text,
    wz_code text,
    business_segment text,
    subject text,

    financials_date text,
    base_share_capital_eur text,
    total_assets_eur text,
    earnings_eur text,
    earnings_cagr_percent text,
    revenue_eur text,
    revenue_cagr_percent text,
    return_on_sales_percent text,
    equity_eur text,
    equity_ratio_percent text,
    return_on_equity_percent text,
    employee_number text,

    raw_data jsonb,

    created_at timestamptz default now(),
    updated_at timestamptz default now(),

    unique(batch_id, register_id)
);

create table if not exists shareholders (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid references batches(id) on delete cascade,
    company_register_id text not null,

    shareholder_name text,
    shareholder_type text,
    birth_dob text,
    age text,
    shareholder_address text,
    shareholder_country_code text,
    shareholder_registration_reference text,
    contribution_amount text,
    contribution_currency text,
    ownership_ratio text,
    ownership_percent text,

    matched_entity_id text,
    matched_company_name text,
    matched_status text,
    legal_form text,
    register_court text,
    register_type text,
    register_number text,
    register_match text,

    api_status text,
    notes text,
    retrieved_at timestamptz default now(),
    raw_data jsonb
);

create table if not exists company_news (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid references batches(id) on delete cascade,
    company_register_id text not null,

    source_type text,
    signal_type text,
    announcement_header text,
    date text,
    title text,
    summary_context text,
    court text,
    case_number text,
    register_reference text,
    url text,
    source_name text,

    api_status text,
    notes text,
    retrieved_at timestamptz default now(),
    raw_data jsonb
);

create table if not exists company_models (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid references batches(id) on delete cascade,
    company_register_id text not null,

    company_name text,
    website text,
    model_provider text,
    model_name text,
    summary text,

    api_status text,
    notes text,
    created_at timestamptz default now(),
    raw_data jsonb,

    unique(batch_id, company_register_id, model_provider, model_name)
);

create table if not exists processing_logs (
    id uuid primary key default gen_random_uuid(),
    batch_id uuid references batches(id) on delete cascade,
    company_register_id text,
    module text,
    status text,
    message text,
    created_at timestamptz default now()
);