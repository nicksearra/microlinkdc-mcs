"""
MCS Stream B — Seed Data
=========================
Populates the MCS database with realistic data for the AB InBev Baldwinsville
pilot site: 1 site, 1 block (1MW), full equipment hierarchy, ~340 sensors with
alarm thresholds, 3 tenants with API keys, 24 hours of simulated telemetry,
sample alarms, and sample events.

Usage:
  # Ensure schema.sql and aggregates.sql have been applied first
  export DB_HOST=localhost DB_PORT=5432 DB_NAME=mcs DB_USER=mcs DB_PASS=...
  python seed_data.py

  # Output: prints generated API keys for each tenant (store securely)
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import bcrypt
import psycopg2
import psycopg2.extras

# =============================================================================
# CONFIG
# =============================================================================

DB_DSN = (
    f"host={os.getenv('DB_HOST', 'localhost')} "
    f"port={os.getenv('DB_PORT', '5432')} "
    f"dbname={os.getenv('DB_NAME', 'mcs')} "
    f"user={os.getenv('DB_USER', 'mcs')} "
    f"password={os.getenv('DB_PASSWORD', '')}"
)

NOW = datetime.now(timezone.utc)
SEED_START = NOW - timedelta(hours=24)

random.seed(42)  # Reproducible data

# =============================================================================
# IDs (fixed UUIDs for cross-referencing in test_fixtures.json)
# =============================================================================

SITE_ID = "a0000001-0000-0000-0000-000000000001"
BLOCK_ID = "b0000001-0000-0000-0000-000000000001"

TENANT_MICROLINK_ID = "c0000001-0000-0000-0000-000000000001"
TENANT_GPUCLOUD_ID = "c0000002-0000-0000-0000-000000000002"
TENANT_ABINBEV_ID = "c0000003-0000-0000-0000-000000000003"

# =============================================================================
# EQUIPMENT & SENSOR DEFINITIONS
# Realistic point schedule for a 1MW liquid-cooled compute block
# =============================================================================

EQUIPMENT: list[dict[str, Any]] = [
    # --- Electrical ---
    {"tag": "MSB-01", "name": "Main Switchboard", "type": "switchboard", "subsystem": "electrical"},
    {"tag": "UPS-01", "name": "Uninterruptible Power Supply 1", "type": "UPS", "subsystem": "electrical"},
    {"tag": "UPS-02", "name": "Uninterruptible Power Supply 2", "type": "UPS", "subsystem": "electrical"},
    {"tag": "PDU-01", "name": "Power Distribution Unit Row A", "type": "PDU", "subsystem": "electrical"},
    {"tag": "PDU-02", "name": "Power Distribution Unit Row B", "type": "PDU", "subsystem": "electrical"},
    {"tag": "PDU-03", "name": "Power Distribution Unit Row C", "type": "PDU", "subsystem": "electrical"},
    {"tag": "ATS-01", "name": "Automatic Transfer Switch", "type": "ATS", "subsystem": "electrical"},
    {"tag": "GEN-01", "name": "Diesel Generator", "type": "generator", "subsystem": "electrical"},

    # --- Thermal Loop 1: IT Secondary (CDUs) ---
    {"tag": "CDU-01", "name": "Coolant Distribution Unit 1", "type": "CDU", "subsystem": "thermal-l1"},
    {"tag": "CDU-02", "name": "Coolant Distribution Unit 2", "type": "CDU", "subsystem": "thermal-l1"},
    {"tag": "CDU-03", "name": "Coolant Distribution Unit 3", "type": "CDU", "subsystem": "thermal-l1"},
    {"tag": "CDU-04", "name": "Coolant Distribution Unit 4", "type": "CDU", "subsystem": "thermal-l1"},

    # --- Thermal Loop 2: MicroLink Primary (glycol) ---
    {"tag": "PUMP-L2-01", "name": "Loop 2 Circulation Pump 1 (duty)", "type": "pump", "subsystem": "thermal-l2"},
    {"tag": "PUMP-L2-02", "name": "Loop 2 Circulation Pump 2 (standby)", "type": "pump", "subsystem": "thermal-l2"},
    {"tag": "EXP-L2", "name": "Loop 2 Expansion Vessel", "type": "expansion_vessel", "subsystem": "thermal-l2"},
    {"tag": "CHEM-L2", "name": "Loop 2 Chemistry Pot", "type": "chemistry", "subsystem": "thermal-l2"},
    {"tag": "FILT-L2", "name": "Loop 2 Strainer/Filter", "type": "filter", "subsystem": "thermal-l2"},

    # --- Thermal Loop 3: Host Process (plate HX) ---
    {"tag": "HX-01", "name": "Plate Heat Exchanger (export)", "type": "heat_exchanger", "subsystem": "thermal-hx"},
    {"tag": "PUMP-L3-01", "name": "Loop 3 Circulation Pump", "type": "pump", "subsystem": "thermal-l3"},
    {"tag": "BIL-METER", "name": "Billing Boundary Meter Assembly", "type": "meter", "subsystem": "thermal-l3"},

    # --- Thermal Reject ---
    {"tag": "DC-01", "name": "Dry Cooler 1", "type": "dry_cooler", "subsystem": "thermal-reject"},
    {"tag": "DC-02", "name": "Dry Cooler 2", "type": "dry_cooler", "subsystem": "thermal-reject"},
    {"tag": "DC-VFD-01", "name": "Dry Cooler 1 Fan VFD", "type": "VFD", "subsystem": "thermal-reject"},
    {"tag": "DC-VFD-02", "name": "Dry Cooler 2 Fan VFD", "type": "VFD", "subsystem": "thermal-reject"},

    # --- Environmental ---
    {"tag": "ENV-INT", "name": "Internal Environment Sensors", "type": "environmental", "subsystem": "environmental"},
    {"tag": "ENV-EXT", "name": "External Environment Sensors", "type": "environmental", "subsystem": "environmental"},
    {"tag": "LEAK-SYS", "name": "Leak Detection System", "type": "leak_detection", "subsystem": "environmental"},

    # --- Network ---
    {"tag": "SW-CORE", "name": "Core Network Switch", "type": "switch", "subsystem": "network"},
    {"tag": "SW-MGMT", "name": "Management Network Switch", "type": "switch", "subsystem": "network"},

    # --- Security ---
    {"tag": "ACCESS-01", "name": "Access Control Panel", "type": "access_control", "subsystem": "security"},
]

# Sensor definitions: (tag, description, unit, data_type, range_min, range_max, subsystem, is_billing, thresholds, parent_equip_tag)
# Thresholds: {"hi_hi": X, "hi": X, "lo": X, "lo_lo": X, "deadband": X}

SENSORS: list[dict[str, Any]] = [
    # === ELECTRICAL ===
    # Main switchboard
    {"tag": "PM-IT", "desc": "IT Load Power", "unit": "kW", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 1200], "billing": True, "thresholds": {"hi_hi": 1100, "hi": 1000, "lo": 50, "deadband": 20},
     "nominal": 850, "noise": 15},
    {"tag": "PM-TOT", "desc": "Total Facility Power", "unit": "kW", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 1500], "billing": True, "thresholds": {"hi_hi": 1350, "hi": 1200, "deadband": 25},
     "nominal": 920, "noise": 18},
    {"tag": "PM-COOL", "desc": "Cooling System Power", "unit": "kW", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 300], "billing": True, "thresholds": {"hi": 250, "deadband": 10},
     "nominal": 70, "noise": 8},
    {"tag": "EM-VOLT-A", "desc": "Phase A Voltage", "unit": "V", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 520], "thresholds": {"hi_hi": 510, "hi": 500, "lo": 440, "lo_lo": 430, "deadband": 3},
     "nominal": 480, "noise": 2},
    {"tag": "EM-VOLT-B", "desc": "Phase B Voltage", "unit": "V", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 520], "thresholds": {"hi_hi": 510, "hi": 500, "lo": 440, "lo_lo": 430, "deadband": 3},
     "nominal": 480, "noise": 2},
    {"tag": "EM-VOLT-C", "desc": "Phase C Voltage", "unit": "V", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0, 520], "thresholds": {"hi_hi": 510, "hi": 500, "lo": 440, "lo_lo": 430, "deadband": 3},
     "nominal": 480, "noise": 2},
    {"tag": "EM-FREQ", "desc": "Grid Frequency", "unit": "Hz", "subsystem": "electrical", "equip": "MSB-01",
     "range": [58, 62], "thresholds": {"hi_hi": 61.5, "hi": 60.5, "lo": 59.5, "lo_lo": 58.5, "deadband": 0.1},
     "nominal": 60.0, "noise": 0.02},
    {"tag": "EM-PF", "desc": "Power Factor", "unit": "", "subsystem": "electrical", "equip": "MSB-01",
     "range": [0.8, 1.0], "thresholds": {"lo": 0.9, "lo_lo": 0.85, "deadband": 0.02},
     "nominal": 0.97, "noise": 0.01},

    # UPS
    {"tag": "UPS-01-LOAD", "desc": "UPS 1 Load", "unit": "%", "subsystem": "electrical", "equip": "UPS-01",
     "range": [0, 100], "thresholds": {"hi_hi": 95, "hi": 85, "deadband": 3},
     "nominal": 62, "noise": 3},
    {"tag": "UPS-01-BAT", "desc": "UPS 1 Battery Level", "unit": "%", "subsystem": "electrical", "equip": "UPS-01",
     "range": [0, 100], "thresholds": {"lo": 50, "lo_lo": 20, "deadband": 5},
     "nominal": 100, "noise": 0},
    {"tag": "UPS-01-TEMP", "desc": "UPS 1 Internal Temp", "unit": "°C", "subsystem": "electrical", "equip": "UPS-01",
     "range": [10, 60], "thresholds": {"hi_hi": 50, "hi": 40, "deadband": 2},
     "nominal": 28, "noise": 1},
    {"tag": "UPS-02-LOAD", "desc": "UPS 2 Load", "unit": "%", "subsystem": "electrical", "equip": "UPS-02",
     "range": [0, 100], "thresholds": {"hi_hi": 95, "hi": 85, "deadband": 3},
     "nominal": 62, "noise": 3},
    {"tag": "UPS-02-BAT", "desc": "UPS 2 Battery Level", "unit": "%", "subsystem": "electrical", "equip": "UPS-02",
     "range": [0, 100], "thresholds": {"lo": 50, "lo_lo": 20, "deadband": 5},
     "nominal": 100, "noise": 0},

    # PDU
    {"tag": "PDU-01-kW", "desc": "PDU Row A Power", "unit": "kW", "subsystem": "electrical", "equip": "PDU-01",
     "range": [0, 400], "billing": True, "thresholds": {"hi": 380, "deadband": 10},
     "nominal": 285, "noise": 8},
    {"tag": "PDU-02-kW", "desc": "PDU Row B Power", "unit": "kW", "subsystem": "electrical", "equip": "PDU-02",
     "range": [0, 400], "billing": True, "thresholds": {"hi": 380, "deadband": 10},
     "nominal": 290, "noise": 8},
    {"tag": "PDU-03-kW", "desc": "PDU Row C Power", "unit": "kW", "subsystem": "electrical", "equip": "PDU-03",
     "range": [0, 400], "billing": True, "thresholds": {"hi": 380, "deadband": 10},
     "nominal": 275, "noise": 8},

    # === THERMAL LOOP 1 (CDUs) ===
    {"tag": "TT-L1s01", "desc": "CDU 1 Supply Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-01",
     "range": [20, 70], "thresholds": {"hi_hi": 55, "hi": 50, "lo": 25, "deadband": 2},
     "nominal": 35, "noise": 0.5},
    {"tag": "TT-L1r01", "desc": "CDU 1 Return Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-01",
     "range": [20, 70], "thresholds": {"hi_hi": 60, "hi": 52, "deadband": 2},
     "nominal": 45, "noise": 0.5},
    {"tag": "FT-L1-01", "desc": "CDU 1 Flow Rate", "unit": "L/min", "subsystem": "thermal-l1", "equip": "CDU-01",
     "range": [0, 500], "thresholds": {"lo": 100, "lo_lo": 50, "deadband": 10},
     "nominal": 320, "noise": 5},
    {"tag": "TT-L1s02", "desc": "CDU 2 Supply Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-02",
     "range": [20, 70], "thresholds": {"hi_hi": 55, "hi": 50, "lo": 25, "deadband": 2},
     "nominal": 35, "noise": 0.5},
    {"tag": "TT-L1r02", "desc": "CDU 2 Return Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-02",
     "range": [20, 70], "thresholds": {"hi_hi": 60, "hi": 52, "deadband": 2},
     "nominal": 45, "noise": 0.5},
    {"tag": "FT-L1-02", "desc": "CDU 2 Flow Rate", "unit": "L/min", "subsystem": "thermal-l1", "equip": "CDU-02",
     "range": [0, 500], "thresholds": {"lo": 100, "lo_lo": 50, "deadband": 10},
     "nominal": 315, "noise": 5},
    {"tag": "TT-L1s03", "desc": "CDU 3 Supply Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-03",
     "range": [20, 70], "thresholds": {"hi_hi": 55, "hi": 50, "lo": 25, "deadband": 2},
     "nominal": 35, "noise": 0.5},
    {"tag": "TT-L1r03", "desc": "CDU 3 Return Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-03",
     "range": [20, 70], "thresholds": {"hi_hi": 60, "hi": 52, "deadband": 2},
     "nominal": 45, "noise": 0.5},
    {"tag": "FT-L1-03", "desc": "CDU 3 Flow Rate", "unit": "L/min", "subsystem": "thermal-l1", "equip": "CDU-03",
     "range": [0, 500], "thresholds": {"lo": 100, "lo_lo": 50, "deadband": 10},
     "nominal": 310, "noise": 5},
    {"tag": "TT-L1s04", "desc": "CDU 4 Supply Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-04",
     "range": [20, 70], "thresholds": {"hi_hi": 55, "hi": 50, "lo": 25, "deadband": 2},
     "nominal": 35, "noise": 0.5},
    {"tag": "TT-L1r04", "desc": "CDU 4 Return Temp", "unit": "°C", "subsystem": "thermal-l1", "equip": "CDU-04",
     "range": [20, 70], "thresholds": {"hi_hi": 60, "hi": 52, "deadband": 2},
     "nominal": 45, "noise": 0.5},
    {"tag": "FT-L1-04", "desc": "CDU 4 Flow Rate", "unit": "L/min", "subsystem": "thermal-l1", "equip": "CDU-04",
     "range": [0, 500], "thresholds": {"lo": 100, "lo_lo": 50, "deadband": 10},
     "nominal": 305, "noise": 5},

    # === THERMAL LOOP 2 (Glycol primary) ===
    {"tag": "TT-L2s", "desc": "Loop 2 Supply Temp", "unit": "°C", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [20, 70], "thresholds": {"hi_hi": 55, "hi": 50, "lo": 25, "lo_lo": 15, "deadband": 2},
     "nominal": 45, "noise": 0.4},
    {"tag": "TT-L2r", "desc": "Loop 2 Return Temp", "unit": "°C", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [20, 60], "thresholds": {"hi_hi": 50, "hi": 42, "deadband": 2},
     "nominal": 35, "noise": 0.4},
    {"tag": "FT-L2", "desc": "Loop 2 Flow Rate", "unit": "L/min", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [0, 2000], "thresholds": {"lo": 500, "lo_lo": 200, "deadband": 30},
     "nominal": 1200, "noise": 15},
    {"tag": "PT-L2s", "desc": "Loop 2 Supply Pressure", "unit": "bar", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [0, 10], "thresholds": {"hi": 6, "lo": 1.5, "lo_lo": 1.0, "deadband": 0.2},
     "nominal": 3.5, "noise": 0.1},
    {"tag": "PT-L2r", "desc": "Loop 2 Return Pressure", "unit": "bar", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [0, 10], "thresholds": {"hi": 5, "lo": 0.8, "lo_lo": 0.5, "deadband": 0.1},
     "nominal": 2.2, "noise": 0.08},
    {"tag": "VIB-L2-01", "desc": "Pump L2-01 Vibration", "unit": "mm/s", "subsystem": "thermal-l2", "equip": "PUMP-L2-01",
     "range": [0, 20], "thresholds": {"hi_hi": 11.2, "hi": 7.1, "deadband": 0.5},
     "nominal": 2.8, "noise": 0.3},
    {"tag": "VIB-L2-02", "desc": "Pump L2-02 Vibration", "unit": "mm/s", "subsystem": "thermal-l2", "equip": "PUMP-L2-02",
     "range": [0, 20], "thresholds": {"hi_hi": 11.2, "hi": 7.1, "deadband": 0.5},
     "nominal": 2.5, "noise": 0.3},
    {"tag": "PT-EXP", "desc": "Expansion Vessel Pressure", "unit": "bar", "subsystem": "thermal-l2", "equip": "EXP-L2",
     "range": [0, 6], "thresholds": {"hi": 4.5, "lo": 1.0, "lo_lo": 0.5, "deadband": 0.2},
     "nominal": 2.5, "noise": 0.05},
    {"tag": "LT-EXP", "desc": "Expansion Vessel Level", "unit": "%", "subsystem": "thermal-l2", "equip": "EXP-L2",
     "range": [0, 100], "thresholds": {"hi": 90, "lo": 30, "lo_lo": 15, "deadband": 3},
     "nominal": 65, "noise": 1},
    {"tag": "CT-pH", "desc": "Glycol pH", "unit": "", "subsystem": "thermal-l2", "equip": "CHEM-L2",
     "range": [6, 11], "thresholds": {"hi": 10.0, "lo": 7.5, "lo_lo": 7.0, "deadband": 0.2},
     "nominal": 8.5, "noise": 0.1},
    {"tag": "CT-COND", "desc": "Glycol Conductivity", "unit": "µS/cm", "subsystem": "thermal-l2", "equip": "CHEM-L2",
     "range": [0, 5000], "thresholds": {"hi": 3000, "hi_hi": 4000, "deadband": 100},
     "nominal": 1200, "noise": 50},
    {"tag": "PT-FILT", "desc": "Filter Differential Pressure", "unit": "bar", "subsystem": "thermal-l2", "equip": "FILT-L2",
     "range": [0, 2], "thresholds": {"hi": 1.2, "hi_hi": 1.5, "deadband": 0.05},
     "nominal": 0.3, "noise": 0.02},

    # === THERMAL HX / LOOP 3 ===
    {"tag": "TT-HX-h-in", "desc": "HX Hot Side Inlet", "unit": "°C", "subsystem": "thermal-hx", "equip": "HX-01",
     "range": [20, 70], "nominal": 45, "noise": 0.4},
    {"tag": "TT-HX-h-out", "desc": "HX Hot Side Outlet", "unit": "°C", "subsystem": "thermal-hx", "equip": "HX-01",
     "range": [20, 60], "nominal": 35, "noise": 0.4},
    {"tag": "TT-HX-c-in", "desc": "HX Cold Side Inlet (brewery return)", "unit": "°C", "subsystem": "thermal-hx", "equip": "HX-01",
     "range": [5, 50], "nominal": 25, "noise": 0.5},
    {"tag": "TT-HX-c-out", "desc": "HX Cold Side Outlet (to brewery)", "unit": "°C", "subsystem": "thermal-hx", "equip": "HX-01",
     "range": [20, 65], "thresholds": {"hi_hi": 65, "hi": 60, "deadband": 2},
     "nominal": 40, "noise": 0.5},
    {"tag": "TT-L3s", "desc": "Loop 3 Supply Temp (to brewery)", "unit": "°C", "subsystem": "thermal-l3", "equip": "PUMP-L3-01",
     "range": [20, 65], "billing": True, "thresholds": {"hi_hi": 65, "hi": 58, "deadband": 2},
     "nominal": 40, "noise": 0.4},
    {"tag": "TT-L3r", "desc": "Loop 3 Return Temp (from brewery)", "unit": "°C", "subsystem": "thermal-l3", "equip": "PUMP-L3-01",
     "range": [5, 50], "billing": True, "nominal": 25, "noise": 0.5},
    {"tag": "FT-BIL", "desc": "Billing Boundary Flow Meter", "unit": "L/min", "subsystem": "thermal-l3", "equip": "BIL-METER",
     "range": [0, 1500], "billing": True, "thresholds": {"lo": 100, "lo_lo": 50, "deadband": 15},
     "nominal": 800, "noise": 10},
    {"tag": "PM-BIL", "desc": "Billing Boundary Energy Meter", "unit": "kWt", "subsystem": "thermal-l3", "equip": "BIL-METER",
     "range": [0, 1000], "billing": True, "nominal": 695, "noise": 12},

    # === THERMAL REJECT (dry coolers) ===
    {"tag": "TT-DC1-in", "desc": "Dry Cooler 1 Inlet", "unit": "°C", "subsystem": "thermal-reject", "equip": "DC-01",
     "range": [10, 70], "nominal": 42, "noise": 0.5},
    {"tag": "TT-DC1-out", "desc": "Dry Cooler 1 Outlet", "unit": "°C", "subsystem": "thermal-reject", "equip": "DC-01",
     "range": [5, 60], "nominal": 30, "noise": 0.5},
    {"tag": "DC-VFD-01-SPD", "desc": "Dry Cooler 1 Fan Speed", "unit": "%", "subsystem": "thermal-reject", "equip": "DC-VFD-01",
     "range": [0, 100], "nominal": 45, "noise": 3},
    {"tag": "TT-DC2-in", "desc": "Dry Cooler 2 Inlet", "unit": "°C", "subsystem": "thermal-reject", "equip": "DC-02",
     "range": [10, 70], "nominal": 42, "noise": 0.5},
    {"tag": "TT-DC2-out", "desc": "Dry Cooler 2 Outlet", "unit": "°C", "subsystem": "thermal-reject", "equip": "DC-02",
     "range": [5, 60], "nominal": 30, "noise": 0.5},
    {"tag": "DC-VFD-02-SPD", "desc": "Dry Cooler 2 Fan Speed", "unit": "%", "subsystem": "thermal-reject", "equip": "DC-VFD-02",
     "range": [0, 100], "nominal": 45, "noise": 3},

    # === ENVIRONMENTAL ===
    {"tag": "TT-AMB-INT", "desc": "Internal Ambient Temp", "unit": "°C", "subsystem": "environmental", "equip": "ENV-INT",
     "range": [10, 50], "thresholds": {"hi_hi": 40, "hi": 35, "lo": 15, "deadband": 1},
     "nominal": 24, "noise": 0.5},
    {"tag": "RH-INT", "desc": "Internal Relative Humidity", "unit": "%", "subsystem": "environmental", "equip": "ENV-INT",
     "range": [0, 100], "thresholds": {"hi": 80, "hi_hi": 90, "lo": 20, "deadband": 3},
     "nominal": 45, "noise": 2},
    {"tag": "TT-AMB-EXT", "desc": "External Ambient Temp", "unit": "°C", "subsystem": "environmental", "equip": "ENV-EXT",
     "range": [-30, 50], "nominal": 2, "noise": 0.3},  # Feb in Baldwinsville, NY
    {"tag": "RH-EXT", "desc": "External Relative Humidity", "unit": "%", "subsystem": "environmental", "equip": "ENV-EXT",
     "range": [0, 100], "nominal": 72, "noise": 3},
    {"tag": "LD-01a", "desc": "Leak Detector Zone 1 Rope", "unit": "", "subsystem": "environmental", "equip": "LEAK-SYS",
     "range": [0, 1], "data_type": "bool", "thresholds": {"hi_hi": 1, "deadband": 0},
     "nominal": 0, "noise": 0},
    {"tag": "LD-01b", "desc": "Leak Detector Zone 2 Rope", "unit": "", "subsystem": "environmental", "equip": "LEAK-SYS",
     "range": [0, 1], "data_type": "bool", "thresholds": {"hi_hi": 1, "deadband": 0},
     "nominal": 0, "noise": 0},
    {"tag": "LD-02a", "desc": "Leak Detector HX Room", "unit": "", "subsystem": "environmental", "equip": "LEAK-SYS",
     "range": [0, 1], "data_type": "bool", "thresholds": {"hi_hi": 1, "deadband": 0},
     "nominal": 0, "noise": 0},

    # === NETWORK ===
    {"tag": "NET-LATENCY", "desc": "Core Switch Latency", "unit": "ms", "subsystem": "network", "equip": "SW-CORE",
     "range": [0, 100], "thresholds": {"hi": 10, "hi_hi": 50, "deadband": 1},
     "nominal": 0.3, "noise": 0.05},
    {"tag": "NET-UTIL", "desc": "Core Switch Port Utilisation", "unit": "%", "subsystem": "network", "equip": "SW-CORE",
     "range": [0, 100], "thresholds": {"hi": 85, "hi_hi": 95, "deadband": 3},
     "nominal": 42, "noise": 5},

    # === SECURITY ===
    {"tag": "DOOR-MAIN", "desc": "Main Door Status", "unit": "", "subsystem": "security", "equip": "ACCESS-01",
     "range": [0, 1], "data_type": "bool", "nominal": 0, "noise": 0},
]


# =============================================================================
# TELEMETRY SIMULATION
# =============================================================================

def simulate_value(
    sensor: dict,
    t: datetime,
    hour_offset: float,
) -> float:
    """
    Generate a realistic sensor value with:
    - Diurnal pattern (load ramps up during day)
    - Gaussian noise
    - Thermal correlation (ambient temp affects cooling)
    """
    nominal = sensor["nominal"]
    noise = sensor["noise"]

    # Diurnal factor: slight load increase during 08:00-20:00
    hour = (SEED_START + timedelta(hours=hour_offset)).hour
    diurnal = 1.0 + 0.05 * math.sin((hour - 6) * math.pi / 12) if 6 <= hour <= 18 else 1.0

    # Ambient temp correlation for outdoor sensors
    if sensor["tag"].startswith("TT-AMB-EXT"):
        # Baldwinsville Feb: avg ~-2°C, daytime peak ~3°C
        return -2 + 5 * math.sin((hour - 6) * math.pi / 12) + random.gauss(0, noise)

    # Outdoor humidity: higher at night
    if sensor["tag"] == "RH-EXT":
        return 75 - 10 * math.sin((hour - 6) * math.pi / 12) + random.gauss(0, noise)

    # Boolean sensors: always 0 (normal)
    if sensor.get("data_type") == "bool":
        return 0.0

    value = nominal * diurnal + random.gauss(0, noise)

    # Clamp to range
    rng = sensor.get("range", [0, 1000])
    return max(rng[0], min(rng[1], round(value, 3)))


# =============================================================================
# MAIN SEED FUNCTION
# =============================================================================

def seed(conn: psycopg2.extensions.connection) -> dict[str, str]:
    """
    Populate the database. Returns dict of tenant_name → raw API key.
    """
    cur = conn.cursor()
    api_keys: dict[str, str] = {}

    print("=" * 60)
    print("MCS SEED DATA — Baldwinsville Pilot Site")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. SITE
    # ------------------------------------------------------------------
    print("\n[1/7] Creating site...")
    cur.execute("""
        INSERT INTO sites (id, name, region, address, latitude, longitude,
                          utility_provider, config_json, status, commissioned_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'operational', %s)
        ON CONFLICT (name) DO NOTHING
    """, (
        SITE_ID,
        "AB InBev Baldwinsville",
        "us-east",
        "7792 Route 31, Baldwinsville, NY 13027",
        43.1587, -76.3327,
        "National Grid",
        json.dumps({
            "utility_voltage_kv": 13.8,
            "grid_connection": "radial",
            "host": "Anheuser-Busch InBev",
            "facility_type": "brewery",
            "facility_sqft": 1500000,
            "facility_acres": 370,
            "operation_schedule": "24/7/365",
        }),
        NOW - timedelta(days=30),
    ))
    print(f"  ✓ Site: AB InBev Baldwinsville ({SITE_ID})")

    # ------------------------------------------------------------------
    # 2. BLOCK
    # ------------------------------------------------------------------
    print("\n[2/7] Creating block...")
    cur.execute("""
        INSERT INTO blocks (id, site_id, name, capacity_mw, current_mode,
                           config_json, status, commissioned_at)
        VALUES (%s, %s, %s, %s, 'EXPORT', %s, 'operational', %s)
        ON CONFLICT (site_id, name) DO NOTHING
    """, (
        BLOCK_ID, SITE_ID,
        "BLK-BV-01", 1.0,
        json.dumps({
            "rack_count": 18,
            "rack_density_kw": 50,
            "cooling_design_supply_c": 35,
            "cooling_design_return_c": 45,
            "heat_export_target_kwt": 700,
            "heat_export_temp_c": 55,
            "glycol_concentration_pct": 30,
            "dry_cooler_capacity_kwt": 1200,
        }),
        NOW - timedelta(days=30),
    ))
    print(f"  ✓ Block: BLK-BV-01 (1MW, EXPORT mode) ({BLOCK_ID})")

    # ------------------------------------------------------------------
    # 3. EQUIPMENT
    # ------------------------------------------------------------------
    print("\n[3/7] Creating equipment...")
    equip_id_map: dict[str, str] = {}
    for eq in EQUIPMENT:
        eid = str(uuid4())
        equip_id_map[eq["tag"]] = eid
        cur.execute("""
            INSERT INTO equipment (id, block_id, tag, name, type, subsystem)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (block_id, tag) DO NOTHING
        """, (eid, BLOCK_ID, eq["tag"], eq["name"], eq["type"], eq["subsystem"]))
    print(f"  ✓ {len(EQUIPMENT)} equipment items")

    # ------------------------------------------------------------------
    # 4. SENSORS
    # ------------------------------------------------------------------
    print("\n[4/7] Creating sensors...")
    sensor_id_map: dict[str, int] = {}
    sensor_meta: dict[int, dict] = {}  # sensor_id → sensor def (for telemetry sim)

    for s in SENSORS:
        equip_tag = s["equip"]
        equip_id = equip_id_map.get(equip_tag)
        if not equip_id:
            print(f"  ⚠ Equipment not found for sensor {s['tag']} (equip={equip_tag})")
            continue

        thresholds = s.get("thresholds", {})
        thresholds["delay_samples"] = 3
        if "deadband" not in thresholds:
            thresholds["deadband"] = 2

        # Add suppression hints for pump sensors
        if "VIB" in s["tag"]:
            # Pump vibration suppresses downstream flow/pressure for 30s
            thresholds["suppresses"] = ["FT-L2", "PT-L2s", "PT-L2r"]

        cur.execute("""
            INSERT INTO sensors (equipment_id, block_id, tag, description, subsystem,
                                unit, data_type, range_min, range_max, poll_rate_ms,
                                alarm_thresholds, protocol, is_billing_grade)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (block_id, tag) DO UPDATE SET tag = EXCLUDED.tag
            RETURNING id
        """, (
            equip_id, BLOCK_ID, s["tag"], s["desc"], s["subsystem"],
            s["unit"], s.get("data_type", "float"),
            s.get("range", [0, 1000])[0], s.get("range", [0, 1000])[1],
            5000,  # 5s poll
            json.dumps(thresholds), "modbus-tcp",
            s.get("billing", False),
        ))
        sid = cur.fetchone()[0]
        sensor_id_map[s["tag"]] = sid
        sensor_meta[sid] = s

    print(f"  ✓ {len(sensor_id_map)} sensors with thresholds")

    # ------------------------------------------------------------------
    # 5. TENANTS + API KEYS
    # ------------------------------------------------------------------
    print("\n[5/7] Creating tenants...")

    tenants = [
        {
            "id": TENANT_MICROLINK_ID,
            "name": "MicroLink Internal",
            "tier": "internal",
            "roles": ["admin", "operator", "viewer"],
            "rate_limit": 1000,
            "email": "ops@microlink.energy",
        },
        {
            "id": TENANT_GPUCLOUD_ID,
            "name": "GPU Cloud Co",
            "tier": "customer",
            "roles": ["customer", "viewer"],
            "rate_limit": 100,
            "email": "noc@gpucloud.example.com",
        },
        {
            "id": TENANT_ABINBEV_ID,
            "name": "AB InBev Baldwinsville",
            "tier": "host",
            "roles": ["host", "viewer"],
            "rate_limit": 100,
            "email": "utilities@ab-inbev.example.com",
        },
    ]

    for t in tenants:
        raw_key = f"ml_{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=40))}"
        hashed = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()
        api_keys[t["name"]] = raw_key

        cur.execute("""
            INSERT INTO tenants (id, name, tier, contact_email, api_key_hash, roles, rate_limit_rpm)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
        """, (t["id"], t["name"], t["tier"], t["email"], hashed,
              t["roles"], t["rate_limit"]))

    # Tenant → Block access
    # MicroLink: admin on all blocks
    cur.execute("""
        INSERT INTO tenant_blocks (tenant_id, block_id, access_level, rack_assignments)
        VALUES (%s, %s, 'admin', '[]')
        ON CONFLICT (tenant_id, block_id) DO NOTHING
    """, (TENANT_MICROLINK_ID, BLOCK_ID))

    # GPU Cloud Co: read access, racks R01-R06
    racks = [{"rack": f"R{i:02d}", "kw": 50} for i in range(1, 7)]
    cur.execute("""
        INSERT INTO tenant_blocks (tenant_id, block_id, access_level, rack_assignments)
        VALUES (%s, %s, 'read', %s)
        ON CONFLICT (tenant_id, block_id) DO NOTHING
    """, (TENANT_GPUCLOUD_ID, BLOCK_ID, json.dumps(racks)))

    # AB InBev: read access, thermal data scopes only
    cur.execute("""
        INSERT INTO tenant_blocks (tenant_id, block_id, access_level, data_scopes)
        VALUES (%s, %s, 'read', %s)
        ON CONFLICT (tenant_id, block_id) DO NOTHING
    """, (TENANT_ABINBEV_ID, BLOCK_ID,
          ["thermal-l3", "thermal-hx", "thermal-reject", "environmental"]))

    print(f"  ✓ 3 tenants with API keys and block access")

    # ------------------------------------------------------------------
    # 6. TELEMETRY (24 hours of simulated data)
    # ------------------------------------------------------------------
    print("\n[6/7] Generating 24h telemetry (this takes a moment)...")

    # Generate at 30-second intervals for 24 hours = 2880 points per sensor
    # With ~65 sensors = ~187k rows
    import io
    buf = io.StringIO()
    row_count = 0
    interval_s = 30  # 30s intervals (reduced from 5s for seed perf)
    total_steps = int(24 * 3600 / interval_s)

    for step in range(total_steps):
        hour_offset = step * interval_s / 3600.0
        ts = SEED_START + timedelta(seconds=step * interval_s)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S+00")

        for sid, sensor_def in sensor_meta.items():
            value = simulate_value(sensor_def, ts, hour_offset)
            quality = 0  # GOOD
            buf.write(f"{ts_str}\t{sid}\t{value}\t{quality}\n")
            row_count += 1

    buf.seek(0)
    cur.copy_expert(
        "COPY telemetry (time, sensor_id, value, quality) FROM STDIN WITH (FORMAT text)",
        buf,
    )
    print(f"  ✓ {row_count:,} telemetry rows ({total_steps} timestamps × {len(sensor_meta)} sensors)")

    # ------------------------------------------------------------------
    # 7. SAMPLE ALARMS & EVENTS
    # ------------------------------------------------------------------
    print("\n[7/7] Creating sample alarms and events...")

    # 2 ACTIVE alarms
    alarm_defs = [
        {"sensor_tag": "VIB-L2-01", "priority": "P2", "state": "ACTIVE",
         "value": 8.2, "threshold": 7.1, "msg": "Pump L2-01 vibration high",
         "raised_ago_h": 2.5},
        {"sensor_tag": "TT-AMB-INT", "priority": "P2", "state": "ACTIVE",
         "value": 36.1, "threshold": 35, "msg": "Internal ambient temp high",
         "raised_ago_h": 0.5},
        # 3 ACKNOWLEDGED
        {"sensor_tag": "PT-FILT", "priority": "P3", "state": "ACKNOWLEDGED",
         "value": 1.3, "threshold": 1.2, "msg": "Filter ΔP high — schedule replacement",
         "raised_ago_h": 12, "acked_ago_h": 11},
        {"sensor_tag": "DC-VFD-01-SPD", "priority": "P3", "state": "ACKNOWLEDGED",
         "value": 92, "threshold": 85, "msg": "Dry cooler fan running high — check ambient",
         "raised_ago_h": 6, "acked_ago_h": 5.5},
        {"sensor_tag": "NET-LATENCY", "priority": "P2", "state": "ACKNOWLEDGED",
         "value": 12.3, "threshold": 10, "msg": "Core switch latency elevated",
         "raised_ago_h": 3, "acked_ago_h": 2.8},
        # 5 CLEARED
        {"sensor_tag": "UPS-01-LOAD", "priority": "P2", "state": "CLEARED",
         "value": 87, "threshold": 85, "msg": "UPS 1 load high (transient)",
         "raised_ago_h": 18, "cleared_ago_h": 17.5},
        {"sensor_tag": "EM-FREQ", "priority": "P1", "state": "CLEARED",
         "value": 59.4, "threshold": 59.5, "msg": "Grid frequency dip",
         "raised_ago_h": 14, "cleared_ago_h": 13.9},
        {"sensor_tag": "CT-COND", "priority": "P3", "state": "CLEARED",
         "value": 3100, "threshold": 3000, "msg": "Glycol conductivity rising",
         "raised_ago_h": 20, "cleared_ago_h": 16},
        {"sensor_tag": "FT-L1-02", "priority": "P2", "state": "CLEARED",
         "value": 95, "threshold": 100, "msg": "CDU 2 flow low (pump switchover)",
         "raised_ago_h": 22, "cleared_ago_h": 21.8},
        {"sensor_tag": "TT-L2s", "priority": "P2", "state": "CLEARED",
         "value": 51, "threshold": 50, "msg": "Loop 2 supply temp high (cooling ramp)",
         "raised_ago_h": 10, "cleared_ago_h": 9.5},
    ]

    for ad in alarm_defs:
        sensor_id = sensor_id_map.get(ad["sensor_tag"])
        if not sensor_id:
            continue

        alarm_id = str(uuid4())
        raised_at = NOW - timedelta(hours=ad["raised_ago_h"])
        acked_at = NOW - timedelta(hours=ad["acked_ago_h"]) if "acked_ago_h" in ad else None
        cleared_at = NOW - timedelta(hours=ad["cleared_ago_h"]) if "cleared_ago_h" in ad else None

        cur.execute("""
            INSERT INTO alarms (id, sensor_id, block_id, priority, state,
                               condition_type, value_at_trigger, threshold, message,
                               raised_at, acknowledged_at, acknowledged_by, cleared_at)
            VALUES (%s, %s, %s, %s, %s, 'threshold', %s, %s, %s, %s, %s, %s, %s)
        """, (
            alarm_id, sensor_id, BLOCK_ID,
            ad["priority"], ad["state"],
            ad["value"], ad["threshold"], ad["msg"],
            raised_at,
            acked_at, "jsmith" if acked_at else None,
            cleared_at,
        ))

        # Alarm history: initial raise
        cur.execute("""
            INSERT INTO alarm_history (alarm_id, state_from, state_to, changed_by, changed_at, notes)
            VALUES (%s, NULL, 'ACTIVE', 'alarm_engine', %s, %s)
        """, (alarm_id, raised_at, ad["msg"]))

        if acked_at:
            cur.execute("""
                INSERT INTO alarm_history (alarm_id, state_from, state_to, changed_by, changed_at, notes)
                VALUES (%s, 'ACTIVE', 'ACKNOWLEDGED', 'jsmith', %s, 'Investigating')
            """, (alarm_id, acked_at))

        if cleared_at:
            prev = "ACKNOWLEDGED" if acked_at else "ACTIVE"
            cur.execute("""
                INSERT INTO alarm_history (alarm_id, state_from, state_to, changed_by, changed_at, notes)
                VALUES (%s, %s, 'CLEARED', 'alarm_engine', %s, 'Value returned to normal')
            """, (alarm_id, prev, cleared_at))

    print(f"  ✓ {len(alarm_defs)} alarms (2 active, 3 acknowledged, 5 cleared)")

    # Sample events
    events = [
        {"type": "commissioning", "source": "operator:nsearra", "severity": "info",
         "payload": {"action": "block_energised", "block": "BLK-BV-01"},
         "ago_h": 720},  # 30 days ago
        {"type": "mode_change", "source": "edge_gateway", "severity": "info",
         "payload": {"from": "REJECT", "to": "EXPORT", "reason": "brewery_demand"},
         "ago_h": 718},
        {"type": "config_change", "source": "operator:nsearra", "severity": "info",
         "payload": {"field": "heat_export_target_kwt", "old": 600, "new": 700},
         "ago_h": 336},  # 14 days ago
        {"type": "mode_change", "source": "edge_gateway", "severity": "warning",
         "payload": {"from": "EXPORT", "to": "REJECT", "reason": "host_maintenance_request"},
         "ago_h": 168},  # 7 days ago
        {"type": "mode_change", "source": "edge_gateway", "severity": "info",
         "payload": {"from": "REJECT", "to": "EXPORT", "reason": "host_maintenance_complete"},
         "ago_h": 164},
        {"type": "maintenance", "source": "operator:dthomas", "severity": "info",
         "payload": {"action": "filter_replacement", "equipment": "FILT-L2", "notes": "Scheduled quarterly maintenance"},
         "ago_h": 72},  # 3 days ago
        {"type": "mode_change", "source": "edge_gateway", "severity": "info",
         "payload": {"from": "EXPORT", "to": "MIXED", "reason": "approach_dt_low"},
         "ago_h": 8},
        {"type": "mode_change", "source": "edge_gateway", "severity": "info",
         "payload": {"from": "MIXED", "to": "EXPORT", "reason": "approach_dt_restored"},
         "ago_h": 6},
    ]

    for ev in events:
        cur.execute("""
            INSERT INTO events (block_id, event_type, source, severity, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            BLOCK_ID, ev["type"], ev["source"], ev["severity"],
            json.dumps(ev["payload"]),
            NOW - timedelta(hours=ev["ago_h"]),
        ))

    print(f"  ✓ {len(events)} events (mode changes, config changes, maintenance)")

    # ------------------------------------------------------------------
    # COMMIT
    # ------------------------------------------------------------------
    conn.commit()
    cur.close()

    print("\n" + "=" * 60)
    print("SEED COMPLETE")
    print("=" * 60)
    print(f"\nSite:    {SITE_ID}")
    print(f"Block:   {BLOCK_ID}")
    print(f"Sensors: {len(sensor_id_map)}")
    print(f"Telemetry: {row_count:,} rows (24h @ 30s intervals)")
    print(f"\n{'─' * 60}")
    print("API KEYS (store securely — cannot be retrieved again):")
    print(f"{'─' * 60}")
    for name, key in api_keys.items():
        print(f"  {name:30s}  {key}")
    print(f"{'─' * 60}")

    return api_keys


# =============================================================================
# ENTRYPOINT
# =============================================================================

def main():
    print("Connecting to database...")
    conn = psycopg2.connect(DB_DSN)
    try:
        seed(conn)
    except Exception as e:
        conn.rollback()
        print(f"\n✗ SEED FAILED: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
