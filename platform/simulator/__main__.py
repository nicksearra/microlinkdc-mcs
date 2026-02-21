"""
MCS Stream B — Telemetry Simulator

Generates realistic MQTT messages that match Stream A's contract.
Simulates a 1MW MicroLink block with ~300 sensors across all subsystems.

Usage:
    python -m simulator
    # or via docker compose — see docker-compose.yml

Generates data at the configured poll interval with:
  - Realistic value ranges per sensor type
  - Gaussian noise on steady-state values
  - Occasional alarm triggers (configurable probability)
  - Proper topic structure: microlink/{site}/{block}/{subsystem}/{tag}
"""

import asyncio
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiomqtt

# ── Configuration ────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
SITE_ID = os.getenv("SITE_ID", "baldwinsville")
BLOCK_COUNT = int(os.getenv("BLOCK_COUNT", "1"))
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", "5000"))
ALARM_PROBABILITY = float(os.getenv("ALARM_PROBABILITY", "0.002"))  # 0.2% per reading


@dataclass
class SensorDef:
    """Definition of a simulated sensor."""
    tag: str
    subsystem: str
    unit: str
    nominal: float          # steady-state value
    noise_std: float        # Gaussian noise σ
    alarm_lo: Optional[float] = None   # P2 alarm if below
    alarm_hi: Optional[float] = None   # P2 alarm if above
    critical_lo: Optional[float] = None  # P0 alarm if below
    critical_hi: Optional[float] = None  # P0 alarm if above


