-- ============================================================================
-- MCS Stream B — Task 3: Continuous Aggregates & Retention Policies
-- ============================================================================
--
-- Builds the rollup pyramid on top of the telemetry hypertable (Task 1).
-- Each tier materialises pre-computed statistics so dashboards and APIs
-- never touch raw data unless they need sub-minute resolution.
--
-- Pyramid:
--   telemetry (raw, 5-sec)  →  90-day retention
--     └─ agg_1min           →  2-year retention
--         └─ agg_5min       →  2-year retention   ← billing queries hit here
--             └─ agg_1hour  →  7-year retention
--                 └─ agg_1day → 7-year retention   ← lender reports, ESG
--
-- Each aggregate stores: avg, min, max, last, count, stddev, sum
-- "last" is critical for dashboard "current value" displays.
-- "sum" is critical for energy counters (kWh, kWht).
--
-- Requires: TimescaleDB 2.x with continuous aggregates support.
-- Run AFTER schema.sql (Task 1).
-- ============================================================================

-- ────────────────────────────────────────────────────────────────────────────
-- 1. ONE-MINUTE AGGREGATE
-- ────────────────────────────────────────────────────────────────────────────
-- Base tier — aggregates raw telemetry into 1-minute buckets.
-- Refresh lag: 2 minutes (allows late-arriving data from store-and-forward).

CREATE MATERIALIZED VIEW agg_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time)   AS bucket,
    sensor_id,
    avg(value)                      AS val_avg,
    min(value)                      AS val_min,
    max(value)                      AS val_max,
    last(value, time)               AS val_last,
    count(*)                        AS sample_count,
    stddev(value)                   AS val_stddev,
    sum(value)                      AS val_sum,
    -- Quality: fraction of GOOD samples (quality=0)
    avg(CASE WHEN quality = 0 THEN 1.0 ELSE 0.0 END) AS quality_good_ratio
FROM telemetry
GROUP BY bucket, sensor_id
WITH NO DATA;

-- Refresh policy: materialise every 1 minute, with a 2-minute lag
-- (lag accounts for store-and-forward edge delays)
SELECT add_continuous_aggregate_policy('agg_1min',
    start_offset    => INTERVAL '10 minutes',
    end_offset      => INTERVAL '2 minutes',
    schedule_interval => INTERVAL '1 minute'
);

COMMENT ON MATERIALIZED VIEW agg_1min IS
    'MCS 1-minute rollup — base aggregate tier. NOC dashboards use this for recent trends.';


-- ────────────────────────────────────────────────────────────────────────────
-- 2. FIVE-MINUTE AGGREGATE
-- ────────────────────────────────────────────────────────────────────────────
-- Primary tier for billing and operational queries.
-- Stream C's kWh/kWht billing calculations consume this.

CREATE MATERIALIZED VIEW agg_5min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', bucket) AS bucket,
    sensor_id,
    avg(val_avg)                     AS val_avg,
    min(val_min)                     AS val_min,
    max(val_max)                     AS val_max,
    last(val_last, bucket)           AS val_last,
    sum(sample_count)                AS sample_count,
    -- Weighted stddev approximation (sufficient for monitoring)
    avg(val_stddev)                  AS val_stddev,
    sum(val_sum)                     AS val_sum,
    avg(quality_good_ratio)          AS quality_good_ratio
FROM agg_1min
GROUP BY time_bucket('5 minutes', bucket), sensor_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agg_5min',
    start_offset    => INTERVAL '30 minutes',
    end_offset      => INTERVAL '10 minutes',
    schedule_interval => INTERVAL '5 minutes'
);

COMMENT ON MATERIALIZED VIEW agg_5min IS
    'MCS 5-minute rollup — billing-grade data. Stream C kWh/kWht calculations use this tier.';


-- ────────────────────────────────────────────────────────────────────────────
-- 3. ONE-HOUR AGGREGATE
-- ────────────────────────────────────────────────────────────────────────────
-- Used for weekly/monthly trend charts, capacity planning, thermal analysis.

