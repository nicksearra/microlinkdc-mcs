# MicroLink MCS — Commissioning Procedures

## Site Acceptance Testing & Automated Verification · v1.0.0

> **This document defines the complete commissioning sequence for a MicroLink
> 1MW compute block.** Every phase must be completed and signed off before
> progressing. Automated test scripts supplement manual verification.
> Reference site: AB InBev Baldwinsville.

---

## Commissioning Overview

```
Phase 1 ─── Pre-Power Checks (mechanical, electrical, network)
   │
Phase 2 ─── Network & Communication Verification
   │
Phase 3 ─── Sensor Calibration & Point Validation
   │
Phase 4 ─── Adapter Integration Testing
   │
Phase 5 ─── Safety System Verification
   │
Phase 6 ─── Mode State Machine Testing
   │
Phase 7 ─── Thermal Performance Validation
   │
Phase 8 ─── End-to-End Acceptance & Handover
```

**Duration estimate:** 5-7 days for a single 1MW block.
**Team required:** 1× commissioning engineer, 1× controls engineer, 1× host facility contact.
**Prerequisites:** All physical installation complete, electrical energisation approved, host BMS integration point provisioned.

---

## Phase 1: Pre-Power Checks

**Duration:** 4-6 hours
**Power status:** DE-ENERGISED — lockout/tagout active

### 1.1 Mechanical Inspection

| # | Check | Method | Accept Criteria | Result | Initials |
|---|-------|--------|----------------|--------|----------|
| 1.1.1 | Pipe connections tight, no visible leaks | Visual + hand torque check | All connections snug, no drips | ☐ Pass ☐ Fail | |
| 1.1.2 | Valve actuators mounted, travel verified | Manually exercise each valve | Full travel 0-100%, smooth operation | ☐ Pass ☐ Fail | |
| 1.1.3 | V-EXP spring-return to CLOSED | Remove actuator power, observe | Valve closes within 30 seconds | ☐ Pass ☐ Fail | |
| 1.1.4 | V-REJ spring-return to OPEN | Remove actuator power, observe | Valve opens within 30 seconds | ☐ Pass ☐ Fail | |
| 1.1.5 | Pump rotation direction verified | Bump-test each pump | Correct rotation (check arrow on casing) | ☐ Pass ☐ Fail | |
| 1.1.6 | Expansion vessel pre-charge pressure | Gauge on Schrader valve | Within ±10% of design cold charge | ☐ Pass ☐ Fail | |
| 1.1.7 | Glycol concentration verified | Refractometer sample from Loop 2 | 35% ±2% propylene glycol | ☐ Pass ☐ Fail | |
| 1.1.8 | Strainer/filter installed and clean | Visual inspection of Y-strainers | Mesh clean, no debris | ☐ Pass ☐ Fail | |
| 1.1.9 | Dry cooler fans free to rotate | Hand-spin each fan | No physical obstruction, bearings smooth | ☐ Pass ☐ Fail | |
| 1.1.10 | Leak detection ropes correctly positioned | Visual trace each rope sensor | Under all pipe runs, CDUs, HX bay, TPM | ☐ Pass ☐ Fail | |
| 1.1.11 | PRV installed, set pressure verified | Check nameplate | 500 kPa (Loop 2), discharge piped to safe drain | ☐ Pass ☐ Fail | |
| 1.1.12 | Backflow preventer installed and tagged | Visual + certification check | Current annual certification | ☐ Pass ☐ Fail | |
| 1.1.13 | Containment bund intact, drain clear | Visual + water test | Holds 110% of largest vessel volume | ☐ Pass ☐ Fail | |

### 1.2 Electrical Pre-Check