def build_sensor_registry() -> list[SensorDef]:
    """
    Build a realistic sensor set for a 1MW MicroLink block.
    Based on the Master Point Schedule from the benchmark design.
    """
    sensors = []

    # ── Electrical (40 sensors) ──────────────────────────────────────
    for phase in ["L1", "L2", "L3"]:
        sensors.append(SensorDef(f"V-MSB-{phase}", "electrical", "V", 400.0, 2.0, alarm_lo=380, alarm_hi=420, critical_lo=370, critical_hi=440))
        sensors.append(SensorDef(f"I-MSB-{phase}", "electrical", "A", 800.0, 20.0, alarm_hi=1200, critical_hi=1400))
    sensors.append(SensorDef("PF-MSB", "electrical", "PF", 0.95, 0.02, alarm_lo=0.85))
    sensors.append(SensorDef("P-MSB-TOTAL", "electrical", "kW", 950.0, 15.0, alarm_hi=1050, critical_hi=1100))
    sensors.append(SensorDef("E-MSB-TOTAL", "electrical", "kWh", 22800.0, 0.0))  # counter, no noise

    for ups_id in range(1, 9):
        sensors.append(SensorDef(f"UPS-{ups_id:02d}-LOAD", "electrical", "%", 60.0, 5.0, alarm_hi=85, critical_hi=95))
        sensors.append(SensorDef(f"UPS-{ups_id:02d}-BAT-SOC", "electrical", "%", 95.0, 1.0, alarm_lo=30, critical_lo=15))

    sensors.append(SensorDef("GEN-01-STATUS", "electrical", "bool", 0.0, 0.0))  # 0=standby, 1=running
    sensors.append(SensorDef("GEN-01-FUEL", "electrical", "%", 88.0, 0.5, alarm_lo=25, critical_lo=10))

    # ── Thermal Loop 1 — IT Secondary / CDU (50 sensors) ────────────
    for cdu_id in range(1, 5):
        sensors.append(SensorDef(f"CDU-{cdu_id:02d}-T-SUP", "thermal-l1", "°C", 32.0, 0.5, alarm_hi=38, critical_hi=42))
        sensors.append(SensorDef(f"CDU-{cdu_id:02d}-T-RET", "thermal-l1", "°C", 45.0, 0.8, alarm_hi=55, critical_hi=60))
        sensors.append(SensorDef(f"CDU-{cdu_id:02d}-FLOW", "thermal-l1", "L/min", 180.0, 5.0, alarm_lo=100, critical_lo=50))
        sensors.append(SensorDef(f"CDU-{cdu_id:02d}-P-DIFF", "thermal-l1", "kPa", 120.0, 8.0, alarm_hi=200, critical_hi=250))
        sensors.append(SensorDef(f"CDU-{cdu_id:02d}-PUMP-SPEED", "thermal-l1", "Hz", 42.0, 2.0))

    # Rack-level coolant temps (16 racks × supply/return)
    for rack in range(1, 17):
        sensors.append(SensorDef(f"RK-{rack:02d}-T-IN", "thermal-l1", "°C", 32.0, 0.3))
        sensors.append(SensorDef(f"RK-{rack:02d}-T-OUT", "thermal-l1", "°C", 48.0, 1.5, alarm_hi=55, critical_hi=60))

    # ── Thermal Loop 2 — MicroLink Primary Glycol (30 sensors) ──────
    sensors.append(SensorDef("ML-T-SUP", "thermal-l2", "°C", 28.0, 0.5, alarm_hi=35, critical_hi=40))
    sensors.append(SensorDef("ML-T-RET", "thermal-l2", "°C", 50.0, 1.0, alarm_hi=60, critical_hi=65))
    sensors.append(SensorDef("ML-FLOW", "thermal-l2", "L/min", 600.0, 10.0, alarm_lo=300, critical_lo=150))
    sensors.append(SensorDef("ML-P-SUP", "thermal-l2", "kPa", 350.0, 10.0, alarm_hi=500))
    sensors.append(SensorDef("ML-P-RET", "thermal-l2", "kPa", 200.0, 8.0))
    sensors.append(SensorDef("ML-PUMP-A-SPEED", "thermal-l2", "Hz", 45.0, 2.0))
    sensors.append(SensorDef("ML-PUMP-B-SPEED", "thermal-l2", "Hz", 0.0, 0.0))  # standby
    sensors.append(SensorDef("ML-GLYCOL-CONC", "thermal-l2", "%", 35.0, 0.2, alarm_lo=30, alarm_hi=45))
    sensors.append(SensorDef("ML-EXP-TANK-LEVEL", "thermal-l2", "%", 65.0, 1.0, alarm_lo=20, alarm_hi=90, critical_lo=10))

    # Plate heat exchanger
    sensors.append(SensorDef("PHX-01-T-PRI-IN", "thermal-l2", "°C", 50.0, 1.0))
    sensors.append(SensorDef("PHX-01-T-PRI-OUT", "thermal-l2", "°C", 30.0, 0.5))
    sensors.append(SensorDef("PHX-01-T-SEC-IN", "thermal-l2", "°C", 25.0, 0.5))
    sensors.append(SensorDef("PHX-01-T-SEC-OUT", "thermal-l2", "°C", 45.0, 0.8))
    sensors.append(SensorDef("PHX-01-APPROACH", "thermal-l2", "°C", 5.0, 0.3, alarm_hi=10))

    # 3-way valve TV-01
    sensors.append(SensorDef("TV-01-POS", "thermal-l2", "%", 70.0, 2.0))  # 0=all to host, 100=all to reject

    # ── Thermal Loop 3 — Host Process Water (15 sensors) ────────────
    sensors.append(SensorDef("HOST-T-SUP", "thermal-l3", "°C", 20.0, 1.0))
    sensors.append(SensorDef("HOST-T-RET", "thermal-l3", "°C", 42.0, 1.5, alarm_hi=55))
    sensors.append(SensorDef("HOST-FLOW", "thermal-l3", "L/min", 400.0, 15.0, alarm_lo=200, critical_lo=100))
    sensors.append(SensorDef("HOST-P-SUP", "thermal-l3", "kPa", 300.0, 10.0))
    sensors.append(SensorDef("HOST-P-RET", "thermal-l3", "kPa", 180.0, 8.0))
    sensors.append(SensorDef("HOST-kWht-TOTAL", "thermal-l3", "kWh", 15000.0, 0.0))  # thermal energy counter

    # Thermal metering
    sensors.append(SensorDef("TM-01-HEAT-RATE", "thermal-l3", "kW", 680.0, 20.0))
    sensors.append(SensorDef("TM-01-DELTA-T", "thermal-l3", "°C", 22.0, 1.0))

    # ── Thermal Reject — Dry Coolers (20 sensors) ───────────────────
    for dc in range(1, 5):
        sensors.append(SensorDef(f"DC-{dc:02d}-FAN-SPEED", "thermal-reject", "Hz", 35.0, 3.0))
        sensors.append(SensorDef(f"DC-{dc:02d}-T-AIR-IN", "thermal-reject", "°C", 20.0, 2.0))
        sensors.append(SensorDef(f"DC-{dc:02d}-T-AIR-OUT", "thermal-reject", "°C", 38.0, 2.0))
        sensors.append(SensorDef(f"DC-{dc:02d}-T-FLUID-IN", "thermal-reject", "°C", 48.0, 1.0))
        sensors.append(SensorDef(f"DC-{dc:02d}-T-FLUID-OUT", "thermal-reject", "°C", 30.0, 1.0))

    # ── Thermal Safety (10 sensors) ─────────────────────────────────
    sensors.append(SensorDef("LSH-01-LEAK-IT", "thermal-safety", "bool", 0.0, 0.0))
    sensors.append(SensorDef("LSH-02-LEAK-TPM", "thermal-safety", "bool", 0.0, 0.0))
    sensors.append(SensorDef("PRV-01-STATUS", "thermal-safety", "bool", 0.0, 0.0))
    sensors.append(SensorDef("PRV-02-STATUS", "thermal-safety", "bool", 0.0, 0.0))
    sensors.append(SensorDef("P-L1-HIGH", "thermal-safety", "kPa", 350.0, 5.0, alarm_hi=450, critical_hi=500))
    sensors.append(SensorDef("P-L2-HIGH", "thermal-safety", "kPa", 380.0, 5.0, alarm_hi=480, critical_hi=530))

    # ── Environmental (20 sensors) ──────────────────────────────────
    sensors.append(SensorDef("AMB-T-IT", "environmental", "°C", 24.0, 0.5, alarm_hi=30, critical_hi=35))
    sensors.append(SensorDef("AMB-RH-IT", "environmental", "% RH", 45.0, 3.0, alarm_lo=20, alarm_hi=70))
    sensors.append(SensorDef("AMB-T-TPM", "environmental", "°C", 22.0, 1.0))
    sensors.append(SensorDef("AMB-T-OUTDOOR", "environmental", "°C", 15.0, 3.0))
    sensors.append(SensorDef("AMB-RH-OUTDOOR", "environmental", "% RH", 55.0, 5.0))

    # VESDA smoke detection
    sensors.append(SensorDef("VESDA-01-LEVEL", "environmental", "level", 0.0, 0.0))  # 0=clear, 1=alert, 2=action, 3=fire
    sensors.append(SensorDef("VESDA-02-LEVEL", "environmental", "level", 0.0, 0.0))

    # ── Network (15 sensors) ────────────────────────────────────────
    for sw in range(1, 5):
        sensors.append(SensorDef(f"SW-{sw:02d}-CPU", "network", "%", 15.0, 5.0, alarm_hi=80))
        sensors.append(SensorDef(f"SW-{sw:02d}-TEMP", "network", "°C", 42.0, 2.0, alarm_hi=65))
    sensors.append(SensorDef("WAN-LATENCY", "network", "ms", 2.5, 0.5, alarm_hi=20))
    sensors.append(SensorDef("WAN-PACKET-LOSS", "network", "%", 0.01, 0.005, alarm_hi=1.0, critical_hi=5.0))
    sensors.append(SensorDef("VPN-STATUS", "network", "bool", 1.0, 0.0))

    # ── Security (5 sensors) ────────────────────────────────────────
    sensors.append(SensorDef("DOOR-IT-STATUS", "security", "bool", 0.0, 0.0))  # 0=closed
    sensors.append(SensorDef("DOOR-TPM-STATUS", "security", "bool", 0.0, 0.0))
    sensors.append(SensorDef("DOOR-ELEC-STATUS", "security", "bool", 0.0, 0.0))

    return sensors