CREATE MATERIALIZED VIEW agg_1hour
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', bucket)    AS bucket,
    sensor_id,
    avg(val_avg)                     AS val_avg,
    min(val_min)                     AS val_min,
    max(val_max)                     AS val_max,
    last(val_last, bucket)           AS val_last,
    sum(sample_count)                AS sample_count,
    avg(val_stddev)                  AS val_stddev,
    sum(val_sum)                     AS val_sum,
    avg(quality_good_ratio)          AS quality_good_ratio
FROM agg_5min
GROUP BY time_bucket('1 hour', bucket), sensor_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agg_1hour',
    start_offset    => INTERVAL '4 hours',
    end_offset      => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

COMMENT ON MATERIALIZED VIEW agg_1hour IS
    'MCS 1-hour rollup — trend analysis, capacity planning, monthly reports.';


-- ────────────────────────────────────────────────────────────────────────────
-- 4. ONE-DAY AGGREGATE
-- ────────────────────────────────────────────────────────────────────────────
-- Used for lender reports, ESG calculations, long-term trend analysis.

CREATE MATERIALIZED VIEW agg_1day
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', bucket)     AS bucket,
    sensor_id,
    avg(val_avg)                     AS val_avg,
    min(val_min)                     AS val_min,
    max(val_max)                     AS val_max,
    last(val_last, bucket)           AS val_last,
    sum(sample_count)                AS sample_count,
    avg(val_stddev)                  AS val_stddev,
    sum(val_sum)                     AS val_sum,
    avg(quality_good_ratio)          AS quality_good_ratio
FROM agg_1hour
GROUP BY time_bucket('1 day', bucket), sensor_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agg_1day',
    start_offset    => INTERVAL '3 days',
    end_offset      => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day'
);

COMMENT ON MATERIALIZED VIEW agg_1day IS
    'MCS 1-day rollup — lender reports, ESG, 7-year retention tier.';


-- ============================================================================
-- 5. RETENTION POLICIES
-- ============================================================================
-- Raw data: 90 days (high volume, expensive to store)
-- 1-min / 5-min aggregates: 2 years (operational queries)
-- 1-hour / 1-day aggregates: 7 years (compliance, lender reporting)
--
-- TimescaleDB drops entire chunks, so this is extremely fast.

SELECT add_retention_policy('telemetry',  drop_after => INTERVAL '90 days');
SELECT add_retention_policy('agg_1min',   drop_after => INTERVAL '2 years');
SELECT add_retention_policy('agg_5min',   drop_after => INTERVAL '2 years');
SELECT add_retention_policy('agg_1hour',  drop_after => INTERVAL '7 years');
SELECT add_retention_policy('agg_1day',   drop_after => INTERVAL '7 years');


-- ============================================================================
-- 6. BILLING VIEWS
-- ============================================================================
-- Pre-built views that Stream C's billing module queries directly.
-- These join aggregates back to the sensor/equipment/block hierarchy
-- to provide per-customer, per-rack energy data.

-- ── 6a. Electrical energy (kWh) per PDU — 5-minute intervals ────────────
-- Billing accuracy: the sum of power readings × interval = energy.
-- For power meters (unit = 'kW'), energy = avg_power × (5/60) hours.
-- For energy counters (unit = 'kWh'), use the difference between
-- the last values at interval boundaries.

CREATE OR REPLACE VIEW billing_kwh_5min AS
SELECT
    a.bucket,
    s.id        AS sensor_id,
    s.tag       AS sensor_tag,
    e.tag       AS equipment_tag,
    e.subsystem,
    b.id        AS block_id,
    b.slug      AS block_slug,
    st.id       AS site_id,
    st.slug     AS site_slug,
    -- For power sensors: average power × 5-min interval = kWh
    CASE
        WHEN s.unit = 'kW'  THEN a.val_avg * (5.0 / 60.0)
        WHEN s.unit = 'kWh' THEN a.val_last  -- counter value (delta computed by Stream C)
        ELSE NULL
    END AS kwh_value,
    s.unit,
    a.sample_count,
    a.quality_good_ratio