| # | Check | Method | Accept Criteria | Result | Initials |
|---|-------|--------|----------------|--------|----------|
| 1.2.1 | Transformer tap position set correctly | Visual inspection | Correct tap for site voltage (480V) | ☐ Pass ☐ Fail | |
| 1.2.2 | All circuit breakers in OFF position | Visual walk-down | Confirmed OFF / tripped | ☐ Pass ☐ Fail | |
| 1.2.3 | Earth/ground continuity verified | Megger test | <1 Ω to building ground grid | ☐ Pass ☐ Fail | |
| 1.2.4 | Insulation resistance — all power cables | Megger 1000V DC | >1 MΩ per cable | ☐ Pass ☐ Fail | |
| 1.2.5 | UPS batteries installed and connected | Visual + voltage check | String voltage within ±5% of rated | ☐ Pass ☐ Fail | |
| 1.2.6 | E-stop buttons located and labelled | Visual walk-down | Red mushroom-head, clearly labelled, accessible | ☐ Pass ☐ Fail | |

### 1.3 Network Physical Check

| # | Check | Method | Accept Criteria | Result | Initials |
|---|-------|--------|----------------|--------|----------|
| 1.3.1 | All network cables labelled (both ends) | Visual walk-down | Tag matches cable schedule, correct colour per zone | ☐ Pass ☐ Fail | |
| 1.3.2 | Zone 1 (red) cables in dedicated conduit | Visual trace | No shared conduit with other zones | ☐ Pass ☐ Fail | |
| 1.3.3 | Cable certification test (Cat6A) | Fluke DSX tester | All cables pass Cat6A channel test | ☐ Pass ☐ Fail | |
| 1.3.4 | Fibre WAN link tested | OTDR / light meter | Loss within budget, no macro-bends | ☐ Pass ☐ Fail | |
| 1.3.5 | Switch mounted, powered (not configured yet) | Visual | LED activity on management port | ☐ Pass ☐ Fail | |
| 1.3.6 | Edge controller mounted, powered | Visual | Boot LED sequence normal | ☐ Pass ☐ Fail | |

**Phase 1 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Electrical Contractor | | | |

---

## Phase 2: Network & Communication Verification

**Duration:** 4-6 hours
**Power status:** Network equipment energised. OT devices powered for comms test.

### 2.1 VLAN Configuration

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 2.1.1 | VLAN 10 (Safety) created and assigned | Switch CLI `show vlan` | VLAN 10 exists, correct ports assigned | ☐ Pass ☐ Fail |
| 2.1.2 | VLAN 20 (OT) created and assigned | Switch CLI | VLAN 20 exists, correct ports | ☐ Pass ☐ Fail |
| 2.1.3 | VLAN 25 (Host BMS) created and assigned | Switch CLI | VLAN 25 exists, eth1 port only | ☐ Pass ☐ Fail |
| 2.1.4 | VLAN 30 (IT/DC) created and assigned | Switch CLI | VLAN 30 exists, correct ports | ☐ Pass ☐ Fail |
| 2.1.5 | VLAN 40 (Edge DMZ) created and assigned | Switch CLI | VLAN 40 exists, trunk to edge | ☐ Pass ☐ Fail |
| 2.1.6 | Inter-VLAN routing ACLs configured | Switch CLI `show access-list` | Rules match OT Network Architecture doc | ☐ Pass ☐ Fail |

### 2.2 Connectivity Tests

Run automated test script: `test_network.py`

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 2.2.1 | Edge → Safety PLC ping | `ping 192.168.1.1` | <5ms, 0% loss (100 packets) | ☐ Pass ☐ Fail |
| 2.2.2 | Edge → OT devices ping (all) | `ping 192.168.10.*` | All configured IPs respond | ☐ Pass ☐ Fail |
| 2.2.3 | Edge → Host BMS ping | `ping 192.168.20.1` | <10ms, 0% loss | ☐ Pass ☐ Fail |
| 2.2.4 | Edge → IT devices ping | `ping 10.10.30.*` | All configured IPs respond | ☐ Pass ☐ Fail |
| 2.2.5 | Edge → Cloud broker | `openssl s_client mqtt.microlink.energy:8883` | TLS handshake succeeds, cert valid | ☐ Pass ☐ Fail |
| 2.2.6 | Zone 1 → Zone 2 blocked | From Safety HMI, `ping 192.168.10.10` | 100% loss (ACL blocking) | ☐ Pass ☐ Fail |
| 2.2.7 | Zone 2 → Zone 1 blocked | From OT device, `ping 192.168.1.1` | 100% loss | ☐ Pass ☐ Fail |
| 2.2.8 | Zone 3 → Zone 1 blocked | From IT switch, `ping 192.168.1.1` | 100% loss | ☐ Pass ☐ Fail |
| 2.2.9 | Zone 3 → Zone 2 blocked | From IT switch, `ping 192.168.10.10` | 100% loss | ☐ Pass ☐ Fail |
| 2.2.10 | OT device → Internet blocked | From 192.168.10.10, `ping 8.8.8.8` | 100% loss | ☐ Pass ☐ Fail |

