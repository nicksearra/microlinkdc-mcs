"""
MCS — Seed Data
================
Populates a fresh MCS database with realistic data for the AB InBev Baldwinsville
pilot site. Usage:
  export DB_HOST=localhost DB_USER=mcs_admin DB_PASSWORD=localdev
  python db/seed_data.py

Requires: pip install psycopg2-binary
"""

import json, math, os, random
from datetime import datetime, timedelta, timezone
import psycopg2, psycopg2.extras

DB_DSN = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'mcs')} "
    f"user={os.getenv('DB_USER', 'mcs_admin')} "
    f"password={os.getenv('DB_PASSWORD', 'localdev')}"
)
NOW = datetime.now(timezone.utc)
SEED_START = NOW - timedelta(hours=24)
random.seed(42)

# Equipment: (tag, type, subsystem, sensors[])
# Sensor:    (tag, desc, unit, rmin, rmax, nominal, noise, thresholds|None)
EQUIPMENT = [
    ("CDU-01", "coolant_distribution_unit", "thermal-l1", [
        ("CDU-01-T-SUP", "CDU 01 Supply", "°C", 15, 40, 31.2, 1.5,
         {"HH": {"value": 38, "priority": "P1", "delay_s": 30}, "H": {"value": 35, "priority": "P2", "delay_s": 60}}),
        ("CDU-01-T-RET", "CDU 01 Return", "°C", 30, 70, 42.8, 2.0,
         {"HH": {"value": 60, "priority": "P0", "delay_s": 10}, "H": {"value": 55, "priority": "P1", "delay_s": 30}}),
        ("CDU-01-FLOW", "CDU 01 Flow", "L/min", 0, 200, 85.2, 5.0,
         {"LL": {"value": 40, "priority": "P1", "delay_s": 30}, "L": {"value": 65, "priority": "P2", "delay_s": 60}}),
        ("CDU-01-DP", "CDU 01 Diff Pressure", "kPa", 0, 200, 95.0, 8.0, None),
    ]),
    ("CDU-02", "coolant_distribution_unit", "thermal-l1", [
        ("CDU-02-T-SUP", "CDU 02 Supply", "°C", 15, 40, 31.5, 1.5,
         {"HH": {"value": 38, "priority": "P1", "delay_s": 30}}),
        ("CDU-02-T-RET", "CDU 02 Return", "°C", 30, 70, 43.1, 2.0,
         {"HH": {"value": 60, "priority": "P0", "delay_s": 10}, "H": {"value": 55, "priority": "P1", "delay_s": 30}}),
        ("CDU-02-FLOW", "CDU 02 Flow", "L/min", 0, 200, 82.7, 5.0,
         {"LL": {"value": 40, "priority": "P1", "delay_s": 30}}),
    ]),
    ("ML-PUMP-A", "circulation_pump", "thermal-l2", [
        ("ML-PUMP-A-SPEED", "Primary Pump A", "Hz", 0, 60, 48.2, 1.0, None),
        ("ML-PUMP-A-AMPS", "Primary Pump A Current", "A", 0, 30, 12.5, 0.8, None),
    ]),
    ("ML-PUMP-B", "circulation_pump", "thermal-l2", [
        ("ML-PUMP-B-SPEED", "Primary Pump B (standby)", "Hz", 0, 60, 0.0, 0.1, None),
    ]),
    ("ML-LOOP", "primary_loop", "thermal-l2", [
        ("ML-FLOW", "Primary Loop Flow", "L/min", 0, 600, 340.5, 15.0,
         {"LL": {"value": 200, "priority": "P1", "delay_s": 30}, "L": {"value": 300, "priority": "P2", "delay_s": 60}}),
        ("ML-T-SUP", "Glycol Supply", "°C", 15, 50, 28.3, 1.5, None),
        ("ML-T-RET", "Glycol Return", "°C", 30, 60, 45.6, 2.0, None),
        ("ML-GLYCOL-CONC", "Glycol Concentration", "%", 20, 60, 34.2, 0.5,
         {"LL": {"value": 28, "priority": "P2", "delay_s": 120}, "L": {"value": 32, "priority": "P3", "delay_s": 300}}),
    ]),
    ("PHX-01", "plate_heat_exchanger", "thermal-l3", [
        ("PHX-01-T-PRI-IN", "PHX Primary In", "°C", 30, 60, 45.6, 2.0, None),
        ("PHX-01-T-PRI-OUT", "PHX Primary Out", "°C", 20, 40, 30.1, 1.5, None),
        ("PHX-01-T-SEC-IN", "Host Water In", "°C", 10, 35, 25.4, 1.0, None),
        ("PHX-01-T-SEC-OUT", "Host Water Out", "°C", 30, 55, 41.2, 2.0,
         {"HH": {"value": 52, "priority": "P1", "delay_s": 60}}),
        ("HOST-FLOW", "Host Flow Rate", "L/min", 0, 500, 280.3, 12.0, None),
    ]),
    ("UPS-01", "ups", "electrical", [
        ("UPS-01-LOAD", "UPS 01 Load", "%", 0, 100, 72.4, 3.0,
         {"HH": {"value": 95, "priority": "P0", "delay_s": 5}, "H": {"value": 85, "priority": "P1", "delay_s": 30}}),
        ("UPS-01-BAT-SOC", "UPS 01 Battery SOC", "%", 0, 100, 98.1, 0.5,
         {"LL": {"value": 20, "priority": "P0", "delay_s": 5}, "L": {"value": 50, "priority": "P1", "delay_s": 30}}),
        ("UPS-01-BAT-V", "UPS 01 Battery Voltage", "V", 300, 500, 432.0, 3.0, None),
    ]),
    ("MSB-01", "main_switchboard", "electrical", [
        ("P-MSB-TOTAL", "Total IT Load", "kW", 0, 1200, 847.0, 20.0, None),
        ("V-MSB-L1", "Mains Voltage L1", "V", 400, 520, 481.2, 3.0,
         {"HH": {"value": 510, "priority": "P1", "delay_s": 10}, "LL": {"value": 430, "priority": "P1", "delay_s": 10}}),
        ("V-MSB-L2", "Mains Voltage L2", "V", 400, 520, 480.8, 3.0, None),
        ("V-MSB-L3", "Mains Voltage L3", "V", 400, 520, 481.5, 3.0, None),
        ("I-MSB-TOTAL", "Total Current", "A", 0, 2000, 1023.0, 30.0, None),
        ("PF-MSB", "Power Factor", "", 0.8, 1.0, 0.97, 0.01, None),
    ]),
    ("PDU-R01", "pdu", "electrical", [
        ("PDU-R01-KW", "PDU Rack 01", "kW", 0, 100, 42.5, 3.0, None),
    ]),
    ("ENV-01", "environmental_sensor", "environmental", [
        ("ENV-T-AMB", "Ambient Temp", "°C", -10, 50, 22.1, 4.0,
         {"HH": {"value": 40, "priority": "P2", "delay_s": 120}}),
        ("ENV-RH", "Relative Humidity", "%", 0, 100, 45.3, 5.0,
         {"HH": {"value": 80, "priority": "P2", "delay_s": 300}}),
        ("ENV-DUST", "Dust Particles", "ppm", 0, 100, 12.0, 3.0, None),
    ]),
    ("SW-CORE-01", "network_switch", "network", [
        ("SW-CORE-01-CPU", "Core Switch CPU", "%", 0, 100, 35.0, 8.0, None),
        ("SW-CORE-01-TEMP", "Core Switch Temp", "°C", 20, 70, 42.0, 3.0, None),
    ]),
    ("DOOR-01", "door_sensor", "security", [
        ("DOOR-MAIN", "Main Door", "", 0, 1, 0.0, 0.0, None),
    ]),
]


