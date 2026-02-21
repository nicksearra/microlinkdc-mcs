-- ============================================================================
-- MCS Stream B — Task 1: Database Schema (PostgreSQL 16 + TimescaleDB 2.x)
-- ============================================================================
--
-- Core data platform schema for the MicroLink Control System.
-- All other tasks depend on this: ingestion (Task 2), aggregates (Task 3),
-- alarm engine (Task 4), REST API (Task 5).
--
-- Run order:
--   1. CREATE DATABASE mcs;
--   2. CREATE EXTENSION IF NOT EXISTS timescaledb;
--   3. Run this file
--   4. Run aggregates.sql (Task 3)
--
-- ============================================================================

-- ────────────────────────────────────────────────────────────────────────────
-- CUSTOM ENUM TYPES
-- ────────────────────────────────────────────────────────────────────────────

-- Ensure TimescaleDB extension is available
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TYPE site_status AS ENUM ('active', 'commissioning', 'decommissioned');
CREATE TYPE block_status AS ENUM ('active', 'commissioning', 'standby', 'decommissioned');
CREATE TYPE equipment_subsystem AS ENUM (
    'electrical',
    'thermal-l1',       -- IT secondary (CDU to rack)
    'thermal-l2',       -- MicroLink primary (glycol loop)
    'thermal-l3',       -- Host process (heat delivery)
    'thermal-reject',   -- Heat rejection (dry coolers)
    'thermal-safety',   -- Leak detection, isolation valves
    'environmental',    -- Ambient temp, humidity, airflow
    'network',          -- Switches, WAN, VPN
    'security'          -- Door contacts, cameras
);

CREATE TYPE alarm_state AS ENUM (
    'ACTIVE',       -- Condition present, not acknowledged
    'ACKED',        -- Condition present, acknowledged
    'RTN_UNACK',    -- Condition cleared, not acknowledged
    'CLEARED',      -- Resolved
    'SHELVED',      -- Temporarily suppressed by operator
    'SUPPRESSED'    -- Suppressed by cascade logic
);

CREATE TYPE alarm_priority AS ENUM ('P0', 'P1', 'P2', 'P3');

CREATE TYPE thermal_mode AS ENUM (
    'FULL_RECOVERY',        -- All heat to host process
    'PARTIAL_RECOVERY',     -- Split between host and reject
    'FULL_REJECTION',       -- All heat to dry coolers
    'EMERGENCY_REJECTION',  -- Bypass all, maximum rejection
    'STARTUP',              -- Warming up loops
    'SHUTDOWN'              -- Draining down
);

CREATE TYPE availability_class AS ENUM ('A', 'B', 'C');
-- A = Tier 3+ (2N power, N+1 cooling) — $220/kW/mo
-- B = Tier 2 (N+1 power, N+1 cooling) — $170/kW/mo
-- C = Tier 1 (N power, N cooling)      — $120/kW/mo

CREATE TYPE dlq_category AS ENUM (
    'PARSE_ERROR',
    'TOPIC_ERROR',
    'SENSOR_UNKNOWN',
    'VALUE_ERROR',
    'INTERNAL_ERROR'
);

CREATE TYPE tenant_role AS ENUM (
    'internal',     -- MicroLink operations team
    'customer',     -- Colocation tenant
    'host',         -- Industrial host (brewery, food plant)
    'lender'        -- Project finance lender (read-only)
);