FROM agg_5min a
JOIN sensors s    ON s.id = a.sensor_id
JOIN equipment e  ON e.id = s.equipment_id
JOIN blocks b     ON b.id = e.block_id
JOIN sites st     ON st.id = b.site_id
WHERE e.subsystem = 'electrical'
  AND s.unit IN ('kW', 'kWh');

COMMENT ON VIEW billing_kwh_5min IS
    'Stream C billing input — 5-minute electrical energy data per metering point.';


-- ── 6b. Thermal energy (kWht) per thermal meter — 5-minute intervals ────
-- For thermal power sensors (unit = 'kW' in thermal subsystems):
--   kWht = avg_thermal_power × (5/60)
-- For thermal energy counters (unit = 'kWh' in thermal subsystems):
--   use counter value directly (delta by Stream C)

CREATE OR REPLACE VIEW billing_kwht_5min AS
SELECT
    a.bucket,
    s.id        AS sensor_id,
    s.tag       AS sensor_tag,
    e.tag       AS equipment_tag,
    e.subsystem,
    b.id        AS block_id,
    b.slug      AS block_slug,
    st.id       AS site_id,
    st.slug     AS site_slug,
    CASE
        WHEN s.unit = 'kW'  THEN a.val_avg * (5.0 / 60.0)
        WHEN s.unit = 'kWh' THEN a.val_last
        ELSE NULL
    END AS kwht_value,
    s.unit,
    a.sample_count,
    a.quality_good_ratio
FROM agg_5min a
JOIN sensors s    ON s.id = a.sensor_id
JOIN equipment e  ON e.id = s.equipment_id
JOIN blocks b     ON b.id = e.block_id
JOIN sites st     ON st.id = b.site_id
WHERE e.subsystem IN ('thermal-l1', 'thermal-l2', 'thermal-l3')
  AND s.unit IN ('kW', 'kWh');

COMMENT ON VIEW billing_kwht_5min IS
    'Stream C billing input — 5-minute thermal energy data per metering point.';


-- ── 6c. Daily energy summary per block ──────────────────────────────────
-- Convenience view for lender reports and ESG calculations.

CREATE OR REPLACE VIEW energy_daily_summary AS
SELECT
    a.bucket                          AS day,
    b.id                              AS block_id,
    b.slug                            AS block_slug,
    st.id                             AS site_id,
    st.slug                           AS site_slug,
    -- Electrical: sum of all power meter averages × 24h
    SUM(CASE
        WHEN e.subsystem = 'electrical' AND s.unit = 'kW'
        THEN a.val_avg * 24.0
        ELSE 0
    END)                              AS electrical_kwh,
    -- Thermal recovered: sum of L3 (host) thermal power × 24h
    SUM(CASE
        WHEN e.subsystem = 'thermal-l3' AND s.unit = 'kW'
        THEN a.val_avg * 24.0
        ELSE 0
    END)                              AS thermal_kwht_recovered,
    -- Thermal rejected: sum of reject loop power × 24h
    SUM(CASE
        WHEN e.subsystem = 'thermal-reject' AND s.unit = 'kW'
        THEN a.val_avg * 24.0
        ELSE 0
    END)                              AS thermal_kwht_rejected,
    -- Heat recovery ratio
    CASE
        WHEN SUM(CASE WHEN e.subsystem = 'electrical' AND s.unit = 'kW' THEN a.val_avg ELSE 0 END) > 0
        THEN SUM(CASE WHEN e.subsystem = 'thermal-l3' AND s.unit = 'kW' THEN a.val_avg ELSE 0 END)
           / SUM(CASE WHEN e.subsystem = 'electrical' AND s.unit = 'kW' THEN a.val_avg ELSE 0 END)
        ELSE 0
    END                               AS heat_recovery_ratio,
    -- PUE proxy (total electrical / IT load)
    CASE
        WHEN SUM(CASE WHEN s.tag LIKE 'P-MSB-TOTAL%' THEN a.val_avg ELSE 0 END) > 0
        THEN SUM(CASE WHEN e.subsystem = 'electrical' AND s.unit = 'kW' THEN a.val_avg ELSE 0 END)
           / NULLIF(SUM(CASE WHEN s.tag LIKE 'P-MSB-TOTAL%' THEN a.val_avg ELSE 0 END), 0)
        ELSE NULL
    END                               AS pue_estimate