### 2.3 Protocol Tests

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 2.3.1 | Modbus TCP to power meter | `modbus_test_read 192.168.10.10:502 40001 2` | Returns valid FLOAT32 | ☐ Pass ☐ Fail |
| 2.3.2 | Modbus TCP to thermal module | `modbus_test_read 192.168.10.20:502 42001 2` | Returns valid FLOAT32 | ☐ Pass ☐ Fail |
| 2.3.3 | Modbus RTU via gateway | `modbus_test_read 192.168.10.99:502 45001 1 slave=3` | Returns 0 or 1 | ☐ Pass ☐ Fail |
| 2.3.4 | SNMP GET to UPS | `snmpget -v2c -c microlink-ro 10.10.30.30 sysUpTime` | Returns timeticks | ☐ Pass ☐ Fail |
| 2.3.5 | SNMP GET to PDU | `snmpget` on power OID | Returns watt value | ☐ Pass ☐ Fail |
| 2.3.6 | SNMPv3 to core switch | `snmpget -v3 -u microlink_monitor` | Auth succeeds, returns value | ☐ Pass ☐ Fail |
| 2.3.7 | BACnet Who-Is discovery | `bacnet_adapter.py --discover` | Host BMS device found, objects enumerated | ☐ Pass ☐ Fail |
| 2.3.8 | BACnet Read present-value | Read AI:1:1 from host BMS | Returns temperature value | ☐ Pass ☐ Fail |

**Phase 2 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Network Engineer | | | |

---

## Phase 3: Sensor Calibration & Point Validation

**Duration:** 6-8 hours
**Power status:** Full power. Hydraulic system pressurised (cold, no IT load).

### 3.1 Temperature Sensor Validation

For each temperature sensor: compare reading against calibrated reference thermometer.

| # | Sensor Tag | Location | Reference °C | Sensor °C | Error °C | Accept (±0.5°C) | Result |
|---|-----------|----------|-------------|----------|---------|-----------------|--------|
| 3.1.1 | TT-L2s | Loop 2 supply header | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.2 | TT-L2r | Loop 2 return header | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.3 | TT-CDU1s | CDU-01 supply | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.4 | TT-CDU1r | CDU-01 return | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.5 | TT-HXpIn | HX primary inlet | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.6 | TT-HXsOut | HX secondary outlet | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.7 | TT-BIL-s | Billing supply (matched pair) | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.8 | TT-BIL-r | Billing return (matched pair) | | | | ☐ | ☐ Pass ☐ Fail |
| 3.1.9 | TT-AMB | Ambient outdoor | | | | ☐ | ☐ Pass ☐ Fail |

**Billing meter matched pair:** TT-BIL-s and TT-BIL-r must read within 0.1°C of each other when immersed in the same water sample (zero-point check). Record: difference = ____°C (accept ≤0.1°C).

### 3.2 Pressure & Flow Validation

| # | Sensor Tag | Method | Reference | Sensor | Accept | Result |
|---|-----------|--------|-----------|--------|--------|--------|
| 3.2.1 | PT-L2s | Calibrated test gauge at tee | | | ±5 kPa | ☐ Pass ☐ Fail |
| 3.2.2 | PT-L2r | Calibrated test gauge | | | ±5 kPa | ☐ Pass ☐ Fail |
| 3.2.3 | FT-L2 | Timed fill of known volume | | | ±5% | ☐ Pass ☐ Fail |
| 3.2.4 | FT-BIL | Compare with FT-L2 at same flow | | | ±2% | ☐ Pass ☐ Fail |
| 3.2.5 | PT-EXP | Calibrated gauge on vessel | | | ±5 kPa | ☐ Pass ☐ Fail |

