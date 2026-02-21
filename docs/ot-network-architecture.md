# MicroLink MCS — OT Network Architecture

## IEC 62443 Zone & Conduit Model · v1.0.0

> **This document defines the network architecture for a MicroLink 1MW compute block.**
> It governs VLAN assignments, firewall rules, access controls, and hardening
> requirements. All deployments MUST conform to this architecture.
> Reference site: AB InBev Baldwinsville.

---

## 1. Design Principles

1. **Defence in depth** — no single point of compromise grants access to safety systems
2. **Least privilege** — every device and service can only reach what it needs
3. **IT uptime independence** — OT/safety zones operate if the IT/cloud zone is fully compromised
4. **Host isolation** — MicroLink and host BMS networks are physically separated
5. **Fail closed** — firewall default is DENY ALL; only explicit rules permit traffic
6. **No direct internet from OT** — all cloud traffic exits through the edge gateway DMZ
7. **Compliance alignment** — IEC 62443 SL-2 target for zones 1-3, SL-1 for zones 4-5

---

## 2. Zone Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ZONE 5: CLOUD / ENTERPRISE                  │
│  Cloud MQTT Broker · Stream B/C/D · NOC Dashboard · Lender Portal  │
│                        (Anthropic / AWS / Azure)                    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ TLS 1.3 / mTLS
                               │ Port 8883 (MQTTS)
                               │ Port 443 (HTTPS management)
                    ┌──────────┴──────────┐
                    │   ZONE 4: EDGE DMZ  │
                    │   VLAN 40 · 10.10.40.0/24
                    │   Edge Gateway (orchestrator)
                    │   Cloud MQTT bridge
                    │   VPN concentrator
                    │   NTP client
                    └──────────┬──────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                   │
   ┌────────┴────────┐ ┌──────┴───────┐ ┌────────┴────────┐
   │  ZONE 3: IT/DC  │ │ ZONE 2: OT   │ │ ZONE 1: SAFETY  │
   │  VLAN 30        │ │ VLAN 20      │ │ VLAN 10         │
   │  10.10.30.0/24  │ │ 192.168.10.0 │ │ 192.168.1.0/24  │
   │                 │ │ /24          │ │                  │
   │ PDUs (SNMP)     │ │ Power meter  │ │ Safety PLC       │
   │ UPS (SNMP)      │ │ Thermal I/O  │ │ Leak detectors   │
   │ Switches        │ │ CDU Modbus   │ │ PRV sensors      │
   │ Customer racks  │ │ Pumps/VFDs   │ │ Freeze sensors   │
   │ IPMI/iDRAC      │ │ Valve posn   │ │ E-stop circuit   │
   │ Cameras (IP)    │ │ Dry cooler   │ │ Hardwired I/O    │
   └─────────────────┘ │ Expansion    │ └──────────────────┘
                        │ Chemistry    │
                        └──────┬───────┘
                               │
                    ┌──────────┴──────────┐
                    │  ZONE 2B: HOST BMS  │
                    │  VLAN 25            │
                    │  192.168.20.0/24    │
                    │  BACnet/IP          │
                    │  READ-ONLY access   │
                    └─────────────────────┘
