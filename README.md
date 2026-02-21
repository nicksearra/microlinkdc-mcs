# MCS — MicroLink Control System

The software platform that monitors and controls MicroLink's 1MW liquid-cooled compute blocks deployed inside industrial hosts (breweries, food manufacturers, wastewater plants). Captures waste heat for the host's hot water while selling colocation, edge, and GPU services.

## Architecture

```
┌─────────────────┐     MQTT      ┌─────────────────┐     REST/WS     ┌──────────────┐
│   Edge / OT     │ ──────────▸   │    Platform      │ ──────────▸    │  Dashboard   │
│   (Stream A)    │               │    (Stream B)    │                │  (Stream D)  │
│                 │               │                  │                │              │
│ Modbus adapters │               │ Ingestion svc    │                │ NOC overview │
│ BACnet adapters │               │ Alarm engine     │     SQL        │ Alarm mgmt   │
│ SNMP adapters   │               │ REST API (21ep)  │ ◂──────────    │ Energy/host  │
│ Mode FSM        │               │ WebSocket feeds  │                │ Fleet nav    │
└─────────────────┘               │ TimescaleDB      │                └──────────────┘
  Runs on edge HW                 │ Redis pub/sub    │
  at each site                    └────────┬─────────┘
                                           │
                                    ┌──────┴───────┐
                                    │   Business   │
                                    │  (Stream C)  │
                                    │              │
                                    │ kWh billing  │
                                    │ kWht credits │
                                    │ SLA engine   │
                                    │ ESG reports  │
                                    │ Lender rpts  │
                                    └──────────────┘
```

## Quick Start

```bash
# Clone
git clone https://github.com/nicksearra/microlinkdc-mcs.git
cd microlinkdc-mcs

# Copy env config
cp .env.example .env

# Boot (infrastructure + all services)
chmod +x start.sh
./start.sh

# Or with simulator + dashboard
./start.sh full
```

After startup:
- **API:** http://localhost:8000
- **Swagger UI:** http://localhost:8000/docs
- **Dashboard:** http://localhost:3000 (with `./start.sh full`)

## Project Structure

```
├── db/                    Schema, aggregates, seed data
│   ├── schema.sql             TimescaleDB DDL (10 tables, 14 enums, RLS)
│   ├── aggregates.sql         Continuous aggregates (1min → 1day rollups)
│   └── seed_data.py           Test data generator
│
├── platform/              Core services (Stream B)
│   ├── ingestion/             MQTT → TimescaleDB batch writer
│   ├── alarm_engine/          ISA-18.2 4-state alarm processor
│   ├── api/                   FastAPI REST + WebSocket (21 endpoints)
│   ├── simulator/             Generates realistic MQTT sensor traffic
│   ├── Dockerfile
│   └── requirements.txt
│
├── business/              Revenue & compliance (Stream C)
│   ├── kwh_calculator.py      5-min electrical energy metering
│   ├── kwht_calculator.py     Thermal energy & heat credits
│   ├── sla_engine.py          Uptime SLA with ISA-18.2 compliance
│   ├── invoice_generator.py   PDF invoice generation
│   ├── lender_reports.py      Monthly/quarterly debt covenant reports
│   └── esg_calculator.py      CO₂ avoided, gas displaced
│
├── edge/                  OT adapters (Stream A — runs on edge hardware)
│   ├── modbus_adapter.py      CDU, UPS, PDU polling
│   ├── bacnet_adapter.py      BMS, AHU integration
│   ├── snmp_adapter.py        Network switches, PDU monitoring
│   └── edge_orchestrator.py   Mode FSM, watchdog, adapter lifecycle
│
├── dashboard/             React frontend (Stream D)
│   └── src/
│       ├── pages/Dashboard.jsx   NOC view with thermal flow diagram
│       ├── pages/Fleet.jsx       Site → block navigation
│       ├── pages/Alarms.jsx      Alarm table with ISA-18.2 stats
│       ├── pages/Energy.jsx      Electrical, thermal, PUE, ESG
│       └── lib/api.js            REST client + WebSocket hooks
│
├── contracts/             Shared specifications
│   ├── openapi.yaml           API contract (19 paths, 27 schemas)
│   ├── mqtt-schema.json       MQTT topic/payload spec
│   └── point-schedule.csv     Full sensor registry
│
├── docs/                  Architecture documentation
├── docker-compose.yml     Single compose for entire stack
├── start.sh               One-command startup script
└── .env.example           Configuration template
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| TimescaleDB | 5432 | PostgreSQL + time-series hypertables |
| Redis | 6379 | Sensor cache, alarm pub/sub |
| Mosquitto | 1883 | MQTT broker (edge → platform) |
| API | 8000 | FastAPI with Swagger docs |
| Ingestor | — | MQTT subscriber → batch DB writer |
| Alarm Engine | — | Redis subscriber → ISA-18.2 state machine |
| Dashboard | 3000 | React SPA (dev profile) |
| Simulator | — | Fake MQTT traffic (dev profile) |

## Key Design Decisions

- **3-loop thermal model:** CDU→Rack (Loop 1) → Glycol primary (Loop 2) → Host PHX (Loop 3). Each loop independently monitored.
- **ISA-18.2 alarm management:** 4-state model (ACTIVE → ACKED → RTN_UNACK → CLEARED) with shelving, suppression, and cascade rules.
- **Automatic telemetry tiering:** API auto-selects resolution based on query time span (<2h=raw, 2-12h=1min, 12h-3d=5min, 3d-90d=1hour, >90d=1day).
- **Host is partner, not customer:** Budget-neutral heat delivery, no host dependency for IT uptime.
- **Waste heat = revenue:** kWh thermal metering drives heat credits to host and gas displacement ESG metrics.

## Development

```bash
# Run just infrastructure for local development
docker compose up -d timescaledb redis mosquitto

# Run API locally (outside Docker)
cd platform
pip install -r requirements.txt
uvicorn api.app:create_app --factory --reload --port 8000

# Run dashboard locally
cd dashboard
npm install
npm run dev
```

## License

Proprietary — MicroLink Data Centers