### 3.3 Electrical Meter Validation

| # | Meter Tag | Method | Reference | Sensor | Accept | Result |
|---|----------|--------|-----------|--------|--------|--------|
| 3.3.1 | MET-01-V-L1 | Calibrated multimeter on secondary | | | ±1V | ☐ Pass ☐ Fail |
| 3.3.2 | MET-01-V-L2 | Calibrated multimeter | | | ±1V | ☐ Pass ☐ Fail |
| 3.3.3 | MET-01-V-L3 | Calibrated multimeter | | | ±1V | ☐ Pass ☐ Fail |
| 3.3.4 | MET-01-kW | Clamp meter + power analyser | | | ±2% | ☐ Pass ☐ Fail |
| 3.3.5 | MET-01-Hz | Power analyser | | | ±0.01 Hz | ☐ Pass ☐ Fail |

### 3.4 Binary Sensor Validation

| # | Sensor Tag | Test | Expected | Actual | Result |
|---|-----------|------|----------|--------|--------|
| 3.4.1 | LD-01a | Apply water to rope sensor | 1 (alarm) | | ☐ Pass ☐ Fail |
| 3.4.2 | LD-01b | Apply water to spot probe | 1 (alarm) | | ☐ Pass ☐ Fail |
| 3.4.3 | LD-02a | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.4 | LD-02b | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.5 | LD-03a | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.6 | LD-03b | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.7 | LD-04a | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.8 | LD-04b | Apply water | 1 | | ☐ Pass ☐ Fail |
| 3.4.9 | SEC-DOOR-main | Open main door | 1 | | ☐ Pass ☐ Fail |
| 3.4.10 | SEC-TAMPER | Simulate tamper | 1 | | ☐ Pass ☐ Fail |
| 3.4.11 | TT-FREEZE-L3 | Verify trace heating activates at 3°C | Simulate with ice | | ☐ Pass ☐ Fail |

**All leak detector zones must be tested with actual water.** Dry testing is not acceptable.

**Phase 3 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Calibration Technician | | | |

---

## Phase 4: Adapter Integration Testing

**Duration:** 4-6 hours
**Power status:** Full power. Edge stack running. No IT load yet.

### 4.1 Docker Stack Verification

Run: `docker compose ps` — all containers must be running.

| # | Container | Status | Healthcheck | Result |
|---|-----------|--------|-------------|--------|
| 4.1.1 | mcs-mosquitto | Running | Healthy | ☐ Pass ☐ Fail |
| 4.1.2 | mcs-modbus-adapter | Running | Healthy | ☐ Pass ☐ Fail |
| 4.1.3 | mcs-snmp-adapter | Running | Healthy | ☐ Pass ☐ Fail |
| 4.1.4 | mcs-bacnet-adapter | Running | Healthy | ☐ Pass ☐ Fail |
| 4.1.5 | mcs-orchestrator | Running | Healthy | ☐ Pass ☐ Fail |

### 4.2 MQTT Message Flow

Run automated test script: `test_mqtt_flow.py`

Subscribe to `microlink/ab-baldwinsville/block-01/#` and verify:

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 4.2.1 | Telemetry messages arriving | MQTT subscribe for 60s | >100 messages received | ☐ Pass ☐ Fail |
| 4.2.2 | Message format valid | JSON schema validation | All messages pass schema | ☐ Pass ☐ Fail |
| 4.2.3 | Sequence numbers incrementing | Check seq field per tag | Monotonically increasing | ☐ Pass ☐ Fail |
| 4.2.4 | Timestamps reasonable | Compare ts to wall clock | Within ±5 seconds | ☐ Pass ☐ Fail |
| 4.2.5 | All subsystems represented | Count unique subsystems | All 13 subsystems present | ☐ Pass ☐ Fail |
| 4.2.6 | Quality flags correct | Check q field | GOOD for healthy sensors | ☐ Pass ☐ Fail |
| 4.2.7 | Units match point schedule | Cross-reference u field | All match canonical units | ☐ Pass ☐ Fail |
| 4.2.8 | Retained messages available | Connect new subscriber | Last values immediately received | ☐ Pass ☐ Fail |