def main():
    print(f"Connecting to DB...")
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Tenants
        print("Creating tenants...")
        tenant_ml = _upsert_tenant(cur, 'microlink', 'MicroLink Data Centers', 'internal')
        tenant_gpu = _upsert_tenant(cur, 'gpucloud', 'GPU Cloud Services', 'customer')
        tenant_host = _upsert_tenant(cur, 'abinbev-baldwinsville', 'AB InBev Baldwinsville', 'host')

        # Site
        print("Creating site...")
        cur.execute("""
            INSERT INTO sites (slug, name, region, status, latitude, longitude, tenant_id, config_json)
            VALUES ('baldwinsville', 'AB InBev Baldwinsville', 'US-East', 'active',
                    43.1587, -76.3327, %s, '{"address":"7792 Plainville Rd, Baldwinsville, NY"}')
            ON CONFLICT (slug) DO NOTHING RETURNING id
        """, (tenant_host,))
        r = cur.fetchone()
        site_id = r[0] if r else _slug_id(cur, "sites", "baldwinsville")

        # Block
        print("Creating block...")
        cur.execute("""
            INSERT INTO blocks (site_id, slug, capacity_mw, status, availability, thermal_mode,
                                commissioned_at, config_json)
            VALUES (%s, 'block-01', 1.0, 'active', 'B', 'FULL_RECOVERY', %s,
                    '{"rack_count":14,"max_kw_per_rack":80}')
            ON CONFLICT (slug) DO NOTHING RETURNING id
        """, (site_id, NOW - timedelta(days=90)))
        r = cur.fetchone()
        block_id = r[0] if r else _slug_id(cur, "blocks", "block-01")

        # Tenant access
        for tid, lvl in [(tenant_ml, 'admin'), (tenant_gpu, 'read'), (tenant_host, 'read')]:
            cur.execute("""
                INSERT INTO tenant_access (tenant_id, site_id, block_id, access_level)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (tid, site_id, block_id, lvl))

        # Equipment + Sensors
        print("Creating equipment & sensors...")
        sensor_defs = []
        for eq_tag, eq_type, subsystem, sensors in EQUIPMENT:
            cur.execute("""
                INSERT INTO equipment (block_id, tag, type, subsystem)
                VALUES (%s, %s, %s, %s) ON CONFLICT (block_id, tag) DO NOTHING RETURNING id
            """, (block_id, eq_tag, eq_type, subsystem))
            r = cur.fetchone()
            eq_id = r[0] if r else _eq_id(cur, block_id, eq_tag)

            for sdef in sensors:
                tag, desc, unit, rmin, rmax, nominal, noise = sdef[:7]
                thresh = sdef[7] if len(sdef) > 7 else None
                cur.execute("""
                    INSERT INTO sensors (equipment_id, tag, description, unit,
                                         range_min, range_max, poll_rate_ms, alarm_thresholds_json)
                    VALUES (%s,%s,%s,%s,%s,%s,5000,%s) ON CONFLICT (equipment_id, tag) DO NOTHING RETURNING id
                """, (eq_id, tag, desc, unit, rmin, rmax, json.dumps(thresh) if thresh else None))
                r = cur.fetchone()
                sid = r[0] if r else _sensor_id(cur, eq_id, tag)
                sensor_defs.append((sid, tag, nominal, noise))

        print(f"  {len(sensor_defs)} sensors")

        # Telemetry — 24h at 1-min intervals
        print("Generating telemetry (24h × 1min)...")
        rows = []
        t = SEED_START
        while t < NOW:
            hrs = (t - SEED_START).total_seconds() / 3600
            for sid, tag, nom, noise in sensor_defs:
                drift = math.sin(hrs / 24 * 2 * math.pi) * noise * 0.5
                val = nom + drift + random.gauss(0, noise * 0.3)
                val = max(nom - noise * 4, min(nom + noise * 4, val))
                rows.append((t.isoformat(), sid, round(val, 3), 0))
            t += timedelta(minutes=1)

        for i in range(0, len(rows), 5000):
            psycopg2.extras.execute_values(
                cur, "INSERT INTO telemetry (time, sensor_id, value, quality) VALUES %s",
                rows[i:i+5000], template="(%s,%s,%s,%s)", page_size=5000)
        print(f"  {len(rows):,} rows")

        # Alarms
        print("Creating alarms...")
        cdu_ret_id = next(s[0] for s in sensor_defs if s[1] == "CDU-01-T-RET")
        glycol_id = next(s[0] for s in sensor_defs if s[1] == "ML-GLYCOL-CONC")
        cur.execute("INSERT INTO alarms (sensor_id, priority, state, raised_at) VALUES (%s,'P1','ACTIVE',%s)",
                    (cdu_ret_id, NOW - timedelta(minutes=3)))
        cur.execute("INSERT INTO alarms (sensor_id, priority, state, raised_at, acked_at, acked_by) VALUES (%s,'P2','ACKED',%s,%s,'nick.searra')",
                    (glycol_id, NOW - timedelta(hours=1), NOW - timedelta(minutes=50)))

        # Events
        print("Creating events...")
        for etype, src, sev, payload, ts in [
            ("mode_change", "orchestrator", "info", {"from":"STARTUP","to":"FULL_RECOVERY"}, NOW-timedelta(days=89)),
            ("alarm_raised", "alarm_engine", "warning", {"tag":"CDU-01-T-RET","priority":"P1"}, NOW-timedelta(minutes=3)),
            ("operator_action", "dashboard", "info", {"action":"ack","tag":"ML-GLYCOL-CONC"}, NOW-timedelta(minutes=50)),
        ]:
            payload["source"] = src
            payload["severity"] = sev
            cur.execute("INSERT INTO events (block_id, event_type, payload, created_at) VALUES (%s,%s,%s,%s)",
                        (block_id, etype, json.dumps(payload), ts))

        conn.commit()
        print(f"\n{'='*50}")
        print(f"  SEED COMPLETE")
        print(f"  Site: baldwinsville | Block: block-01")
        print(f"  {len(sensor_defs)} sensors | {len(rows):,} telemetry rows")
        print(f"  curl http://localhost:8000/docs")
        print(f"{'='*50}\n")

    except Exception as e:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _upsert_tenant(cur, slug, name, role):
    cur.execute("INSERT INTO tenants (slug,name,role) VALUES (%s,%s,%s) ON CONFLICT (slug) DO NOTHING RETURNING id", (slug, name, role))
    r = cur.fetchone()
    return r[0] if r else _slug_id(cur, "tenants", slug)

def _slug_id(cur, table, slug):
    cur.execute(f"SELECT id FROM {table} WHERE slug=%s", (slug,))
    return cur.fetchone()[0]

def _eq_id(cur, block_id, tag):
    cur.execute("SELECT id FROM equipment WHERE block_id=%s AND tag=%s", (block_id, tag))
    return cur.fetchone()[0]

def _sensor_id(cur, eq_id, tag):
    cur.execute("SELECT id FROM sensors WHERE equipment_id=%s AND tag=%s", (eq_id, tag))
    return cur.fetchone()[0]

if __name__ == "__main__":
    main()
