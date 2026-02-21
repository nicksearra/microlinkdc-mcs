---
title: "MCS Stream D — Frontend & Dashboards"
---

# Stream D: Frontend Architecture

## Views

| View | File | Description | User |
|------|------|-------------|------|
| Fleet Overview | `fleet-overview.jsx` | Site → block navigation, fleet KPIs | NOC operators |
| NOC Dashboard | `dashboard.jsx` | Per-block live telemetry, subsystem panels, alarms | NOC operators |
| Alarm Management | `alarm-management.jsx` | Full alarm table, ISA-18.2 stats, ack/shelve actions | NOC operators |
| Energy Dashboard | `energy-dashboard.jsx` | Electrical, thermal, PUE, ESG, host value | Ops + hosts |

## Data Flow

```
Stream B API (REST)              Stream B WebSocket
  │                                │
  ├─ GET /sites, /blocks          ├─ WS /ws/telemetry/{block}
  ├─ GET /telemetry/latest        ├─ WS /ws/alarms
  ├─ GET /telemetry?sensor_id=    └─ WS /ws/events/{block}
  ├─ GET /alarms                        │
  ├─ POST /alarms/{id}/acknowledge      │
  ├─ POST /alarms/{id}/shelve           │
  ├─ GET /billing/energy-daily          │
  └─ GET /alarms/stats                  │
       │                                │
       └──────── api-client.js ─────────┘
                     │
                React Hooks
                     │
         ┌───────────┼───────────┐
         │           │           │
    Fleet     NOC Dashboard   Energy
   Overview   + Alarm Mgmt   Dashboard
```

## API Client (api-client.js)

### REST Methods
Every Stream B endpoint has a typed wrapper:
- `api.listSites()`, `api.getSite(slug)`
- `api.listBlocks(siteSlug?)`, `api.getBlock(slug)`
- `api.queryTelemetry(sensorId, start, end, agg?)`
- `api.getLatestValues(blockSlug, subsystem?)`
- `api.listAlarms({ state, priority, blockSlug })`
- `api.acknowledgeAlarm(sensorId, operator)`
- `api.shelveAlarm(sensorId, operator, reason, hours)`
- `api.alarmStats(blockSlug?)`
- `api.energyDailySummary(blockSlug, start, end)`

### WebSocket Manager
`WSConnection` class with:
- Automatic reconnect with exponential backoff (max 10 attempts)
- JSON parse on incoming messages
- Clean disconnect on component unmount

### React Hooks
| Hook | Purpose | Refresh |
|------|---------|---------|
| `useApiQuery(fn, deps)` | Generic fetch with loading/error | On dep change |
| `usePolling(fn, interval, deps)` | Periodic polling | Every N ms |
| `useLiveTelemetry(blockSlug)` | WS telemetry feed → `{sensorId: reading}` | Real-time |
| `useLiveAlarms(blockSlug?)` | WS alarm events → alarm list | Real-time |
| `useTelemetryHistory(id, start, end)` | Historical query with auto-tier | On params change |
| `useBlockLatest(blockSlug)` | Latest values (polls 5s) | 5 seconds |
| `useAlarmStats(blockSlug?)` | ISA-18.2 compliance (polls 30s) | 30 seconds |
| `useEnergyDaily(blockSlug, start, end)` | Billing energy data | On params change |

## Design System

**Theme:** Industrial dark — `#0a0e17` background, high contrast text. Operators need data density and instant status recognition in low-light NOC environments.

**Typography:**
- Display: DM Sans (800 weight for headings)
- Data: JetBrains Mono (monospace for values, tags, timestamps)
- Body: DM Sans (400/500 for labels and descriptions)

**Color Semantics:**
| Color | Hex | Meaning |
|-------|-----|---------|
| Green | `#10b981` | Normal, compliant, good |
| Yellow | `#f59e0b` | Warning, P2 alarm, approaching limit |
| Red | `#ef4444` | Critical, P0 alarm, fault |
| Orange | `#f97316` | P1 alarm, high priority |
| Cyan | `#06b6d4` | Thermal/heat, RTN_UNACK |
| Purple | `#8b5cf6` | Shelved, manual action |
| Blue | `#3b82f6` | Primary accent, electrical, interactive |

**Alarm Visual Priority:**
- P0: Red background glow + pulsing border
- P1: Orange left border + priority badge
- P2: Yellow badge, no background
- P3: Muted, dim presentation

## Technology Stack

- **React 18** with hooks (no class components)
- **Recharts** for time-series charts (AreaChart, BarChart, LineChart)
- **Tailwind utility classes** for layout
- **Inline styles** for component-scoped theming
- **No external state management** — React state + context sufficient at current scale
- **No router in artifacts** — production app would use React Router

## Production Notes

1. **Connect to real API:** Replace `MOCK_*` data with the React hooks from `api-client.js`
2. **Authentication:** Wire `api-client.js` to use JWT from auth flow (Stream B `auth.py`)
3. **Routing:** Add React Router — `/fleet` → `/site/:slug` → `/block/:slug/dashboard`
4. **State management:** Consider Zustand if cross-component state gets complex
5. **Notifications:** Browser Notification API for P0/P1 alarms via WS feed
6. **Offline:** Service worker for basic offline indicator + reconnect