def generate_reading(sensor: SensorDef, t: float) -> tuple[float, Optional[str]]:
    """
    Generate a simulated sensor value with optional alarm.
    t = elapsed seconds since start (for slow drift patterns).
    """
    # Base value with noise
    if sensor.noise_std > 0:
        value = sensor.nominal + random.gauss(0, sensor.noise_std)
        # Add slow sinusoidal drift (simulates diurnal temperature swings etc.)
        drift = math.sin(t / 3600 * math.pi / 12) * sensor.noise_std * 2
        value += drift
    else:
        value = sensor.nominal

    # Determine alarm state
    alarm = None
    if random.random() < ALARM_PROBABILITY:
        # Force an excursion
        if sensor.critical_hi is not None:
            value = sensor.critical_hi + random.uniform(1, 10)
            alarm = "P0"
        elif sensor.alarm_hi is not None:
            value = sensor.alarm_hi + random.uniform(1, 5)
            alarm = "P2"
    else:
        # Normal threshold check
        if sensor.critical_hi is not None and value > sensor.critical_hi:
            alarm = "P0"
        elif sensor.critical_lo is not None and value < sensor.critical_lo:
            alarm = "P0"
        elif sensor.alarm_hi is not None and value > sensor.alarm_hi:
            alarm = "P2"
        elif sensor.alarm_lo is not None and value < sensor.alarm_lo:
            alarm = "P2"

    return round(value, 3), alarm


