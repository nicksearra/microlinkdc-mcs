---
title: "MCS Stream B — Continuous Aggregate Pyramid"
---

# Aggregate Architecture

## Data Flow Pyramid

```mermaid
graph TD
    subgraph "Raw Ingestion"
        MQTT["MQTT Broker<br/>~5,000 msg/sec per block"] --> ING["Ingestion Service<br/>(Task 2)"]
        ING --> RAW["telemetry (hypertable)<br/>5-sec resolution<br/>~432M rows/day/block<br/><b>Retention: 90 days</b><br/>Compress after: 7 days"]
    end

    subgraph "Aggregate Pyramid"
        RAW -->|"refresh: 1 min<br/>lag: 2 min"| A1["agg_1min<br/>1-minute buckets<br/><b>Retention: 2 years</b><br/>Compress after: 30 days"]
        A1 -->|"refresh: 5 min<br/>lag: 10 min"| A5["agg_5min<br/>5-minute buckets<br/><b>Retention: 2 years</b><br/>Compress after: 90 days"]
        A5 -->|"refresh: 1 hour<br/>lag: 1 hour"| AH["agg_1hour<br/>1-hour buckets<br/><b>Retention: 7 years</b><br/>Compress after: 1 year"]
        AH -->|"refresh: 1 day<br/>lag: 1 day"| AD["agg_1day<br/>1-day buckets<br/><b>Retention: 7 years</b><br/>Compress after: 2 years"]
    end

    subgraph "Consumers"
        RAW -.->|"< 2h range"| API["REST API<br/>(Task 5)"]
        A1 -.->|"2h – 12h range"| API
        A5 -.->|"12h – 3d range"| API
        AH -.->|"3d – 90d range"| API
        AD -.->|"> 90d range"| API
        A5 -.-> BILL["billing_kwh_5min<br/>billing_kwht_5min<br/>(Stream C)"]
        AD -.-> ESG["energy_daily_summary<br/>(Lender Reports, ESG)"]
        A1 -.-> NOC["NOC Dashboard<br/>(Stream D)"]
    end

    style RAW fill:#e74c3c,color:#fff
    style A1 fill:#e67e22,color:#fff
    style A5 fill:#f1c40f,color:#000
    style AH fill:#2ecc71,color:#fff
    style AD fill:#3498db,color:#fff
    style BILL fill:#9b59b6,color:#fff
    style ESG fill:#9b59b6,color:#fff
    style NOC fill:#9b59b6,color:#fff
```

## Query Routing Logic

```mermaid
flowchart LR
    REQ["API Request<br/>sensor_id + start + end"] --> ROUTER{"select_aggregate_tier()"}
    ROUTER -->|"span < 2h"| T["telemetry<br/>(raw)"]
    ROUTER -->|"2h ≤ span < 12h"| M1["agg_1min"]
    ROUTER -->|"12h ≤ span < 3d"| M5["agg_5min"]
    ROUTER -->|"3d ≤ span < 90d"| MH["agg_1hour"]
    ROUTER -->|"span ≥ 90d"| MD["agg_1day"]
```

## Storage Estimate (1MW Block, 300 sensors)

| Tier | Row Size | Daily Rows | Daily Storage | After Compression |
|------|----------|-----------|---------------|-------------------|
| Raw (5s) | ~40 bytes | ~432M | ~16 GB | ~1.6 GB (10:1) |
| 1-min | ~80 bytes | ~432K | ~33 MB | ~5 MB |
| 5-min | ~80 bytes | ~86K | ~7 MB | ~1 MB |
| 1-hour | ~80 bytes | ~7.2K | ~560 KB | ~90 KB |
| 1-day | ~80 bytes | ~300 | ~24 KB | ~4 KB |

**Total per block at retention limits:**
- Raw (90 days compressed): ~144 GB
- All aggregates (up to 7 years): ~25 GB
- **Total: ~170 GB per 1MW block**

**10MW site (10 blocks): ~1.7 TB** — well within a single TimescaleDB instance.

## Columns Stored Per Aggregate Row

| Column | Type | Purpose |
|--------|------|---------|
| `bucket` | timestamptz | Time bucket boundary |
| `sensor_id` | integer | FK to sensors table |
| `val_avg` | double precision | Average value in bucket |
| `val_min` | double precision | Minimum value in bucket |
| `val_max` | double precision | Maximum value in bucket |
| `val_last` | double precision | Last value (for "current" display) |
| `sample_count` | bigint | Number of raw readings |
| `val_stddev` | double precision | Standard deviation (anomaly detection) |
| `val_sum` | double precision | Sum (for energy counters) |
| `quality_good_ratio` | double precision | Fraction of GOOD quality samples |

## Billing Data Flow

```mermaid
flowchart LR
    A5["agg_5min"] --> BK["billing_kwh_5min<br/>(electrical energy)"]
    A5 --> BT["billing_kwht_5min<br/>(thermal energy)"]
    AD["agg_1day"] --> ED["energy_daily_summary<br/>(per-block totals)"]
    BK --> SC["Stream C<br/>Billing Calculator"]
    BT --> SC
    ED --> LR["Lender Reports"]
    ED --> ESG["ESG Module"]
```

## Design Decisions

1. **Hierarchical aggregates (not flat):** Each tier builds on the previous one, reducing computation. The 1-day aggregate never touches raw data — it reads from the 1-hour tier.

2. **`val_last` column:** Critical for dashboards showing "current value." Without it, you'd need to query raw data for the latest reading. `last(value, time)` is a TimescaleDB aggregate that efficiently tracks the most recent value.

3. **`val_sum` column:** Needed for energy counters (kWh, kWht) where the total over an interval matters, not just the average. Stream C's billing module relies on this.

4. **`quality_good_ratio`:** Propagated through all tiers so billing can flag intervals with degraded data quality. If quality drops below a threshold, Stream C can flag the invoice for manual review.

5. **Compression segmented by `sensor_id`:** TimescaleDB compresses by segment. Segmenting by sensor_id means queries for a single sensor decompress minimal data.

6. **2-minute refresh lag on 1-min aggregate:** Accounts for store-and-forward edge controllers that may deliver data with a delay after network reconnection.

7. **`query_telemetry()` function:** Single entry point for the REST API. Automatically selects the best tier based on the requested time range, or allows explicit tier override for billing precision.
