# MicroLink MCS — MQTT Topic Schema v1.0.0

> **This is the integration contract between Stream A (Edge) and Stream B (Data Platform).**
> Stream B builds their ingestion pipeline against this document. Any changes require both streams to agree.

---

## 1. Topic Hierarchy

All MQTT topics follow a single consistent structure:

```
microlink/{site_id}/{block_id}/{subsystem}/{tag}
```

| Segment | Format | Example | Description |
|---------|--------|---------|-------------|
| `site_id` | lowercase kebab-case | `ab-baldwinsville` | Immutable site identifier |
| `block_id` | `block-NN` (zero-padded) | `block-01` | Block within a site |
| `subsystem` | lowercase kebab-case | `thermal-l2` | Engineering domain |
| `tag` | MicroLink tag convention | `TT-L2s` | Sensor/point tag |

### Site ID Rules
- Lowercase letters, digits, hyphens only
- 4-50 characters
- Assigned at site commissioning, never changes
- Examples: `ab-baldwinsville`, `rfg-foods-jhb`, `jack-black-cpt`

### Block ID Rules
- Always `block-NN` format (block-01 through block-99)
- Sequential per site

---

## 2. Subsystems

Each subsystem maps to an engineering domain. This is how Stream B partitions data.

| Subsystem | Description | Typical Poll Rate |
|-----------|-------------|-------------------|
| `electrical` | Power meters, UPS, PDUs, per-rack metering | 5s |
| `thermal-l1` | Loop 1 — IT secondary (CDUs, rack coolant) | 2s |
| `thermal-l2` | Loop 2 — primary warm-water loop (pumps, expansion) | 2s |
| `thermal-l3` | Loop 3 — host process side (HX secondary, host interface) | 2s |
| `thermal-hx` | Plate heat exchanger (approach ΔT, fouling, billing meter) | 2s |
| `thermal-reject` | Dry cooler, ambient, adiabatic spray | 2s |
| `thermal-safety` | Leak detectors, freeze protection, PRV, glycol spill | 1s |
| `environmental` | Rack inlet/outlet temps, humidity, differential pressure | 5s |
| `network` | Switch ports, latency, bandwidth, packet loss | 30s |
| `security` | Door contacts, PIR, cameras | 30s |
| `mode` | Thermal mode state (EXPORT/MIXED/REJECT/MAINT) | Event-driven |
| `edge` | Edge controller health (heartbeat) | 30s |
| `host-bms` | Readings from host's BACnet BMS (read-only) | 30s |

---

## 3. Message Schemas

### 3.1 Telemetry Message (95% of all traffic)

**Topic pattern:** `microlink/{site}/{block}/{subsystem}/{tag}`
**QoS:** 0 (at most once — volume over guaranteed delivery)
**Retain:** true (new subscribers get last known value)

```json
{
  "ts": "2026-02-21T14:30:00.123Z",
  "v": 45.2,
  "u": "°C",
  "q": "GOOD",
  "alarm": null,
  "seq": 184320
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string (ISO 8601) | ✅ | UTC timestamp with milliseconds. Set by edge at time of read. |
| `v` | number | ✅ | Sensor value. Boolean sensors use 0/1. |
| `u` | string | ✅ | Engineering unit (see Unit Registry below). |
| `q` | string enum | ✅ | `GOOD` / `UNCERTAIN` / `BAD` |
| `alarm` | string or null | optional | `null` (no alarm) or `P0`/`P1`/`P2`/`P3`. |
| `seq` | integer | optional | Monotonic counter per sensor. Enables gap detection. |

**Quality rules:**
- `GOOD` — successful read, value in expected range
- `UNCERTAIN` — successful read but value outside expected range, or data is >2× poll interval old (stale)
- `BAD` — read failed, sensor fault, or communication loss. `v` set to 0, the *previous* good value, or NaN

**Examples by subsystem:**

```
Topic: microlink/ab-baldwinsville/block-01/electrical/MET-01-kW
{"ts":"2026-02-21T14:30:00.123Z","v":1042.5,"u":"kW","q":"GOOD","alarm":null,"seq":9001}