```

---

## 3. Zone Definitions

### Zone 1: Safety (SL-2)

| Attribute | Value |
|-----------|-------|
| VLAN | 10 |
| Subnet | 192.168.1.0/24 |
| Purpose | Safety-critical instrumentation and PLC |
| Security Level | SL-2 (IEC 62443) |
| Physical | Dedicated cabling, locked cabinet, tamper-evident |

**Devices:**

| Device | IP | Function |
|--------|----|----------|
| Safety PLC | 192.168.1.1 | Mode state machine, valve control |
| Leak det controller | 192.168.1.10 | 8-zone leak detection |
| Freeze/PRV I/O | 192.168.1.11 | Temperature switches, PRV status |
| Safety HMI | 192.168.1.20 | Local touchscreen (key-switch access) |

**Access rules:**
- Only the edge gateway on VLAN 40 can READ from the Safety PLC (Modbus TCP port 502)
- NO device outside Zone 1 can WRITE to the Safety PLC
- Cloud commands for mode override go: Cloud → Edge (VLAN 40) → PLC command register (authenticated, 2FA)
- Hardwired interlocks (E-stop, leak→pump-stop) operate entirely within Zone 1 wiring — no network dependency
- Physical key-switch required for local override

**Hardening:**
- PLC firmware signed and version-locked
- Modbus TCP restricted to read-only function codes (FC03, FC04) from edge gateway IP only
- Write function codes (FC05, FC06, FC16) only accepted from Safety HMI IP (192.168.1.20)
- No USB ports enabled on PLC
- Configuration changes require physical presence + engineering workstation on Zone 1

---

### Zone 2: OT Process (SL-2)

| Attribute | Value |
|-----------|-------|
| VLAN | 20 |
| Subnet | 192.168.10.0/24 |
| Purpose | Thermal and electrical process instrumentation |
| Security Level | SL-2 |

**Devices:**

| Device | IP | Protocol | Function |
|--------|----| ---------|----------|
| Revenue meter (ION9000) | 192.168.10.10 | Modbus TCP | Billing-grade power |
| Thermal sensor module | 192.168.10.20 | Modbus TCP | Temps, pressures, flows |
| CDU-01 controller | 192.168.10.21 | Modbus TCP | IT coolant management |
| CDU-02 controller | 192.168.10.22 | Modbus TCP | IT coolant management |
| VFD PP-01 | 192.168.10.30 | Modbus RTU/gateway | Duty pump |
| VFD PP-02 | 192.168.10.31 | Modbus RTU/gateway | Standby pump |
| Billing thermal meter | 192.168.10.40 | Modbus TCP | Heat export metering |
| Dry cooler controller | 192.168.10.50 | Modbus TCP | Fan speed, spray |
| Expansion vessel | 192.168.10.60 | Modbus TCP | Pressure, level |
| Chemistry analyser | 192.168.10.70 | Modbus TCP | pH, conductivity |
| Modbus RTU gateway | 192.168.10.99 | Serial→TCP bridge | RS-485 devices |

**Access rules:**
- Edge gateway (VLAN 40) reads all devices via Modbus TCP
- Edge gateway is the ONLY external accessor — no other zone reaches these devices
- Modbus RTU devices connect through a serial gateway (not directly IP-accessible)
- No internet access, no DNS, no DHCP (static IPs only)

---

### Zone 2B: Host BMS (SL-1)

| Attribute | Value |
|-----------|-------|
| VLAN | 25 |
| Subnet | 192.168.20.0/24 |
| Purpose | Read-only interface to host Building Management System |
| Security Level | SL-1 |

**Devices:**

| Device | IP | Protocol | Function |
|--------|----| ---------|----------|
| Host BMS controller | 192.168.20.1 | BACnet/IP | Brewery automation |
| Edge BACnet interface | 192.168.20.10 | BACnet/IP | Our read-only listener |

**Access rules:**
- Edge BACnet adapter (192.168.20.10) can READ from host BMS (192.168.20.1)
- BACnet port 47808 (host) and 47809 (our adapter) only
- Absolutely NO WRITE operations — contractual obligation
- This VLAN is physically separated from all other MicroLink VLANs
- Host manages their own BMS security; we only observe

**Rationale for separate zone:** The host's BMS is their property. We must not introduce any risk to their automation systems. Physical VLAN separation ensures that even if our OT network is compromised, their BMS is untouched.

---

### Zone 3: IT/Data Centre (SL-1)

| Attribute | Value |
|-----------|-------|
| VLAN | 30 |
| Subnet | 10.10.30.0/24 |
| Purpose | Customer IT equipment and DC infrastructure |
| Security Level | SL-1 |

**Devices:**

| Device | IP Range | Protocol | Function |
|--------|----------|----------|----------|
| PDU-01 | 10.10.30.40 | SNMP | Rack power (1-5) |
| PDU-02 | 10.10.30.41 | SNMP | Rack power (6-10) |
| PDU-03 | 10.10.30.42 | SNMP | Rack power (11-14) |
| UPS-01 | 10.10.30.30 | SNMP | Battery backup |
| Core switch SW-01 | 10.10.30.1 | SNMP | Network core |
| Access switch SW-02 | 10.10.30.2 | SNMP | Customer ports |
| IP cameras | 10.10.30.50-53 | RTSP/ONVIF | Security cameras |
| Door controllers | 10.10.30.60-62 | Modbus RTU | Access control |
| Customer racks | 10.10.30.100-113 | IPMI/iDRAC | Server management |
| Customer uplinks | VLAN 30 trunk | Ethernet | Customer data traffic |

**Access rules:**
- Edge SNMP adapter (via VLAN 40) reads PDUs, UPS, switches
- Customer traffic is on separate VLANs trunked through — MicroLink only monitors infrastructure
- IPMI/iDRAC access restricted to MicroLink management IPs
- Customer data traffic is NOT inspected or monitored (we're not a carrier)

---

### Zone 4: Edge DMZ (SL-2)

| Attribute | Value |
|-----------|-------|
| VLAN | 40 |
| Subnet | 10.10.40.0/24 |
| Purpose | Edge controller, cloud bridge, management |
| Security Level | SL-2 |

**Devices:**

| Device | IP | Function |
|--------|----|----------|
| Edge controller | 10.10.40.10 | Orchestrator, adapters (Docker) |
| Local Mosquitto | 10.10.40.10:1883 | Internal MQTT broker |
| Management VPN endpoint | 10.10.40.10 | WireGuard/IPSec |
| NTP client | 10.10.40.10 | Time synchronisation |
| LTE backup modem | 10.10.40.20 | Backup WAN connection |

**Access rules:**
- This is the ONLY zone with outbound internet access (cloud MQTT, NTP, VPN)
- Outbound: TLS 1.3 to cloud broker (port 8883), HTTPS (443), NTP (123), WireGuard (51820)
- Inbound: VPN management only (WireGuard from known IPs)
- Cross-zone access: reads from zones 1, 2, 2B, 3 as described per zone
- Local MQTT (1883) only accessible within Docker internal network — not exposed externally

---

### Zone 5: Cloud / Enterprise

| Attribute | Value |
|-----------|-------|
| Location | Cloud (AWS/Azure) |
| Purpose | Data platform, dashboards, business systems |
| Access | Via internet from edge DMZ, via HTTPS from operators |

**Not managed by this OT architecture document.** Stream B/C/D handle cloud security. The interface point is the TLS-encrypted MQTT connection from Zone 4.

---

## 4. Physical Network Design

### 4.1 Cabling

| Zone | Cable Type | Colour | Routing |
|------|-----------|--------|---------|
| Zone 1 (Safety) | Cat6A STP | **Red** | Dedicated conduit, not shared |
| Zone 2 (OT) | Cat6A STP | **Orange** | Dedicated conduit or tray |
| Zone 2B (Host BMS) | Cat6A STP | **Yellow** | Separate conduit to host BMS room |
| Zone 3 (IT/DC) | Cat6A UTP | **Blue** | Standard DC structured cabling |
| Zone 4 (Edge DMZ) | Cat6A STP | **Green** | Short runs within edge enclosure |
| WAN uplink | Single-mode fibre | N/A | ISP demarcation to edge enclosure |

Cable colours are mandatory for visual identification during maintenance.

### 4.2 Switch Architecture

```
                                    WAN (Fibre)
                                        │
                                 ┌──────┴──────┐
                                 │  FW-01      │
                                 │  Firewall   │
                                 │  (pfSense/  │
                                 │   Fortinet) │
                                 └──────┬──────┘
                                        │
                              ┌─────────┴─────────┐
                              │  SW-CORE (L3)     │
                              │  Managed Switch   │
                              │  VLAN routing     │
                              │  ACLs enforced    │
                              └───┬───┬───┬───┬───┘
                                  │   │   │   │
              ┌───────────────────┘   │   │   └───────────────────┐
              │                       │   │                       │
       ┌──────┴──────┐    ┌──────────┴┐  ┌┴──────────┐   ┌──────┴──────┐
       │ VLAN 10     │    │ VLAN 20   │  │ VLAN 25   │   │ VLAN 30     │
       │ Safety      │    │ OT        │  │ Host BMS  │   │ IT/DC       │
       │ (unmanaged  │    │ (managed  │  │ (2-port   │   │ (managed    │
       │  switch or  │    │  switch)  │  │  crossover│   │  switch)    │
       │  direct)    │    │           │  │  or VLAN) │   │             │
       └─────────────┘    └───────────┘  └───────────┘   └─────────────┘
