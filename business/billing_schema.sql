-- ============================================================
-- MCS Stream C — Task 1: Billing Schema (PostgreSQL / TimescaleDB)
-- ============================================================
-- Run against the MCS database. Uses a dedicated `billing` schema
-- to keep business tables separate from Stream B's telemetry tables.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS billing;
SET search_path TO billing, public;

-- ──────────────────────────────
-- ENUM TYPES
-- ──────────────────────────────

CREATE TYPE billing.contract_type AS ENUM ('colo_msa', 'host_agreement', 'lender');
CREATE TYPE billing.contract_status AS ENUM ('draft', 'active', 'expired', 'terminated');
CREATE TYPE billing.availability_class AS ENUM ('A', 'B', 'C');
CREATE TYPE billing.rate_type AS ENUM (
    'colo_per_kw', 'power_per_kwh', 'demand_charge', 'cooling_pue',
    'heat_credit', 'revenue_share', 'cross_connect'
);
CREATE TYPE billing.invoice_status AS ENUM ('draft', 'sent', 'paid', 'overdue', 'void');
CREATE TYPE billing.line_item_type AS ENUM (
    'colo_fee', 'metered_power', 'cooling_overhead', 'heat_credit',
    'demand_charge', 'cross_connect', 'sla_credit', 'manual_adjustment', 'tax'
);
CREATE TYPE billing.heat_pricing_model AS ENUM ('credit', 'revenue_share');
CREATE TYPE billing.meter_type AS ENUM ('electrical', 'thermal');

-- ──────────────────────────────
-- CUSTOMERS
-- ──────────────────────────────