Topic: microlink/ab-baldwinsville/block-01/thermal-l2/TT-L2s
{"ts":"2026-02-21T14:30:00.456Z","v":45.2,"u":"°C","q":"GOOD","alarm":null,"seq":18200}

Topic: microlink/ab-baldwinsville/block-01/thermal-l2/FT-L2
{"ts":"2026-02-21T14:30:00.456Z","v":12.8,"u":"m³/h","q":"GOOD","alarm":null,"seq":18201}

Topic: microlink/ab-baldwinsville/block-01/thermal-safety/LD-01a
{"ts":"2026-02-21T14:30:01.001Z","v":0,"u":"bool","q":"GOOD","alarm":null,"seq":92100}

Topic: microlink/ab-baldwinsville/block-01/thermal-l2/TT-L2s  (sensor failure)
{"ts":"2026-02-21T14:30:05.456Z","v":0,"u":"°C","q":"BAD","alarm":"P1","seq":18201}

Topic: microlink/ab-baldwinsville/block-01/environmental/TT-R01-in
{"ts":"2026-02-21T14:30:05.789Z","v":24.3,"u":"°C","q":"GOOD","alarm":null,"seq":5500}

Topic: microlink/ab-baldwinsville/block-01/network/SW-01-port1-bw
{"ts":"2026-02-21T14:30:30.000Z","v":847.2,"u":"Mbps","q":"GOOD","alarm":null}