### 4.3 Heartbeat Verification

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 4.3.1 | Heartbeat publishing | Subscribe to edge/heartbeat | Message every 30s ±5s | ☐ Pass ☐ Fail |
| 4.3.2 | Adapter statuses in heartbeat | Parse adapters field | All 3 adapters show "running" | ☐ Pass ☐ Fail |
| 4.3.3 | System metrics present | Parse system field | cpu, mem, disk, temp all >0 | ☐ Pass ☐ Fail |
| 4.3.4 | Buffer stats present | Parse buffer field | depth=0, cloud_connected=true | ☐ Pass ☐ Fail |

### 4.4 Cloud Connection

| # | Test | Method | Accept Criteria | Result |
|---|------|--------|----------------|--------|
| 4.4.1 | Cloud broker connected | Heartbeat buffer.cloud_connected | true | ☐ Pass ☐ Fail |
| 4.4.2 | Telemetry arriving in cloud | Stream B ingestion dashboard | Data appearing within 5s of local publish | ☐ Pass ☐ Fail |
| 4.4.3 | Store-and-forward test | Disconnect WAN for 5 min | Buffer depth increases. Reconnect → replay. Cloud receives buffered data. | ☐ Pass ☐ Fail |
| 4.4.4 | Command round-trip | Send diagnostics_request from cloud | Response received with device info within 10s | ☐ Pass ☐ Fail |

### 4.5 Point Count Verification

Run: `test_point_count.py`

| # | Subsystem | Expected Points | Received Points | Match | Result |
|---|-----------|----------------|-----------------|-------|--------|
| 4.5.1 | electrical | 82 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.2 | thermal-l1 | 30 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.3 | thermal-l2 | 25 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.4 | thermal-hx | 18 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.5 | thermal-l3 | 18 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.6 | thermal-reject | 17 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.7 | thermal-safety | 26 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.8 | environmental | 43 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.9 | network | 17 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.10 | security | 10 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.11 | host-bms | 8 | | ☐ | ☐ Pass ☐ Fail |
| 4.5.12 | mode | 6 | | ☐ | ☐ Pass ☐ Fail |
| | **TOTAL** | **342** | | | |

**Phase 4 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Controls Engineer | | | |

---

## Phase 5: Safety System Verification

**Duration:** 4-6 hours
**Power status:** Full power. Pumps running (no IT load). THIS IS CRITICAL.

### 5.1 Hardwired Interlock Tests

**WARNING: These tests involve activating safety devices. Ensure all personnel are clear of rotating equipment.**

| # | Interlock | Test Method | Expected Response | Max Time | Result |
|---|-----------|------------|-------------------|----------|--------|
| 5.1.1 | IL-01: E-stop | Press E-stop button | All pumps stop, valves spring-return | 2 seconds | ☐ Pass ☐ Fail |
| 5.1.2 | IL-01: E-stop reset | Reset E-stop, verify no auto-restart | Pumps remain stopped until manually restarted | Immediate | ☐ Pass ☐ Fail |
| 5.1.3 | IL-02: Leak → pump stop (Zone 1) | Trigger LD-01a with water | PP-01 and PP-02 contactors open (hardwired) | 2 seconds | ☐ Pass ☐ Fail |
| 5.1.4 | IL-02: Leak → pump stop (Zone 2) | Trigger LD-02a with water | Pumps stop | 2 seconds | ☐ Pass ☐ Fail |
| 5.1.5 | IL-02: Leak → pump stop (Zone 3) | Trigger LD-03a with water | Pumps stop | 2 seconds | ☐ Pass ☐ Fail |
| 5.1.6 | IL-02: Leak → pump stop (Zone 4) | Trigger LD-04a with water | Pumps stop | 2 seconds | ☐ Pass ☐ Fail |
| 5.1.7 | IL-03: PRV mechanical | Slowly increase system pressure (test pump) | PRV opens at 500 ±25 kPa | At setpoint | ☐ Pass ☐ Fail |
| 5.1.8 | IL-05: V-REJ spring-return | Remove actuator power (breaker) | V-REJ opens to >95% within 30s | 30 seconds | ☐ Pass ☐ Fail |
| 5.1.9 | IL-06: V-EXP spring-return | Remove actuator power (breaker) | V-EXP closes to <5% within 30s | 30 seconds | ☐ Pass ☐ Fail |