FROM agg_1day a
JOIN sensors s    ON s.id = a.sensor_id
JOIN equipment e  ON e.id = s.equipment_id
JOIN blocks b     ON b.id = e.block_id
JOIN sites st     ON st.id = b.site_id
WHERE s.unit IN ('kW', 'kWh')
GROUP BY a.bucket, b.id, b.slug, st.id, st.slug;

COMMENT ON VIEW energy_daily_summary IS
    'Daily energy summary per block — electrical kWh, thermal recovery, PUE. Used by lender reports and ESG.';


-- ============================================================================
-- 7. HELPER FUNCTIONS
-- ============================================================================

-- ── 7a. Query router — automatically selects the best aggregate tier ────
-- Given a time range, returns the name of the most efficient aggregate.
-- API layer uses this to avoid scanning raw data unnecessarily.

CREATE OR REPLACE FUNCTION select_aggregate_tier(
    p_start timestamptz,
    p_end   timestamptz
) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    span interval;
BEGIN
    span := p_end - p_start;

    -- < 2 hours: use raw telemetry (sub-second resolution)
    IF span < INTERVAL '2 hours' THEN
        RETURN 'telemetry';
    -- < 12 hours: use 1-minute aggregates
    ELSIF span < INTERVAL '12 hours' THEN
        RETURN 'agg_1min';
    -- < 3 days: use 5-minute aggregates
    ELSIF span < INTERVAL '3 days' THEN
        RETURN 'agg_5min';
    -- < 90 days: use hourly aggregates
    ELSIF span < INTERVAL '90 days' THEN
        RETURN 'agg_1hour';
    -- > 90 days: use daily aggregates
    ELSE
        RETURN 'agg_1day';
    END IF;
END;
$$;

COMMENT ON FUNCTION select_aggregate_tier IS
    'Returns the optimal aggregate view name for a given time range. Used by the REST API query router.';


-- ── 7b. Telemetry query with automatic tier selection ───────────────────
-- Returns aggregated data from the optimal tier for the given range.
-- This is the primary function the REST API calls for /telemetry endpoint.