```

**Minimum hardware:**
- 1× managed L3 switch (24-port, PoE optional) — Cisco Catalyst / Aruba / Juniper EX
- 1× firewall appliance — pfSense (CE) or Fortinet FortiGate 60F (enterprise)
- Optional: dedicated unmanaged switch for Zone 1 if PLC has limited Ethernet ports

### 4.3 Edge Controller Connectivity

The edge controller (RPi5 / NUC) has multiple network interfaces:

| Interface | Connection | VLAN | Purpose |
|-----------|-----------|------|---------|
| eth0 | Trunk port to SW-CORE | 10,20,30,40 | All MicroLink zones |
| eth1 | Dedicated port | 25 | Host BMS (physically separate) |
| wlan0 | Disabled | — | Not used in production |
| usb-lte | LTE modem | WAN backup | Failover internet |

eth0 carries tagged VLANs. Docker macvlan networks map to specific VLANs so each adapter container only sees its permitted zone.

---

## 5. Firewall Rules

### 5.1 Core Firewall (FW-01)

Default policy: **DENY ALL** inbound and outbound.

#### Outbound (Edge DMZ → Internet)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| O1 | 10.10.40.10 | mqtt.microlink.energy | 8883 | TCP/TLS | ALLOW | Cloud MQTT |
| O2 | 10.10.40.10 | ntp.ubuntu.com | 123 | UDP | ALLOW | Time sync |
| O3 | 10.10.40.10 | VPN endpoints | 51820 | UDP | ALLOW | Management VPN |
| O4 | 10.10.40.10 | registry.docker.io | 443 | TCP/TLS | ALLOW | Container updates |
| O5 | ANY | ANY | ANY | ANY | **DENY** | Default deny |

#### Inbound (Internet → Edge DMZ)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| I1 | VPN peers (IP list) | 10.10.40.10 | 51820 | UDP | ALLOW | Management VPN |
| I2 | ANY | ANY | ANY | ANY | **DENY** | Default deny |

### 5.2 Inter-VLAN ACLs (SW-CORE)

#### Zone 4 (Edge DMZ) → Zone 1 (Safety)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| S1 | 10.10.40.10 | 192.168.1.1 | 502 | TCP | ALLOW | Modbus READ from PLC |
| S2 | ANY | 192.168.1.0/24 | ANY | ANY | **DENY** | Block all else |

#### Zone 4 (Edge DMZ) → Zone 2 (OT)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| T1 | 10.10.40.10 | 192.168.10.0/24 | 502 | TCP | ALLOW | Modbus TCP |
| T2 | ANY | 192.168.10.0/24 | ANY | ANY | **DENY** | Block all else |

#### Zone 4 (Edge DMZ) → Zone 2B (Host BMS)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| B1 | 192.168.20.10 | 192.168.20.1 | 47808 | UDP | ALLOW | BACnet/IP read |
| B2 | ANY | 192.168.20.0/24 | ANY | ANY | **DENY** | Block all else |

#### Zone 4 (Edge DMZ) → Zone 3 (IT/DC)

| # | Source | Destination | Port | Protocol | Action | Purpose |
|---|--------|-------------|------|----------|--------|---------|
| D1 | 10.10.40.10 | 10.10.30.0/24 | 161 | UDP | ALLOW | SNMP polling |
| D2 | 10.10.30.0/24 | 10.10.40.10 | 162 | UDP | ALLOW | SNMP traps |
| D3 | ANY | 10.10.30.0/24 | ANY | ANY | **DENY** | Block all else |

#### Cross-zone isolation

| # | Source | Destination | Action | Purpose |
|---|--------|-------------|--------|---------|
| X1 | Zone 1 | Zone 2 | **DENY** | Safety isolated from OT |
| X2 | Zone 1 | Zone 3 | **DENY** | Safety isolated from IT |
| X3 | Zone 2 | Zone 1 | **DENY** | OT cannot reach safety |
| X4 | Zone 2 | Zone 3 | **DENY** | OT isolated from IT |
| X5 | Zone 2B | ANY except 4 | **DENY** | Host BMS fully isolated |
| X6 | Zone 3 | Zone 1 | **DENY** | IT cannot reach safety |
| X7 | Zone 3 | Zone 2 | **DENY** | IT cannot reach OT |
| X8 | ANY | Zone 5 | via FW-01 only | All cloud via firewall |

**Key principle:** Zones 1, 2, 2B, and 3 cannot communicate with each other. They can only communicate with Zone 4 (Edge DMZ) through specific permitted ports.

---

## 6. Authentication & Access Control

### 6.1 Device Authentication

| Zone | Method | Details |
|------|--------|---------|
| Zone 1 | Physical key-switch + engineering workstation MAC | MAC whitelist on switch port |
| Zone 2 | Static IP + Modbus device ID | No auth on Modbus (protocol limitation) — rely on network isolation |
| Zone 2B | BACnet device ID | Read-only; network isolation is the primary control |
| Zone 3 | SNMP community string (v2c) or SNMPv3 auth | Rotate community strings quarterly |
| Zone 4 | SSH key + 2FA | Edge controller access requires VPN + SSH key + TOTP |
| Zone 5 | JWT + mTLS | Cloud services use mutual TLS certificates |

### 6.2 Management Access

| Access Type | Method | Authentication | Authorisation |
|-------------|--------|----------------|---------------|
| Edge SSH | VPN → SSH | Key + TOTP | Named accounts (no shared root) |
| Edge Docker | SSH tunnel only | Same as SSH | Docker group membership |
| PLC programming | Physical presence | Engineering workstation + Zone 1 | Audited, change-controlled |
| Cloud dashboard | HTTPS | SSO / JWT | Role-based (operator, engineer, admin) |
| Firmware updates | VPN → Watchtower | Signed images | Auto-update with rollback |

### 6.3 Certificate Management

| Certificate | Issuer | Lifetime | Renewal | Storage |
|-------------|--------|----------|---------|---------|
| Edge → Cloud mTLS | MicroLink CA | 1 year | Auto-renew at 80% | /etc/edge/certs/ (encrypted volume) |
| VPN peer | MicroLink CA | 2 years | Manual + alert at 60 days | VPN config |
| HTTPS management | Let's Encrypt | 90 days | Auto (certbot) | Cloud servers |
| PLC firmware signing | MicroLink root CA | 5 years | Manual | Offline HSM |

---

## 7. Monitoring & Intrusion Detection

### 7.1 Network Monitoring

| What | How | Alert Threshold |
|------|-----|----------------|
| VLAN traffic volume | Switch port counters (SNMP) | >2× baseline for 5 minutes |
| Unknown MAC addresses | Switch MAC table monitoring | Any unknown MAC → P2 alarm |
| ARP anomalies | Edge arpwatch daemon | ARP spoofing detected → P1 alarm |
| Modbus function codes | Edge deep packet inspection | Any write FC to PLC not from HMI → P0 |
| Port scans | Firewall IDS (Suricata/Snort) | Any scan activity → P1 alarm |
| DNS queries from OT | Firewall log | OT zones should NEVER make DNS queries |
| Failed auth attempts | SSH/VPN logs | 5 failures in 5 minutes → block + alert |
| Certificate expiry | Edge cron job | 30 days before expiry → P3 alert |

### 7.2 Logging

All logs are shipped from edge to cloud (within the encrypted MQTT channel) for centralised analysis.

| Source | Format | Retention (edge) | Retention (cloud) |
|--------|--------|-------------------|-------------------|
| Firewall | JSON syslog | 7 days | 2 years |
| Switch ACL hits | SNMP trap → MQTT | 7 days | 2 years |
| Edge auth events | journald → MQTT | 7 days | 5 years |
| Adapter logs | Docker JSON → MQTT | 3 days | 1 year |
| PLC audit trail | Modbus registers → MQTT | Flash (limited) | Indefinite |

---

## 8. Hardening Checklist

### 8.1 Edge Controller (Zone 4)

- [ ] Disable WiFi and Bluetooth
- [ ] Disable unused USB ports (usbguard)
- [ ] Enable full-disk encryption (LUKS)
- [ ] Enable secure boot (if hardware supports)
- [ ] Set BIOS/UEFI password
- [ ] Disable root SSH login
- [ ] Configure fail2ban (5 attempts → 1 hour ban)
- [ ] Enable unattended-upgrades for security patches
- [ ] Configure AppArmor profiles for adapter containers
- [ ] Set Docker daemon to rootless mode
- [ ] Enable audit logging (auditd)
- [ ] Restrict kernel parameters (sysctl hardening)
- [ ] NTP sync verified (chrony with authenticated sources)
- [ ] DNS resolver set to cloud-based (1.1.1.1/9.9.9.9) — only Zone 4
- [ ] Firewall (nftables) on host — backup to network firewall

### 8.2 Network Infrastructure

- [ ] Change all default passwords on switches and firewall
- [ ] Disable unused switch ports (admin down)
- [ ] Enable port security (MAC limiting) on all access ports
- [ ] Disable CDP/LLDP on ports facing OT devices
- [ ] Enable DHCP snooping on IT VLAN (Zone 3)
- [ ] Disable DHCP entirely on Zones 1, 2, 2B (static only)
- [ ] Enable spanning tree BPDU guard on access ports
- [ ] Configure switch management on dedicated VLAN (not any operational VLAN)
- [ ] Enable SNMPv3 on managed switches (disable v1/v2c for switch management)
- [ ] Back up switch and firewall configs to encrypted cloud storage weekly
- [ ] Physical lock on network cabinet
- [ ] Tamper-evident seals on fibre patch panels

### 8.3 OT Devices (Zones 1, 2)

- [ ] Update firmware to latest stable (during commissioning)
- [ ] Disable unused protocols on each device (HTTP/Telnet/FTP)
- [ ] Disable unused Modbus function codes where device supports it
- [ ] Change default Modbus slave IDs from 1 to site-specific values
- [ ] Document and photograph all DIP switch settings
- [ ] Set static IPs on all OT devices (no DHCP)
- [ ] Label every cable at both ends (tag + zone colour)
- [ ] Record MAC addresses of all OT devices in asset register

### 8.4 Quarterly Review

- [ ] Review firewall rules — remove any temporary permits
- [ ] Rotate SNMP community strings
- [ ] Verify certificate expiry dates
- [ ] Review access logs for anomalies
- [ ] Run vulnerability scan on edge controller (from VPN)
- [ ] Verify backup restoration procedure
- [ ] Test failover to LTE backup WAN
- [ ] Confirm Watchtower update logs — no failed updates

---

## 9. Incident Response

### 9.1 Network Security Incidents

| Severity | Example | Response |
|----------|---------|----------|
| P0 | Unauthorised write to Safety PLC | Immediately isolate Zone 1 (pull cable). Engage security team. Preserve logs. |
| P0 | Edge controller compromised | Isolate Zone 4 from WAN. Safety PLC continues independently. Ship replacement. |
| P1 | Unknown device on OT VLAN | Disable switch port immediately. Investigate MAC address. |
| P1 | Port scan detected from inside | Identify source device. Isolate. Check for malware. |
| P2 | Multiple failed VPN auth attempts | Block source IP. Review for credential compromise. |
| P2 | Certificate expiry imminent | Renew immediately. No service interruption expected. |
| P3 | Firmware update failed | Rollback via Watchtower. Investigate root cause. |

### 9.2 Recovery Priorities

1. **Safety system integrity** — verify PLC running, valves responding, hardwired interlocks functional
2. **IT cooling continuity** — verify heat rejection path active (dry cooler running)
3. **Customer compute uptime** — verify racks powered and cooled
4. **Monitoring restoration** — restore edge→cloud telemetry
5. **Heat export** — restore EXPORT mode (revenue — lowest priority vs safety)

---

## 10. Diagrams

### 10.1 Data Flow Diagram

```
Zone 1 (Safety PLC)                 Zone 2 (OT)              Zone 2B (Host BMS)
  │ Modbus TCP :502                   │ Modbus TCP :502         │ BACnet/IP :47808
  │ READ ONLY                         │ READ/WRITE              │ READ ONLY
  │                                   │                         │
  └───────────┐                       │                         │
              │                       │                         │
         ┌────┴───────────────────────┴─────────────────────────┴────┐
         │                    ZONE 4: EDGE GATEWAY                    │
         │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
         │  │ Modbus   │ │ SNMP     │ │ BACnet   │ │ Orchestrator │ │
         │  │ Adapter  │ │ Adapter  │ │ Adapter  │ │ + Buffer     │ │
         │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘ │
         │       │             │            │              │         │
         │       └─────────────┴────────────┘              │         │
         │                     │                           │         │
         │               ┌─────┴──────┐                    │         │
         │               │ Mosquitto  │◄───────────────────┘         │
         │               │ (local)    │──── MQTT/TLS ──────┐        │
         │               └────────────┘                     │        │
         └──────────────────────────────────────────────────┼────────┘
                                                            │
                                                   TLS 1.3 :8883
                                                            │
                                                    ┌───────┴───────┐
                                                    │   ZONE 5:     │
                                                    │   CLOUD MQTT  │
                                                    │   Stream B    │
                                                    └───────────────┘
