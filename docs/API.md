---
title: "MCS Stream B — Task 5: REST API & WebSocket"
---

# Data Platform API — Endpoint Reference

**Base URL:** `http://api.mcs.local/api/v1`
**Docs:** `http://api.mcs.local/docs` (Swagger UI) | `/redoc` (ReDoc)

This is the contract that Stream C (Business Systems) and Stream D (Frontend) build against.

## Quick Reference

| Method | Endpoint | Description | Consumer |
|--------|----------|-------------|----------|
| GET | `/sites` | List all sites | D: site selector |
| GET | `/sites/{slug}` | Site details | D: site overview |
| GET | `/blocks` | List blocks (filter by site) | D: block cards |
| GET | `/blocks/{slug}` | Block details | D: block detail view |
| GET | `/equipment/{block_slug}` | Equipment list | D: equipment tree |
| GET | `/sensors/{block_slug}` | Sensor registry | D: sensor picker |
| GET | `/telemetry` | Historical telemetry (auto-tier) | D: trend charts |
| GET | `/telemetry/latest` | Last value per sensor | D: NOC dashboard |
| GET | `/telemetry/multi` | Multi-sensor query | D: comparison charts |
| GET | `/alarms` | Active alarm list | D: alarm banner |
| POST | `/alarms/{sensor_id}/acknowledge` | Operator ack | D: alarm UI |
| POST | `/alarms/{sensor_id}/shelve` | Operator shelve | D: alarm UI |
| GET | `/alarms/stats` | ISA-18.2 metrics | D: alarm KPIs |
| GET | `/events` | Immutable event log | C: audit, D: timeline |
| GET | `/billing/kwh` | 5-min electrical energy | C: billing calc |
| GET | `/billing/kwht` | 5-min thermal energy | C: heat credits |
| GET | `/billing/energy-daily` | Daily energy summary | C: lender reports |
| GET | `/health` | Health check | Ops: monitoring |
| GET | `/stats` | System statistics | Ops: capacity |
| WS | `/ws/telemetry/{block_slug}` | Live sensor values | D: NOC dashboard |
| WS | `/ws/alarms` | Live alarm events | D: alarm banner |
| WS | `/ws/events/{block_slug}` | Live event stream | D: event timeline |

## Telemetry Query — Automatic Tier Selection

The `/telemetry` endpoint automatically selects the optimal aggregate tier:

| Time Range | Tier Used | Resolution | Use Case |
|-----------|-----------|------------|----------|
| < 2 hours | `telemetry` (raw) | 5 seconds | Live debugging |
| 2h – 12h | `agg_1min` | 1 minute | NOC shift view |
| 12h – 3 days | `agg_5min` | 5 minutes | Incident analysis |
| 3d – 90 days | `agg_1hour` | 1 hour | Monthly trends |
| > 90 days | `agg_1day` | 1 day | Quarterly reports |

Override with `?agg=5min` to force a specific tier.

### Example Request

```bash
# Last 6 hours of CDU supply temperature (auto-selects agg_1min)
curl "http://api.mcs.local/api/v1/telemetry?sensor_id=42&start=2026-02-21T06:00:00Z&end=2026-02-21T12:00:00Z"

# Force 5-minute resolution for billing precision
curl "http://api.mcs.local/api/v1/telemetry?sensor_id=42&start=2026-02-01T00:00:00Z&end=2026-03-01T00:00:00Z&agg=5min"
```

### Response

```json
{
    "sensor_id": 42,
    "sensor_tag": "CDU-01-T-SUP",
    "unit": "°C",
    "tier": "agg_1min",
    "start": "2026-02-21T06:00:00Z",
    "end": "2026-02-21T12:00:00Z",
    "point_count": 360,
    "data": [
        {
            "bucket": "2026-02-21T06:00:00Z",
            "val_avg": 32.14,
            "val_min": 31.8,
            "val_max": 32.6,
            "val_last": 32.2,
            "sample_count": 12,
            "quality_ratio": 1.0
        }
    ]
}
```

## Alarm Endpoints

### Acknowledge

```bash
curl -X POST "http://api.mcs.local/api/v1/alarms/42/acknowledge" \
  -H "Content-Type: application/json" \
  -d '{"operator": "nick.searra"}'
```

### Shelve (requires reason)

```bash
curl -X POST "http://api.mcs.local/api/v1/alarms/42/shelve" \
  -H "Content-Type: application/json" \
  -d '{
    "operator": "nick.searra",
    "reason": "Sensor calibration in progress — expected excursion",
    "duration_hours": 4
  }'
```

### Alarm Stats (ISA-18.2 compliance)

```json
{
    "standing": 3,
    "acked": 1,
    "shelved": 2,
    "suppressed": 5,
    "raised_last_hour": 4,
    "avg_response_seconds_24h": 127.3,
    "isa_18_2_target_per_hour": 6,
    "compliant": true
}
```

## Billing Endpoints (Stream C Contract)

### GET /billing/kwh

Returns 5-minute electrical energy data. Stream C multiplies `kwh_value` by the customer's rate to produce invoice line items.

### GET /billing/kwht

Returns 5-minute thermal energy data. Stream C uses this for heat credit calculations — the host's share of recovered waste heat.

### GET /billing/energy-daily

Daily summary with `heat_recovery_ratio` and `pue_estimate` — directly feeds lender reports and ESG calculations.

## WebSocket Feeds

### Telemetry Stream

```javascript
const ws = new WebSocket("ws://api.mcs.local/api/v1/ws/telemetry/block-01?subsystem=thermal-l1");
ws.onmessage = (event) => {
    const reading = JSON.parse(event.data);
    // { sensor_id, tag, subsystem, value, quality, timestamp }
};
```

### Alarm Stream

```javascript
const ws = new WebSocket("ws://api.mcs.local/api/v1/ws/alarms?min_priority=P1");
ws.onmessage = (event) => {
    const { event: type, alarm } = JSON.parse(event.data);
    // type: alarm_raised | alarm_acked | alarm_cleared | alarm_shelved
    // alarm: full AlarmResponse object
};
```

## Running the API

```bash
# Add to docker-compose.yml
api:
  build: .
  command: uvicorn api.app:app --host 0.0.0.0 --port 8000
  depends_on:
    - timescaledb
    - redis
  environment:
    DB_HOST: timescaledb
    REDIS_URL: redis://redis:6379/0
  ports:
    - "8000:8000"

# Standalone
uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
```

## File Structure

```
api/
├── __init__.py
├── app.py              # FastAPI factory + lifespan
├── deps.py             # Dependency injection (DB, Redis, auth, pagination)
├── schemas.py          # Pydantic response models (the contract)
└── routes/
    ├── sites.py        # GET /sites
    ├── blocks.py       # GET /blocks, /equipment, /sensors
    ├── telemetry.py    # GET /telemetry (auto-tier), /telemetry/latest, /telemetry/multi
    ├── alarms.py       # GET/POST /alarms
    ├── events.py       # GET /events
    ├── billing.py      # GET /billing/kwh, /kwht, /energy-daily
    ├── health.py       # GET /health, /stats
    └── websockets.py   # WS /ws/telemetry, /ws/alarms, /ws/events
```

## What's Next

- **Task 6:** OpenAPI spec export — auto-generated from FastAPI, published as the formal contract for Stream C and D