Topic: microlink/ab-baldwinsville/block-01/host-bms/TT-HOST-hw-return
{"ts":"2026-02-21T14:30:30.100Z","v":38.5,"u":"°C","q":"GOOD","alarm":null}
```

---

### 3.2 Mode State Message

**Topic:** `microlink/{site}/{block}/mode/state`
**QoS:** 1 (at least once — MUST NOT be lost)
**Retain:** true (current state available to new subscribers)

Published **only on state transitions** — not periodic.

```json
{
  "ts": "2026-02-21T14:30:00.000Z",
  "state": "EXPORT",
  "previous": "REJECT",
  "trigger": "auto:temp_stabilised",
  "valves": {
    "v_exp": "OPEN",
    "v_rej": "CLOSED"
  },
  "guards": {
    "loop2_supply_temp": 45.2,
    "host_demand_active": true,
    "all_safety_good": true,
    "loop2_pressure_ok": true
  },
  "operator": null
}
```

**Valid state transitions:**

| From | To | Typical Trigger |
|------|----|-----------------|
| STARTUP | REJECT | `startup:initial` (always boot to REJECT) |
| REJECT | EXPORT | `auto:temp_stabilised` (guards met, stable for 5 min) |
| REJECT | MIXED | `auto:partial_demand` |
| EXPORT | REJECT | `auto:safety_limit` or `auto:host_demand_off` |
| EXPORT | MIXED | `auto:partial_reject_needed` |
| MIXED | EXPORT | `auto:full_demand_restored` |
| MIXED | REJECT | `auto:host_demand_off` or `auto:safety_limit` |
| ANY | REJECT | `auto:sensor_fault`, `plc:watchdog_timeout` (emergency) |
| ANY | MAINTENANCE | `operator:override` (requires operator field) |
| MAINTENANCE | REJECT | `operator:override` (return from maintenance always via REJECT) |

**Trigger naming convention:**
- `auto:*` — Safety PLC automatic decision
- `operator:*` — Human-initiated (operator field is set)
- `plc:*` — PLC internal event (watchdog, hardware fault)
- `startup:*` — System initialisation

**Examples:**

Normal startup:
```json
{"ts":"2026-02-21T08:00:00.000Z","state":"REJECT","previous":"STARTUP","trigger":"startup:initial","valves":{"v_exp":"CLOSED","v_rej":"OPEN"},"guards":{"loop2_supply_temp":22.1,"host_demand_active":false,"all_safety_good":true,"loop2_pressure_ok":true},"operator":null}
```

Transition to EXPORT after warmup:
```json
{"ts":"2026-02-21T08:12:00.000Z","state":"EXPORT","previous":"REJECT","trigger":"auto:temp_stabilised","valves":{"v_exp":"OPEN","v_rej":"CLOSED"},"guards":{"loop2_supply_temp":44.8,"host_demand_active":true,"all_safety_good":true,"loop2_pressure_ok":true},"operator":null}
```

Emergency REJECT on leak detection:
```json
{"ts":"2026-02-21T15:22:33.456Z","state":"REJECT","previous":"EXPORT","trigger":"auto:safety_limit","valves":{"v_exp":"CLOSED","v_rej":"OPEN"},"guards":{"loop2_supply_temp":45.1,"host_demand_active":true,"all_safety_good":false,"loop2_pressure_ok":true},"operator":null}
```

Operator override to MAINTENANCE:
```json
{"ts":"2026-02-21T22:00:00.000Z","state":"MAINTENANCE","previous":"REJECT","trigger":"operator:override","valves":{"v_exp":"CLOSED","v_rej":"CLOSED"},"guards":{"loop2_supply_temp":32.0,"host_demand_active":false,"all_safety_good":true,"loop2_pressure_ok":true},"operator":"nick.searra"}
```

---

### 3.3 Alarm Event Message

**Topic:** `microlink/{site}/{block}/alarms/{priority}` (e.g., `.../alarms/P0`)
**QoS:** 1 (MUST NOT be lost)
**Retain:** false (alarms are events, not persistent state)

Published when the **edge adapter** detects an alarm condition (value crosses threshold from the point schedule).

```json
{
  "ts": "2026-02-21T15:22:33.456Z",
  "alarm_id": "block-01-TT-L2s-1708523400123",
  "action": "RAISED",
  "priority": "P1",
  "sensor_tag": "TT-L2s",
  "subsystem": "thermal-l2",
  "value": 55.2,
  "threshold": 55.0,
  "direction": "HIGH",
  "description": "Loop 2 supply temperature HIGH — 55.2°C exceeds P1 limit 55.0°C"
}
```

**Action values:**
- `RAISED` — threshold crossed, new alarm instance
- `CLEARED` — value returned to normal (below threshold minus deadband)
- `ESCALATED` — alarm upgraded from lower to higher priority (e.g., P2 → P1 if value keeps rising)

**Alarm ID format:** `{block_id}-{sensor_tag}-{unix_ms}` — globally unique, used as dedup key across the system.

**Examples:**

Leak detected (P0 — immediate):
```json
{
  "ts": "2026-02-21T15:22:33.456Z",
  "alarm_id": "block-01-LD-01a-1708524153456",
  "action": "RAISED",
  "priority": "P0",
  "sensor_tag": "LD-01a",
  "subsystem": "thermal-safety",
  "value": 1,
  "threshold": 1,
  "direction": "BOOL",
  "description": "LEAK DETECTED — TPM zone rope sensor LD-01a triggered"
}
```

Pump vibration high (P2):
```json
{
  "ts": "2026-02-21T10:15:00.000Z",
  "alarm_id": "block-01-VIB-01-1708510500000",
  "action": "RAISED",
  "priority": "P2",
  "sensor_tag": "VIB-01",
  "subsystem": "thermal-l2",
  "value": 7.2,
  "threshold": 7.0,
  "direction": "HIGH",
  "description": "Pump PP-01 vibration HIGH — 7.2 mm/s exceeds P2 limit 7.0 mm/s"
}
```

Alarm cleared:
```json
{
  "ts": "2026-02-21T10:45:00.000Z",
  "alarm_id": "block-01-VIB-01-1708510500000",
  "action": "CLEARED",
  "priority": "P2",
  "sensor_tag": "VIB-01",
  "subsystem": "thermal-l2",
  "value": 4.8,
  "threshold": 7.0,
  "direction": "HIGH",
  "description": "Pump PP-01 vibration returned to normal — 4.8 mm/s"
}
```

---

### 3.4 Heartbeat Message

**Topic:** `microlink/{site}/{block}/edge/heartbeat`
**QoS:** 1
**Retain:** true
**Interval:** Every 30 seconds

```json
{
  "ts": "2026-02-21T14:30:00.000Z",
  "edge_id": "edge-ab-bville-01",
  "uptime_s": 1209600,
  "adapters": {
    "modbus": {
      "status": "running",
      "last_read_ts": "2026-02-21T14:29:58.000Z",
      "reads_total": 184320,
      "errors_total": 12,
      "devices_online": 3,
      "devices_total": 3
    },
    "snmp": {
      "status": "running",
      "last_read_ts": "2026-02-21T14:29:55.000Z",
      "reads_total": 42100,
      "errors_total": 0,
      "devices_online": 2,
      "devices_total": 2
    },
    "bacnet": {
      "status": "running",
      "last_read_ts": "2026-02-21T14:29:50.000Z",
      "reads_total": 8400,
      "errors_total": 3,
      "devices_online": 1,
      "devices_total": 1
    }
  },
  "buffer": {
    "depth": 0,
    "capacity": 5000000,
    "oldest_ts": null,
    "cloud_connected": true,
    "replay_active": false
  },
  "system": {
    "cpu_pct": 22.5,
    "mem_pct": 41.2,
    "disk_pct": 18.7,
    "temp_c": 52.3
  }
}
```

**Stream B monitoring rules:**
- If no heartbeat for >90 seconds → raise edge-offline alarm
- If `buffer.cloud_connected` = false → edge is buffering locally, expect data gaps
- If `buffer.replay_active` = true → buffered data is being replayed, expect out-of-order timestamps
- If any adapter has `status: "error"` → corresponding subsystem data may be BAD quality

---

### 3.5 Command Message (Cloud → Edge)

**Topic:** `microlink/{site}/{block}/command/{target}`
**QoS:** 1
**Retain:** false

Commands sent from the cloud to the edge controller. The edge subscribes to its own command topics.

```json
{
  "ts": "2026-02-21T22:00:00.000Z",
  "cmd": "mode_override",
  "params": {
    "target_mode": "MAINTENANCE",
    "reason": "Planned HX maintenance — 4 hour window"
  },
  "source": "operator:nick.searra",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Command response** published to `microlink/{site}/{block}/command/response`:
```json
{
  "ts": "2026-02-21T22:00:00.150Z",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "accepted",
  "reason": "Mode override to MAINTENANCE accepted. PLC transition initiated."
}
```

---

## 4. Polling Groups

Sensors are assigned to polling groups based on how fast their values change and how critical they are.

| Group | Interval | Use Case | Subsystems |
|-------|----------|----------|------------|
| `safety` | 1 second | Safety-critical — leak, thermal limit, PRV | `thermal-safety` |
| `fast` | 2 seconds | Process values — temps, pressures, flow, valves | `thermal-l1`, `thermal-l2`, `thermal-l3`, `thermal-hx`, `thermal-reject` |
| `normal` | 5 seconds | Electrical, environmental | `electrical`, `environmental` |
| `slow` | 30 seconds | Network, security, BMS, chemistry | `network`, `security`, `host-bms` |

---

## 5. Unit Registry

All adapters MUST normalise readings to these canonical unit strings. No variations allowed.

| Measurement | Unit String | Notes |
|-------------|-------------|-------|
| Temperature | `°C` | Always Celsius |
| Pressure | `kPa` | Convert from bar/psi at adapter |
| Flow | `m³/h` | Volumetric. Convert from l/min at adapter |
| Active power | `kW` | |
| Apparent power | `kVA` | |
| Reactive power | `kVAr` | |
| Energy (electrical) | `kWh` | Cumulative counter |
| Energy (thermal) | `kWht` | Cumulative counter — distinct from electrical |
| Voltage | `V` | Line-to-line unless tagged otherwise |
| Current | `A` | |
| Frequency | `Hz` | |
| Power factor | `pf` | 0.0 to 1.0 |
| Humidity | `%RH` | Relative humidity |
| Differential pressure | `Pa` | Pascals (not kPa) for small values |
| Vibration | `mm/s` | RMS velocity |
| Conductivity | `μS/cm` | Water quality |
| pH | `pH` | |
| Level | `%` | Percentage of range |
| Speed | `rpm` | Fan/pump speed |
| Boolean | `bool` | 0=false/off/closed, 1=true/on/open |
| Valve position | `%open` | 0=fully closed, 100=fully open |

---

## 6. Topic Sizing (Baldwinsville 1MW block)

Estimated message volume for capacity planning:

| Subsystem | Sensors | Poll Rate | Messages/min |
|-----------|---------|-----------|-------------|
| thermal-safety | ~20 | 1s | 1,200 |
| thermal-l1 | ~40 | 2s | 1,200 |
| thermal-l2 | ~25 | 2s | 750 |
| thermal-l3 | ~15 | 2s | 450 |
| thermal-hx | ~12 | 2s | 360 |
| thermal-reject | ~15 | 2s | 450 |
| electrical | ~80 | 5s | 960 |
| environmental | ~40 | 5s | 480 |
| network | ~25 | 30s | 50 |
| security | ~20 | 30s | 40 |
| host-bms | ~10 | 30s | 20 |
| **TOTAL** | **~300** | | **~5,960/min** |

Plus: 2 heartbeats/min, ~5-10 alarm events/day, ~4-8 mode changes/day

**Average message size:** ~120 bytes JSON → **~700 KB/min → ~1 GB/day per block**

At 10 blocks (10MW site): **~60,000 msg/min → ~10 GB/day**

---

## 7. Bridge Configuration

The edge runs a local Mosquitto broker. Messages are bridged to the cloud broker.

```
EDGE (local)                          CLOUD
┌─────────────┐     MQTT/TLS:8883    ┌─────────────┐
│ Mosquitto   │ ──────────────────▶  │ Cloud Broker│
│ port 1883   │                      │ (EMQX/HiveMQ)│
│             │ ◀────────────────── │             │
│ adapters    │     commands only    │ Stream B    │
│ publish here│                      │ subscribes  │
└─────────────┘                      └─────────────┘
```

**Bridge topics:**
- Edge → Cloud: `microlink/{site}/{block}/#` (all telemetry, alarms, heartbeats, mode)
- Cloud → Edge: `microlink/{site}/{block}/command/#` (commands only)

**Bridge settings:**
- TLS 1.3 with mutual certificate authentication
- Clean session: false (persistent session for QoS 1 delivery guarantee)
- Keepalive: 60 seconds
- Max inflight: 100 messages
- Retry interval: 5 seconds for unacknowledged QoS 1

If bridge disconnects, the edge store-and-forward buffer captures all messages locally (SQLite ring buffer, 72-hour capacity) and replays them when connection restores. Replayed messages have their original `ts` — Stream B MUST handle out-of-order timestamps.

---

## 8. Versioning

This is schema version `1.0.0`. The version is embedded in the JSON schema `$id` field.

Breaking changes (new required fields, changed topic structure) → major version bump.
Additive changes (new optional fields, new subsystems) → minor version bump.

Edge and cloud must agree on major version. Minor version mismatches are tolerated (new fields ignored by older consumers).

---

*This document is the authoritative source for MCS MQTT message formats. All protocol adapters (Modbus, SNMP, BACnet) normalise their readings to conform to these schemas before publishing.*