**Test 5.1.7 (PRV):** This is a destructive test (PRV may need re-seating after activation). Have replacement ready. Record actual lift pressure: ______ kPa.

### 5.2 Safety PLC Verification

| # | Test | Method | Expected | Result |
|---|------|--------|----------|--------|
| 5.2.1 | PLC boots to REJECT | Power cycle PLC | Mode register = 2 (REJECT) within 10s | ☐ Pass ☐ Fail |
| 5.2.2 | PLC watchdog incrementing | Read register 45405 twice, 5s apart | Value has increased | ☐ Pass ☐ Fail |
| 5.2.3 | PLC fault register clear | Read register 45407 | Value = 0 | ☐ Pass ☐ Fail |
| 5.2.4 | Leak → PLC emergency REJECT | Trigger any leak detector | PLC mode → 2 (REJECT), V-REJ opens | ☐ Pass ☐ Fail |
| 5.2.5 | PLC reads all safety sensors | Check PLC I/O status page | All inputs show valid (no comm faults) | ☐ Pass ☐ Fail |

### 5.3 Alarm Generation Tests

| # | Test | Method | Expected Alarm | Priority | Result |
|---|------|--------|---------------|----------|--------|
| 5.3.1 | Leak alarm | Trigger LD-01a | MQTT alarm: RAISED, P0, LEAK | P0 | ☐ Pass ☐ Fail |
| 5.3.2 | Leak alarm clears | Dry LD-01a sensor | MQTT alarm: CLEARED | - | ☐ Pass ☐ Fail |
| 5.3.3 | Temperature alarm | Heat TT-L2s above 52°C (heat gun on probe) | MQTT alarm: RAISED, P2 | P2 | ☐ Pass ☐ Fail |
| 5.3.4 | Pressure alarm | Increase PT-L2s above 400 kPa | MQTT alarm: RAISED, P1 | P1 | ☐ Pass ☐ Fail |
| 5.3.5 | UPS on-battery trap | Disconnect UPS utility input | SNMP trap → MQTT alarm, P1 | P1 | ☐ Pass ☐ Fail |

**Phase 5 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Safety Assessor | | | |

---

## Phase 6: Mode State Machine Testing

**Duration:** 6-8 hours
**Power status:** Full power. Pumps running. Partial IT load recommended (~200kW minimum to generate heat).

### 6.1 Mode Transition Tests

| # | Transition | Setup | Trigger | Expected | Verify | Result |
|---|-----------|-------|---------|----------|--------|--------|
| 6.1.1 | T01: STARTUP→REJECT | Power cycle PLC | Auto | V-REJ opens, V-EXP closed, pumps start | Mode=REJECT within 60s | ☐ Pass ☐ Fail |
| 6.1.2 | T02: REJECT→EXPORT | Wait in REJECT with IT load running, host demand ON | Auto (guards met for 5 min) | V-EXP opens, V-REJ closes, heat at HX | TT-HXsOut rises, billing kWt >0 | ☐ Pass ☐ Fail |
| 6.1.3 | T05: EXPORT→REJECT (host demand off) | In EXPORT mode | Set HOST-demand = 0 | V-REJ opens within 15s, V-EXP closes | Mode=REJECT, dry cooler starts | ☐ Pass ☐ Fail |
| 6.1.4 | T05: EXPORT→REJECT (leak) | In EXPORT mode | Trigger LD-01a | Mode→REJECT within 2s, P0 alarm | V-REJ open, pumps running | ☐ Pass ☐ Fail |
| 6.1.5 | T05: EXPORT→REJECT (sensor fault) | In EXPORT mode | Disconnect TT-L2s | q=BAD for 5s → Mode→REJECT | P1 alarm raised | ☐ Pass ☐ Fail |
| 6.1.6 | T06: EXPORT→MIXED | In EXPORT, increase load until TT-L2s >48°C | Auto (sustained 30s) | Valves modulate, DC fans start | Both V-EXP and V-REJ partially open | ☐ Pass ☐ Fail |
| 6.1.7 | T08: MIXED→EXPORT | In MIXED, reduce load until TT-L2s <45°C | Auto (sustained 3 min + 5 min dwell) | V-EXP→100%, V-REJ→0% | Full export restored | ☐ Pass ☐ Fail |
| 6.1.8 | T04: REJECT→MAINTENANCE | In REJECT | Key-switch (or auth command) | Pumps ramp down, all valves close | Mode=MAINTENANCE, audit log entry | ☐ Pass ☐ Fail |
| 6.1.9 | T11: MAINTENANCE→REJECT | In MAINTENANCE | Release key-switch | V-REJ opens, pumps restart | Mode=REJECT, never goes to EXPORT directly | ☐ Pass ☐ Fail |