CREATE TABLE billing.customers (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL UNIQUE,          -- FK → Stream B tenants
    name            VARCHAR(256) NOT NULL,
    billing_contact_email VARCHAR(320) NOT NULL,
    billing_address JSONB,
    payment_method  VARCHAR(32),                          -- stripe / wire / ach
    stripe_customer_id VARCHAR(128),
    tax_id          VARCHAR(64),
    currency        VARCHAR(3) NOT NULL DEFAULT 'USD',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────
-- CONTRACTS
-- ──────────────────────────────

CREATE TABLE billing.contracts (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES billing.customers(id),
    contract_type   billing.contract_type NOT NULL,
    site_id         VARCHAR(64) NOT NULL,                 -- FK → Stream B sites
    contract_ref    VARCHAR(64) NOT NULL UNIQUE,          -- ML-BALD-2026-001
    start_date      DATE NOT NULL,
    end_date        DATE,
    auto_renew      BOOLEAN NOT NULL DEFAULT FALSE,
    status          billing.contract_status NOT NULL DEFAULT 'draft',
    terms_json      JSONB,                                -- notice period, payment terms, etc.
    payment_terms_days INTEGER NOT NULL DEFAULT 30,
    tax_rate_pct    NUMERIC(5,4) NOT NULL DEFAULT 0.0000, -- 0.08 = 8%
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_contracts_customer_status ON billing.contracts (customer_id, status);
CREATE INDEX ix_contracts_site_status ON billing.contracts (site_id, status);

-- ──────────────────────────────
-- RACK ASSIGNMENTS
-- ──────────────────────────────

CREATE TABLE billing.contract_rack_assignments (
    id              BIGSERIAL PRIMARY KEY,
    contract_id     BIGINT NOT NULL REFERENCES billing.contracts(id),
    block_id        VARCHAR(64) NOT NULL,                 -- FK → Stream B blocks
    rack_ids        VARCHAR(32)[] NOT NULL,               -- array of rack IDs
    committed_kw    NUMERIC(10,2) NOT NULL,
    availability_class billing.availability_class NOT NULL
);

CREATE INDEX ix_rack_assignments_block ON billing.contract_rack_assignments (block_id);

-- ──────────────────────────────
-- RATE SCHEDULES
-- ──────────────────────────────

CREATE TABLE billing.rate_schedules (
    id              BIGSERIAL PRIMARY KEY,
    contract_id     BIGINT NOT NULL REFERENCES billing.contracts(id),
    effective_from  DATE NOT NULL,
    rate_type       billing.rate_type NOT NULL,
    rate_value      NUMERIC(12,6) NOT NULL,
    currency        VARCHAR(3) NOT NULL DEFAULT 'USD',
    notes           TEXT,

    CONSTRAINT uq_rate_schedule UNIQUE (contract_id, effective_from, rate_type)
);

CREATE INDEX ix_rate_schedules_lookup ON billing.rate_schedules (contract_id, rate_type, effective_from);

-- ──────────────────────────────
-- INVOICES
-- ──────────────────────────────

CREATE TABLE billing.invoices (
    id              BIGSERIAL PRIMARY KEY,
    invoice_number  VARCHAR(64) NOT NULL UNIQUE,          -- ML-BALD-202601-001
    customer_id     BIGINT NOT NULL REFERENCES billing.customers(id),
    contract_id     BIGINT NOT NULL REFERENCES billing.contracts(id),
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    status          billing.invoice_status NOT NULL DEFAULT 'draft',
    subtotal        NUMERIC(12,2) NOT NULL,
    tax             NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    total           NUMERIC(12,2) NOT NULL,
    currency        VARCHAR(3) NOT NULL DEFAULT 'USD',
    stripe_invoice_id VARCHAR(128),
    due_date        DATE NOT NULL,
    notes           TEXT,
    usage_summary   JSONB,                                -- total_kwh, peak_kw, avg_pue, availability_pct
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,

    CONSTRAINT ck_invoices_period CHECK (period_end > period_start)
);

CREATE INDEX ix_invoices_customer_period ON billing.invoices (customer_id, period_start);
CREATE INDEX ix_invoices_status ON billing.invoices (status);

-- ──────────────────────────────
-- LINE ITEMS
-- ──────────────────────────────

CREATE TABLE billing.invoice_line_items (
    id              BIGSERIAL PRIMARY KEY,
    invoice_id      BIGINT NOT NULL REFERENCES billing.invoices(id),
    line_type       billing.line_item_type NOT NULL,
    description     VARCHAR(512) NOT NULL,
    quantity        NUMERIC(14,4) NOT NULL,
    unit            VARCHAR(32) NOT NULL,                 -- kW, kWh, kWht, month, ea, pct
    unit_price      NUMERIC(12,6) NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,               -- qty × unit_price (negative for credits)
    sort_order      INTEGER NOT NULL DEFAULT 0,
    metadata_json   JSONB                                 -- meter reading count, quality flags, etc.
);

CREATE INDEX ix_line_items_invoice ON billing.invoice_line_items (invoice_id);

-- ──────────────────────────────
-- HOST AGREEMENTS
-- ──────────────────────────────

CREATE TABLE billing.host_agreements (
    id                      BIGSERIAL PRIMARY KEY,
    contract_id             BIGINT NOT NULL UNIQUE REFERENCES billing.contracts(id),
    heat_pricing_model      billing.heat_pricing_model NOT NULL,
    host_energy_rate        NUMERIC(10,6) NOT NULL,       -- $/kWh of displaced fuel
    displacement_efficiency NUMERIC(4,3) NOT NULL DEFAULT 0.800,
    revenue_share_pct       NUMERIC(5,4) NOT NULL DEFAULT 0.0000,
    budget_neutral_threshold NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    host_existing_efficiency NUMERIC(4,3) NOT NULL DEFAULT 0.850, -- gas boiler default
    grid_emission_factor    NUMERIC(6,4) NOT NULL DEFAULT 0.2400  -- US-NY kg CO₂/kWh
);

-- ──────────────────────────────
-- BILLING METERS
-- ──────────────────────────────

CREATE TABLE billing.billing_meters (
    id              BIGSERIAL PRIMARY KEY,
    contract_id     BIGINT NOT NULL REFERENCES billing.contracts(id),
    sensor_tag      VARCHAR(64) NOT NULL,                 -- MET-01, MET-T1
    meter_type      billing.meter_type NOT NULL,
    is_primary      BOOLEAN NOT NULL DEFAULT TRUE,
    description     VARCHAR(256),
    accuracy_class  VARCHAR(16),                          -- IEC accuracy class, e.g. 0.5
    last_calibration DATE,

    CONSTRAINT uq_billing_meter_sensor UNIQUE (contract_id, sensor_tag)
);

CREATE INDEX ix_billing_meters_sensor ON billing.billing_meters (sensor_tag);

-- ──────────────────────────────
-- PLANNED MAINTENANCE
-- ──────────────────────────────

CREATE TABLE billing.planned_maintenance (
    id                  BIGSERIAL PRIMARY KEY,
    block_id            VARCHAR(64) NOT NULL,
    site_id             VARCHAR(64) NOT NULL,
    start_at            TIMESTAMPTZ NOT NULL,
    end_at              TIMESTAMPTZ NOT NULL,
    notice_sent_at      TIMESTAMPTZ,
    description         TEXT NOT NULL,
    affected_customers  BIGINT[],                         -- NULL = all customers on block
    created_by          VARCHAR(128),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_maintenance_window CHECK (end_at > start_at)
);

CREATE INDEX ix_planned_maintenance_block_time
    ON billing.planned_maintenance (block_id, start_at, end_at);

-- ──────────────────────────────
-- MANUAL ADJUSTMENTS
-- ──────────────────────────────

CREATE TABLE billing.manual_adjustments (
    id                      BIGSERIAL PRIMARY KEY,
    customer_id             BIGINT NOT NULL REFERENCES billing.customers(id),
    contract_id             BIGINT NOT NULL REFERENCES billing.contracts(id),
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    description             VARCHAR(512) NOT NULL,
    amount                  NUMERIC(12,2) NOT NULL,       -- positive = charge, negative = credit
    approved_by             VARCHAR(128),
    applied_to_invoice_id   BIGINT REFERENCES billing.invoices(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────
-- EMISSION FACTORS (config table)
-- ──────────────────────────────

CREATE TABLE billing.emission_factors (
    id                  BIGSERIAL PRIMARY KEY,
    region_code         VARCHAR(32) NOT NULL,              -- US-NY, EU, ZA
    factor_kg_per_kwh   NUMERIC(6,4) NOT NULL,
    source              VARCHAR(256),
    effective_year      INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_emission_factor_region_year UNIQUE (region_code, effective_year)
);

-- ──────────────────────────────
-- SEED: emission factors
-- ──────────────────────────────

INSERT INTO billing.emission_factors (region_code, factor_kg_per_kwh, source, effective_year) VALUES
    ('US-NY',  0.2400, 'EPA eGRID 2024 — NYUP subregion',     2025),
    ('US-AVG', 0.3900, 'EPA eGRID 2024 — US average',         2025),
    ('EU',     0.2300, 'EEA 2024 — EU27 average',             2025),
    ('ZA',     0.9500, 'Eskom 2024 — South Africa grid',      2025),
    ('UK',     0.2100, 'DEFRA 2024 — UK grid average',        2025);

-- ──────────────────────────────
-- SEED: Baldwinsville pilot data
-- ──────────────────────────────

-- AB InBev as host customer
INSERT INTO billing.customers (tenant_id, name, billing_contact_email, currency) VALUES
    ('abinbev-baldwinsville', 'Anheuser-Busch InBev — Baldwinsville Brewery', 'utilities@ab-inbev.com', 'USD');

-- Host agreement contract
INSERT INTO billing.contracts (customer_id, contract_type, site_id, contract_ref, start_date, status, payment_terms_days, tax_rate_pct) VALUES
    (1, 'host_agreement', 'BALD-01', 'ML-BALD-2026-H01', '2026-03-01', 'draft', 30, 0.0000);

-- Host agreement terms
INSERT INTO billing.host_agreements (contract_id, heat_pricing_model, host_energy_rate, displacement_efficiency, revenue_share_pct, budget_neutral_threshold, host_existing_efficiency, grid_emission_factor) VALUES
    (1, 'credit', 0.065000, 0.850, 0.0500, 0.00, 0.850, 0.2400);

-- ──────────────────────────────
-- UPDATED_AT trigger
-- ──────────────────────────────

CREATE OR REPLACE FUNCTION billing.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON billing.customers
    FOR EACH ROW EXECUTE FUNCTION billing.set_updated_at();

CREATE TRIGGER trg_contracts_updated_at
    BEFORE UPDATE ON billing.contracts
    FOR EACH ROW EXECUTE FUNCTION billing.set_updated_at();