CREATE OR REPLACE FUNCTION query_telemetry(
    p_sensor_id  integer,
    p_start      timestamptz,
    p_end        timestamptz,
    p_agg        text DEFAULT NULL  -- override tier: '1min', '5min', '1hour', '1day', 'raw'
) RETURNS TABLE (
    bucket          timestamptz,
    val_avg         double precision,
    val_min         double precision,
    val_max         double precision,
    val_last        double precision,
    sample_count    bigint,
    quality_ratio   double precision
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    tier text;
BEGIN
    -- Determine tier
    IF p_agg IS NOT NULL THEN
        tier := CASE p_agg
            WHEN 'raw'   THEN 'telemetry'
            WHEN '1min'  THEN 'agg_1min'
            WHEN '5min'  THEN 'agg_5min'
            WHEN '1hour' THEN 'agg_1hour'
            WHEN '1day'  THEN 'agg_1day'
            ELSE 'agg_5min'
        END;
    ELSE
        tier := select_aggregate_tier(p_start, p_end);
    END IF;

    -- Route to appropriate table/view
    IF tier = 'telemetry' THEN
        RETURN QUERY
            SELECT
                t.time AS bucket,
                t.value::double precision AS val_avg,
                t.value::double precision AS val_min,
                t.value::double precision AS val_max,
                t.value::double precision AS val_last,
                1::bigint AS sample_count,
                CASE WHEN t.quality = 0 THEN 1.0 ELSE 0.0 END AS quality_ratio
            FROM telemetry t
            WHERE t.sensor_id = p_sensor_id
              AND t.time >= p_start
              AND t.time < p_end
            ORDER BY t.time;

    ELSIF tier = 'agg_1min' THEN
        RETURN QUERY
            SELECT a.bucket, a.val_avg, a.val_min, a.val_max, a.val_last,
                   a.sample_count, a.quality_good_ratio
            FROM agg_1min a
            WHERE a.sensor_id = p_sensor_id
              AND a.bucket >= p_start AND a.bucket < p_end
            ORDER BY a.bucket;

    ELSIF tier = 'agg_5min' THEN
        RETURN QUERY
            SELECT a.bucket, a.val_avg, a.val_min, a.val_max, a.val_last,
                   a.sample_count, a.quality_good_ratio
            FROM agg_5min a
            WHERE a.sensor_id = p_sensor_id
              AND a.bucket >= p_start AND a.bucket < p_end
            ORDER BY a.bucket;

    ELSIF tier = 'agg_1hour' THEN
        RETURN QUERY
            SELECT a.bucket, a.val_avg, a.val_min, a.val_max, a.val_last,
                   a.sample_count, a.quality_good_ratio
            FROM agg_1hour a
            WHERE a.sensor_id = p_sensor_id
              AND a.bucket >= p_start AND a.bucket < p_end
            ORDER BY a.bucket;

    ELSIF tier = 'agg_1day' THEN
        RETURN QUERY
            SELECT a.bucket, a.val_avg, a.val_min, a.val_max, a.val_last,
                   a.sample_count, a.quality_good_ratio
            FROM agg_1day a
            WHERE a.sensor_id = p_sensor_id
              AND a.bucket >= p_start AND a.bucket < p_end
            ORDER BY a.bucket;
    END IF;
END;
$$;

COMMENT ON FUNCTION query_telemetry IS
    'Primary telemetry query function — auto-selects optimal aggregate tier. Called by REST API /telemetry endpoint.';


-- ============================================================================
-- 8. INDEXES ON CONTINUOUS AGGREGATES
-- ============================================================================
-- TimescaleDB creates time-based indexes automatically, but we add
-- composite indexes for the common query patterns.

CREATE INDEX IF NOT EXISTS idx_agg_1min_sensor_bucket
    ON agg_1min (sensor_id, bucket DESC);

CREATE INDEX IF NOT EXISTS idx_agg_5min_sensor_bucket
    ON agg_5min (sensor_id, bucket DESC);

CREATE INDEX IF NOT EXISTS idx_agg_1hour_sensor_bucket
    ON agg_1hour (sensor_id, bucket DESC);

CREATE INDEX IF NOT EXISTS idx_agg_1day_sensor_bucket
    ON agg_1day (sensor_id, bucket DESC);


-- ============================================================================
-- 9. COMPRESSION POLICIES (for older aggregate data)
-- ============================================================================
-- Compress older chunks to save disk space.  Compressed data is still
-- queryable — TimescaleDB decompresses on-the-fly.

-- Compress raw telemetry after 7 days (still within 90-day retention)
ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('telemetry', compress_after => INTERVAL '7 days');

-- Compress 1-min aggregates after 30 days
ALTER MATERIALIZED VIEW agg_1min SET (
    timescaledb.compress = true
);
SELECT add_compression_policy('agg_1min', compress_after => INTERVAL '30 days');

-- Compress 5-min aggregates after 90 days
ALTER MATERIALIZED VIEW agg_5min SET (
    timescaledb.compress = true
);
SELECT add_compression_policy('agg_5min', compress_after => INTERVAL '90 days');

-- Compress hourly aggregates after 1 year
ALTER MATERIALIZED VIEW agg_1hour SET (
    timescaledb.compress = true
);
SELECT add_compression_policy('agg_1hour', compress_after => INTERVAL '1 year');

-- Daily aggregates: compress after 2 years
ALTER MATERIALIZED VIEW agg_1day SET (
    timescaledb.compress = true
);
SELECT add_compression_policy('agg_1day', compress_after => INTERVAL '2 years');


-- ============================================================================
-- 10. MONITORING VIEWS — aggregate health
-- ============================================================================

-- ── 10a. Continuous aggregate refresh status ────────────────────────────
CREATE OR REPLACE VIEW cagg_refresh_status AS
SELECT
    view_name,
    completed_threshold   AS last_refreshed_to,
    now() - completed_threshold AS refresh_lag,
    CASE
        WHEN now() - completed_threshold > INTERVAL '10 minutes' THEN 'WARNING'
        WHEN now() - completed_threshold > INTERVAL '30 minutes' THEN 'CRITICAL'
        ELSE 'OK'
    END AS status
FROM timescaledb_information.continuous_aggregate_stats
ORDER BY view_name;

COMMENT ON VIEW cagg_refresh_status IS
    'Monitor continuous aggregate freshness — alert if refresh lag exceeds thresholds.';


-- ── 10b. Chunk compression status ───────────────────────────────────────
CREATE OR REPLACE VIEW chunk_compression_status AS
SELECT
    hypertable_name,
    count(*) FILTER (WHERE is_compressed)     AS compressed_chunks,
    count(*) FILTER (WHERE NOT is_compressed) AS uncompressed_chunks,
    pg_size_pretty(
        sum(CASE WHEN is_compressed THEN after_compression_total_bytes ELSE 0 END)
    ) AS compressed_size,
    pg_size_pretty(
        sum(CASE WHEN NOT is_compressed THEN total_bytes ELSE 0 END)
    ) AS uncompressed_size,
    CASE
        WHEN sum(before_compression_total_bytes) > 0
        THEN round(
            (1 - sum(after_compression_total_bytes)::numeric
               / sum(before_compression_total_bytes)::numeric) * 100, 1
        )
        ELSE 0
    END AS compression_ratio_pct
FROM timescaledb_information.chunk_compression_stats
GROUP BY hypertable_name
ORDER BY hypertable_name;

COMMENT ON VIEW chunk_compression_status IS
    'Chunk compression overview — track storage savings across all hypertables.';


-- ── 10c. Data volume estimate per block ─────────────────────────────────
CREATE OR REPLACE VIEW data_volume_per_block AS
SELECT
    st.slug AS site,
    b.slug  AS block,
    count(DISTINCT s.id) AS sensor_count,
    -- Estimated daily raw rows: sensors × readings/sec × 86400 sec
    count(DISTINCT s.id) * (1000.0 / COALESCE(AVG(s.poll_rate_ms), 5000)) * 86400 AS est_daily_rows,
    -- Estimated daily storage (uncompressed): ~40 bytes per row
    pg_size_pretty(
        (count(DISTINCT s.id) * (1000.0 / COALESCE(AVG(s.poll_rate_ms), 5000)) * 86400 * 40)::bigint
    ) AS est_daily_storage
FROM sensors s
JOIN equipment e ON e.id = s.equipment_id
JOIN blocks b   ON b.id = e.block_id
JOIN sites st   ON st.id = b.site_id
WHERE st.status = 'active'
GROUP BY st.slug, b.slug
ORDER BY st.slug, b.slug;

COMMENT ON VIEW data_volume_per_block IS
    'Estimated data volume per block — capacity planning for storage and ingestion.';


-- ============================================================================
-- DONE — Task 3 complete
-- ============================================================================
-- Summary:
--   4 continuous aggregate tiers (1min, 5min, 1hour, 1day)
--   5 retention policies (raw 90d, 1min/5min 2yr, 1hour/1day 7yr)
--   5 compression policies (raw 7d, 1min 30d, 5min 90d, 1hour 1yr, 1day 2yr)
--   3 billing views (kwh_5min, kwht_5min, energy_daily_summary)
--   2 helper functions (select_aggregate_tier, query_telemetry)
--   3 monitoring views (cagg_refresh_status, chunk_compression, data_volume)
-- ============================================================================