```

### 10.2 Failure Scenario: Edge Gateway Compromised

```
Zone 1 (Safety) ──── HARDWIRED ──── Valves, Pumps, E-stop
    │
    └── PLC continues running mode state machine autonomously
    └── Leak detection continues via hardwired interlocks
    └── No dependency on edge gateway for safety

Zone 2 (OT) ──── Devices continue operating at last setpoint
    └── VFDs maintain last commanded speed
    └── No new commands possible (acceptable)

Zone 3 (IT/DC) ──── Customer equipment unaffected
    └── Power and cooling continue normally

Result: LOSS OF MONITORING ONLY. No safety impact. No uptime impact.
Replace edge controller, re-provision from config backup, restore.
```

---

## 11. Compliance Mapping

### IEC 62443 Requirements (SL-2 Target)

| Foundational Requirement | Zone 1 | Zone 2 | Zone 4 | Implementation |
|-------------------------|--------|--------|--------|---------------|
| FR 1: Identification & Auth | ✅ | ⚠️ | ✅ | Physical key + MAC whitelist (Z1), Modbus has no auth — network isolates (Z2), SSH key + 2FA (Z4) |
| FR 2: Use Control | ✅ | ✅ | ✅ | Read-only Modbus from edge, no unused services, role-based cloud access |
| FR 3: System Integrity | ✅ | ✅ | ✅ | Signed firmware, no USB, hardened OS, container isolation |
| FR 4: Data Confidentiality | ✅ | ✅ | ✅ | Network isolation (Z1/Z2), TLS for all cloud traffic (Z4) |
| FR 5: Data Flow Control | ✅ | ✅ | ✅ | VLAN ACLs, firewall rules, no cross-zone communication |
| FR 6: Event Response | ✅ | ✅ | ✅ | Alarm system, audit logging, incident response procedures |
| FR 7: Resource Availability | ✅ | ✅ | ✅ | Hardware fail-safes, N+1 pumps, store-and-forward, LTE backup |

**Note:** Modbus TCP has no built-in authentication (FR1 gap for Zone 2). This is a known protocol limitation. Mitigation: strict network isolation ensures only the edge gateway IP can reach Modbus devices. Future: consider OPC UA migration for authenticated OT communication.

---

*This architecture is reviewed annually and updated for any new site deployments. Changes require engineering approval and are documented in the site commissioning record.*