async def run_simulator():
    """Main simulator loop — publishes telemetry for all blocks."""
    sensors = build_sensor_registry()
    print(f"Simulator: {len(sensors)} sensors per block × {BLOCK_COUNT} blocks = {len(sensors) * BLOCK_COUNT} total")
    print(f"Poll interval: {POLL_INTERVAL_MS}ms → {len(sensors) * BLOCK_COUNT * 1000 / POLL_INTERVAL_MS:.0f} msg/sec")

    start_time = time.monotonic()

    async with aiomqtt.Client(
        hostname=MQTT_BROKER,
        port=MQTT_PORT,
        identifier="mcs-simulator",
    ) as client:
        print(f"Connected to {MQTT_BROKER}:{MQTT_PORT}")

        cycle = 0
        while True:
            t = time.monotonic() - start_time
            ts = datetime.now(timezone.utc).isoformat()

            for block_idx in range(1, BLOCK_COUNT + 1):
                block_id = f"block-{block_idx:02d}"
                for sensor in sensors:
                    value, alarm = generate_reading(sensor, t)
                    topic = f"microlink/{SITE_ID}/{block_id}/{sensor.subsystem}/{sensor.tag}"
                    payload = json.dumps({
                        "ts": ts,
                        "v": value,
                        "u": sensor.unit,
                        "q": "GOOD",
                        "alarm": alarm,
                    })
                    await client.publish(topic, payload, qos=0)

            cycle += 1
            if cycle % 10 == 0:
                elapsed = time.monotonic() - start_time
                rate = (cycle * len(sensors) * BLOCK_COUNT) / elapsed
                print(f"Cycle {cycle}: {rate:.0f} msg/sec average, elapsed {elapsed:.0f}s")

            await asyncio.sleep(POLL_INTERVAL_MS / 1000)


if __name__ == "__main__":
    asyncio.run(run_simulator())