### 6.2 Anti-Hunting Test

| # | Test | Method | Expected | Result |
|---|------|--------|----------|--------|
| 6.2.1 | Rapid host demand toggling | Toggle HOST-demand on/off every 30s for 5 min | Mode changes limited by dwell timers, no rapid oscillation | ☐ Pass ☐ Fail |
| 6.2.2 | MIXED valve stability | In MIXED, observe valve positions for 10 min | Position changes spaced ≥30s apart (anti-hunt timer) | ☐ Pass ☐ Fail |

### 6.3 Override Tests

| # | Test | Method | Expected | Result |
|---|------|--------|----------|--------|
| 6.3.1 | Key-switch override to REJECT | In EXPORT, turn key-switch | Immediate REJECT, audit logged | ☐ Pass ☐ Fail |
| 6.3.2 | Cloud command override | Send mode_override:reject from cloud | REJECT, response "accepted" | ☐ Pass ☐ Fail |
| 6.3.3 | Override release + recovery | Release override | System returns to normal REJECT, may auto-transition to EXPORT | ☐ Pass ☐ Fail |

**Phase 6 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Controls Engineer | | | |

---

## Phase 7: Thermal Performance Validation

**Duration:** 8-12 hours (requires sustained IT load)
**Power status:** Full power. IT load at 50%+ (≥350kW) for meaningful thermal testing.

### 7.1 Heat Balance Test

Run IT load at steady state for minimum 2 hours. Record:

| Parameter | Value | Unit |
|-----------|-------|------|
| IT load (MET-01-kW minus cooling) | | kW |
| Cooling overhead (pumps + fans) | | kW |
| Total electrical (MET-01-kW) | | kW |
| Loop 2 ΔT (TT-L2s − TT-L2r) | | °C |
| Loop 2 flow (FT-L2) | | m³/h |
| Calculated thermal power (flow × ρ × Cp × ΔT) | | kW |
| Billing meter thermal power (MET-T1-kWt) | | kW |
| Heat balance error (IT load vs thermal) | | % |

**Accept criteria:** Heat balance within ±5%. If error >5%, check flow meter calibration and sensor accuracy.

### 7.2 EXPORT Mode Performance

| # | Measurement | Value | Accept Criteria | Result |
|---|------------|-------|----------------|--------|
| 7.2.1 | HX approach ΔT at 50% load | | <8°C | ☐ Pass ☐ Fail |
| 7.2.2 | HX approach ΔT at 75% load | | <10°C | ☐ Pass ☐ Fail |
| 7.2.3 | Host supply temp (TT-HXsOut) | | >35°C sustained | ☐ Pass ☐ Fail |
| 7.2.4 | Billing meter vs calculated | | Within ±3% | ☐ Pass ☐ Fail |
| 7.2.5 | Export thermal energy over 2h test | | >0 kWht | ☐ Pass ☐ Fail |

### 7.3 REJECT Mode Performance

| # | Measurement | Value | Accept Criteria | Result |
|---|------------|-------|----------------|--------|
| 7.3.1 | Dry cooler outlet temp | | < ambient + 15°C | ☐ Pass ☐ Fail |
| 7.3.2 | Loop 2 supply temp stability | | < 55°C at full load | ☐ Pass ☐ Fail |
| 7.3.3 | Noise at property boundary | | < 65 dBA | ☐ Pass ☐ Fail |