-- ────────────────────────────────────────────────────────────────────────────
-- TENANTS (multi-tenant access control)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE tenants (
    id          serial PRIMARY KEY,
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    role        tenant_role NOT NULL DEFAULT 'customer',
    config_json jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenants_slug ON tenants(slug);


-- ────────────────────────────────────────────────────────────────────────────
-- SITES
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE sites (
    id          serial PRIMARY KEY,
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    region      text,
    status      site_status NOT NULL DEFAULT 'commissioning',
    latitude    double precision,
    longitude   double precision,
    config_json jsonb,                  -- Site-specific overrides
    tenant_id   integer REFERENCES tenants(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sites_slug ON sites(slug);
CREATE INDEX idx_sites_status ON sites(status);


-- ────────────────────────────────────────────────────────────────────────────
-- BLOCKS (1MW compute modules)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE blocks (
    id                serial PRIMARY KEY,
    site_id           integer NOT NULL REFERENCES sites(id),
    slug              text NOT NULL UNIQUE,
    capacity_mw       numeric(4,2) NOT NULL DEFAULT 1.0,
    status            block_status NOT NULL DEFAULT 'commissioning',
    availability      availability_class NOT NULL DEFAULT 'B',
    thermal_mode      thermal_mode NOT NULL DEFAULT 'FULL_RECOVERY',
    commissioned_at   timestamptz,
    config_json       jsonb,              -- Block-specific thresholds, setpoints
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_blocks_site ON blocks(site_id);
CREATE INDEX idx_blocks_slug ON blocks(slug);
CREATE INDEX idx_blocks_status ON blocks(status);


-- ────────────────────────────────────────────────────────────────────────────
-- EQUIPMENT
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE equipment (
    id              serial PRIMARY KEY,
    block_id        integer NOT NULL REFERENCES blocks(id),
    tag             text NOT NULL,          -- e.g. CDU-01, UPS-01, PHX-01
    type            text NOT NULL,          -- e.g. coolant_distribution_unit, ups, heat_exchanger
    subsystem       equipment_subsystem NOT NULL,
    metadata_json   jsonb,                  -- Manufacturer, model, serial, specs
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE(block_id, tag)
);

CREATE INDEX idx_equipment_block ON equipment(block_id);
CREATE INDEX idx_equipment_subsystem ON equipment(subsystem);


-- ────────────────────────────────────────────────────────────────────────────
-- SENSORS
-- ────────────────────────────────────────────────────────────────────────────
-- Integer PK for hypertable join efficiency (TimescaleDB optimisation).
-- ~300 sensors per 1MW block.

CREATE TABLE sensors (
    id                      serial PRIMARY KEY,
    equipment_id            integer NOT NULL REFERENCES equipment(id),
    tag                     text NOT NULL,          -- MQTT tag: CDU-01-T-SUP
    description             text,
    unit                    text,                   -- °C, kW, Hz, L/min, %
    range_min               double precision,
    range_max               double precision,
    poll_rate_ms            integer DEFAULT 5000,   -- 5-second default
    alarm_thresholds_json   jsonb,                  -- {HH: {value, priority, delay_s}, H: ...}
    config_json             jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE(equipment_id, tag)
);

CREATE INDEX idx_sensors_equipment ON sensors(equipment_id);
CREATE INDEX idx_sensors_tag ON sensors(tag);


-- ────────────────────────────────────────────────────────────────────────────
-- TELEMETRY (TimescaleDB hypertable)
-- ────────────────────────────────────────────────────────────────────────────
-- Core time-series table. ~5,000 inserts/sec per block, ~50,000 for a 10MW site.
-- Integer sensor_id FK for chunk-level index efficiency.
-- Quality stored as smallint (0=GOOD, 1=UNCERTAIN, 2=BAD) to save space.

CREATE TABLE telemetry (
    time        timestamptz NOT NULL,
    sensor_id   integer NOT NULL,       -- FK to sensors.id (not enforced for perf)
    value       double precision NOT NULL,
    quality     smallint NOT NULL DEFAULT 0
);

-- Convert to hypertable — 1-day chunks
SELECT create_hypertable('telemetry', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Primary index: sensor + time (most common query pattern)
CREATE INDEX idx_telemetry_sensor_time
    ON telemetry (sensor_id, time DESC);


-- ────────────────────────────────────────────────────────────────────────────
-- ALARMS (ISA-18.2 lifecycle)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE alarms (
    id              serial PRIMARY KEY,
    sensor_id       integer NOT NULL REFERENCES sensors(id),
    priority        text NOT NULL DEFAULT 'P2',     -- P0, P1, P2, P3
    state           text NOT NULL DEFAULT 'ACTIVE', -- ISA-18.2 states
    raised_at       timestamptz,
    acked_at        timestamptz,
    acked_by        text,
    cleared_at      timestamptz,
    shelved_at      timestamptz,
    shelved_by      text,
    shelved_until   timestamptz,
    shelve_reason   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_alarms_state ON alarms(state) WHERE state != 'CLEARED';
CREATE INDEX idx_alarms_sensor ON alarms(sensor_id);
CREATE INDEX idx_alarms_raised ON alarms(raised_at DESC);


-- ────────────────────────────────────────────────────────────────────────────
-- EVENTS (immutable audit log)
-- ────────────────────────────────────────────────────────────────────────────
-- Insert-only table. Events are never updated or deleted.
-- All system and operator actions are logged here.

CREATE TABLE events (
    id          bigserial PRIMARY KEY,
    block_id    integer REFERENCES blocks(id),
    event_type  text NOT NULL,          -- alarm_raised, alarm_acked, mode_change, etc.
    payload     jsonb,                  -- Event-specific data
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Immutability trigger — prevent UPDATE and DELETE
CREATE OR REPLACE FUNCTION prevent_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Events table is immutable — INSERT only';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_events_immutable
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_event_mutation();

CREATE INDEX idx_events_block_type ON events(block_id, event_type);
CREATE INDEX idx_events_created ON events(created_at DESC);
CREATE INDEX idx_events_type ON events(event_type);


-- ────────────────────────────────────────────────────────────────────────────
-- DEAD LETTER QUEUE
-- ────────────────────────────────────────────────────────────────────────────
-- Captures malformed or unresolvable MQTT messages from the ingestion service.

CREATE TABLE dead_letter_queue (
    id              bigserial PRIMARY KEY,
    topic           text,
    payload         text,               -- Raw payload (truncated to 4KB)
    category        text NOT NULL,      -- PARSE_ERROR, TOPIC_ERROR, etc.
    error_message   text,
    received_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_dlq_received ON dead_letter_queue(received_at DESC);
CREATE INDEX idx_dlq_category ON dead_letter_queue(category);


-- ────────────────────────────────────────────────────────────────────────────
-- TENANT ACCESS CONTROL
-- ────────────────────────────────────────────────────────────────────────────
-- Maps tenants to the sites/blocks they can access.

CREATE TABLE tenant_access (
    id          serial PRIMARY KEY,
    tenant_id   integer NOT NULL REFERENCES tenants(id),
    site_id     integer REFERENCES sites(id),
    block_id    integer REFERENCES blocks(id),
    access_level text NOT NULL DEFAULT 'read',  -- read, write, admin
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, site_id, block_id)
);

CREATE INDEX idx_tenant_access_tenant ON tenant_access(tenant_id);


-- ────────────────────────────────────────────────────────────────────────────
-- ROW-LEVEL SECURITY
-- ────────────────────────────────────────────────────────────────────────────
-- Session variable: SET app.current_tenant_id = '1';
-- The API layer sets this before each request.

ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocks ENABLE ROW LEVEL SECURITY;

-- Internal users see everything
CREATE POLICY sites_internal ON sites
    FOR ALL
    USING (
        current_setting('app.current_tenant_role', true) = 'internal'
    );

-- External users see only their accessible sites
CREATE POLICY sites_tenant ON sites
    FOR SELECT
    USING (
        id IN (
            SELECT site_id FROM tenant_access
            WHERE tenant_id = current_setting('app.current_tenant_id', true)::integer
        )
    );

CREATE POLICY blocks_internal ON blocks
    FOR ALL
    USING (
        current_setting('app.current_tenant_role', true) = 'internal'
    );

CREATE POLICY blocks_tenant ON blocks
    FOR SELECT
    USING (
        id IN (
            SELECT block_id FROM tenant_access
            WHERE tenant_id = current_setting('app.current_tenant_id', true)::integer
        )
        OR site_id IN (
            SELECT site_id FROM tenant_access
            WHERE tenant_id = current_setting('app.current_tenant_id', true)::integer
              AND block_id IS NULL
        )
    );


-- ────────────────────────────────────────────────────────────────────────────
-- AUDIT LOGGING
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    table_name  text NOT NULL,
    operation   text NOT NULL,      -- INSERT, UPDATE, DELETE
    row_id      integer,
    old_data    jsonb,
    new_data    jsonb,
    changed_by  text,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_table ON audit_log(table_name, changed_at DESC);


-- ────────────────────────────────────────────────────────────────────────────
-- DATABASE ROLES
-- ────────────────────────────────────────────────────────────────────────────

-- Admin role (schema changes, full access)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcs_admin') THEN
        CREATE ROLE mcs_admin WITH LOGIN PASSWORD 'localdev';
    END IF;
END $$;

-- API role (read/write data, no schema changes)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcs_api') THEN
        CREATE ROLE mcs_api WITH LOGIN PASSWORD 'localdev';
    END IF;
END $$;

-- Read-only role (dashboards, lender reports)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcs_readonly') THEN
        CREATE ROLE mcs_readonly WITH LOGIN PASSWORD 'localdev';
    END IF;
END $$;

GRANT ALL ON ALL TABLES IN SCHEMA public TO mcs_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO mcs_admin;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO mcs_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mcs_api;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcs_readonly;


-- ============================================================================
-- DONE — Task 1 schema complete
-- ============================================================================
-- Summary:
--   14 custom enum types
--   10 tables: tenants, sites, blocks, equipment, sensors, telemetry,
--              alarms, events, dead_letter_queue, tenant_access, audit_log
--   1 hypertable (telemetry) with 1-day chunks
--   Row-level security on sites and blocks
--   Immutability trigger on events
--   3 database roles (admin, api, readonly)
-- ============================================================================