### 7.4 PUE Calculation

| Component | Power (kW) |
|-----------|-----------|
| IT load | |
| CDU pumps (internal) | |
| Loop 2 pumps (PP-01) | |
| Dry cooler fans (in REJECT) | |
| UPS losses | |
| Transformer losses | |
| Controls & lighting | |
| **Total facility power** | |
| **PUE = Total / IT** | |

**Accept criteria:** PUE < 1.15 in EXPORT mode, PUE < 1.25 in REJECT mode.

**Phase 7 sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Commissioning Engineer | | | |
| Thermal Engineer | | | |

---

## Phase 8: End-to-End Acceptance & Handover

**Duration:** 4 hours
**Power status:** Full power. All systems operational.

### 8.1 72-Hour Burn-In

Before final acceptance, system must run continuously for 72 hours with:
- IT load at ≥50% capacity
- At least one EXPORT→REJECT→EXPORT cycle
- No unplanned mode changes
- No P0 or P1 alarms (P2/P3 acceptable if documented)
- Cloud connectivity maintained ≥99% of the time
- All 342 sensor points reporting q=GOOD

**Burn-in log:**

| Day | Hours | Mode Distribution | Alarms | Uptime | Notes |
|-----|-------|-------------------|--------|--------|-------|
| 1 | 0-24 | | | | |
| 2 | 24-48 | | | | |
| 3 | 48-72 | | | | |

### 8.2 Documentation Handover

| # | Document | Status | Location |
|---|----------|--------|----------|
| 8.2.1 | As-built point schedule (CSV) | ☐ Complete | Site folder / Google Drive |
| 8.2.2 | Network diagram (as-built) | ☐ Complete | |
| 8.2.3 | Firewall rule set (exported config) | ☐ Complete | |
| 8.2.4 | Sensor calibration certificates | ☐ Complete | |
| 8.2.5 | Phase 1-7 signed checklists | ☐ Complete | |
| 8.2.6 | 72-hour burn-in log | ☐ Complete | |
| 8.2.7 | Edge controller config backup | ☐ Complete | Encrypted cloud backup |
| 8.2.8 | PLC program backup | ☐ Complete | Version-controlled |
| 8.2.9 | Switch/firewall config backup | ☐ Complete | Encrypted cloud backup |
| 8.2.10 | Maintenance schedule (first year) | ☐ Complete | |

### 8.3 Training Sign-Off

| # | Topic | Trainee(s) | Trainer | Date |
|---|-------|-----------|---------|------|
| 8.3.1 | Mode state overview & override procedure | Site operator(s) | | |
| 8.3.2 | Alarm response procedures (P0-P3) | Site operator(s) | | |
| 8.3.3 | E-stop location and procedure | All site personnel | | |
| 8.3.4 | Dashboard navigation (Stream D) | Site operator(s) | | |
| 8.3.5 | Escalation contacts | Site operator(s) | | |

### 8.4 Final Acceptance

**System acceptance criteria — ALL must be true:**

- [ ] All Phase 1-7 checklists signed with no outstanding failures
- [ ] 72-hour burn-in completed with no P0/P1 alarms
- [ ] All 342 sensor points reporting valid data
- [ ] Mode state machine tested through all transitions
- [ ] Safety interlocks verified (hardwired and PLC)
- [ ] Billing meters calibrated and recording
- [ ] Cloud telemetry flowing with <10s latency
- [ ] Store-and-forward tested and verified
- [ ] Documentation package complete
- [ ] Training completed

**Final sign-off:**

| Role | Name | Signature | Date |
|------|------|-----------|------|
| MicroLink Commissioning Engineer | | | |
| MicroLink Controls Engineer | | | |
| Host Facility Representative | | | |
| MicroLink Operations Manager | | | |

---

**Site status upon acceptance: OPERATIONAL**
**Handover to: MicroLink NOC (remote monitoring) + local site contact**
**First scheduled maintenance: 90 days post-commissioning**
