"""
MicroLink MCS — Modbus Device Simulator
Stream A · Task 8 · v1.0.0

Simulates all Modbus devices for a 1MW block so the full edge stack
(adapters → MQTT → Stream B ingestion) can be tested without hardware.

Features:
- Realistic thermal physics model (heat generation, transfer, rejection)
- Mode state machine with automatic transitions
- Sensor noise and drift
- Fault injection via CLI commands
- Configurable scenarios (startup, steady-state, export, reject, leak)

Dependencies:
    pip install pymodbus pyyaml

Usage:
    python modbus_simulator.py                          # Default: steady-state EXPORT
    python modbus_simulator.py --scenario startup       # Cold start sequence
    python modbus_simulator.py --scenario fault-leak    # Leak fault injection
    python modbus_simulator.py --interactive            # CLI for live fault injection

The simulator runs Modbus TCP servers on the same ports as real devices,
so the modbus_adapter.py reads from it with zero config changes.
"""

import asyncio
import json
import logging
import math
import random
import struct
import time
import argparse
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional, Dict, List, Callable

import yaml

from pymodbus.server import StartAsyncTcpServer, ServerAsyncStop
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusSlaveContext,
    ModbusServerContext,
    ModbusSequentialDataBlock,
)

# ─── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulator")

# ─── Constants ─────────────────────────────────────────────────────────────

WATER_DENSITY = 997.0       # kg/m³
WATER_CP = 4.186            # kJ/(kg·°C)
GLYCOL_CP = 3.5             # kJ/(kg·°C) for 35% PG
SIM_TICK_S = 1.0            # Physics update interval


class Mode(IntEnum):
    EXPORT = 0
    MIXED = 1
    REJECT = 2
    MAINTENANCE = 3


# ─── Thermal physics model ────────────────────────────────────────────────

@dataclass
class ThermalModel:
    """Simplified thermal model for a 1MW compute block.

    Models heat flow through the 3-loop architecture:
    - Loop 1: IT coolant picks up heat from servers
    - Loop 2: Facility water transports heat to HX or dry cooler
    - Loop 3: Host process water receives heat via plate HX
    """

    # ─── IT load ───
    it_load_kw: float = 700.0          # Current IT power draw (kW)
    it_load_target_kw: float = 700.0   # Target (ramps toward this)
    it_load_ramp_rate: float = 50.0    # kW/minute ramp rate

    # ─── Loop 1 (IT secondary) ───
    cdu1_supply_t: float = 35.0        # CDU supply to racks
    cdu1_return_t: float = 50.0        # CDU return from racks
    cdu2_supply_t: float = 35.0
    cdu2_return_t: float = 50.0
    cdu1_flow: float = 8.0             # m³/h
    cdu2_flow: float = 8.0

    # ─── Loop 2 (MicroLink primary) ───
    l2_supply_t: float = 45.0          # Warm supply header
    l2_return_t: float = 35.0          # Cooled return header
    l2_flow: float = 12.8              # m³/h
    l2_pressure_supply: float = 300.0  # kPa
    l2_pressure_return: float = 250.0
    l2_dp: float = 50.0

    # ─── Heat exchanger ───
    hx_primary_in: float = 45.0
    hx_primary_out: float = 38.0
    hx_secondary_in: float = 28.0      # Host return (cold)
    hx_secondary_out: float = 40.0     # Host supply (warm)
    hx_approach: float = 5.0
    hx_dp: float = 30.0
    hx_fouling_factor: float = 1.0     # 1.0=clean, rises with fouling

    # ─── Billing meter ───
    bil_flow: float = 10.0
    bil_kwt: float = 0.0               # Instantaneous thermal power
    bil_kwht: float = 0.0              # Cumulative thermal energy

    # ─── Loop 3 (host) ───
    l3_supply_t: float = 40.0
    l3_return_t: float = 28.0
    l3_flow: float = 10.0
    host_demand: bool = True
    host_hw_tank_t: float = 55.0
    host_boiler_on: bool = False

    # ─── Dry cooler / rejection ───
    dc_inlet_t: float = 45.0
    dc_outlet_t: float = 35.0
    ambient_t: float = 15.0            # Outdoor temp (Baldwinsville average)
    ambient_wb: float = 12.0
    ambient_rh: float = 60.0
    dc_fan_speed: float = 0.0          # 0-1200 rpm (0 in EXPORT mode)
    dc_kw: float = 0.0                 # Fan power consumption

    # ─── Mode state ───
    mode: Mode = Mode.REJECT
    v_exp_pos: float = 0.0             # Export valve %open
    v_rej_pos: float = 100.0           # Reject valve %open
    mode_timer: float = 0.0            # Time in current mode

    # ─── Pumps ───
    pp01_running: bool = True
    pp01_speed: float = 75.0           # VFD %
    pp01_kw: float = 2.5
    pp01_vibration: float = 3.2        # mm/s RMS
    pp01_bearing_t: float = 45.0
    pp01_hours: float = 2400.0
    pp02_running: bool = False
    pp02_speed: float = 0.0

    # ─── Expansion vessel ───
    exp_pressure: float = 250.0
    exp_level: float = 60.0
    exp_temp: float = 40.0

    # ─── Chemistry ───
    ph: float = 8.1
    conductivity: float = 800.0
    glycol_pct: float = 35.0

    # ─── Electrical ───
    revenue_kw: float = 850.0          # Total block power (IT + cooling)
    revenue_kva: float = 900.0
    revenue_kwh: float = 125000.0      # Cumulative
    voltage_l1: float = 480.0
    voltage_l2: float = 479.5
    voltage_l3: float = 480.2
    frequency: float = 60.0
    power_factor: float = 0.94
    transformer_t_pri: float = 65.0
    transformer_t_sec: float = 58.0
    transformer_load: float = 57.0
    ups_load: float = 45.0
    ups_batt_pct: float = 100.0
    ups_runtime_min: float = 30.0
    ups_on_battery: bool = False

    # ─── Safety ───
    leak_zones: List[bool] = field(default_factory=lambda: [False]*8)
    freeze_l2_t: float = 15.0
    freeze_l3_t: float = 15.0
    freeze_dc_t: float = 15.0
    prv_l2_open: bool = False
    plc_running: bool = True
    plc_watchdog: int = 0
    plc_faults: int = 0

    # ─── Environmental ───
    rack_inlet_temps: List[float] = field(
        default_factory=lambda: [24.0 + random.uniform(-1, 1) for _ in range(14)]
    )
    rack_outlet_temps: List[float] = field(
        default_factory=lambda: [40.0 + random.uniform(-2, 2) for _ in range(3)]
    )
    humidity_front: float = 45.0
    humidity_rear: float = 48.0

    # ─── Fault injection ───
    _faults: Dict[str, bool] = field(default_factory=dict)

    def tick(self, dt: float = SIM_TICK_S):
        """Advance the thermal model by dt seconds."""

        # ─── IT load ramping ───
        if self.it_load_kw != self.it_load_target_kw:
            delta = self.it_load_ramp_rate * (dt / 60.0)
            if self.it_load_kw < self.it_load_target_kw:
                self.it_load_kw = min(self.it_load_kw + delta, self.it_load_target_kw)
            else:
                self.it_load_kw = max(self.it_load_kw - delta, self.it_load_target_kw)

        # ─── Mode timer ───
        self.mode_timer += dt

        # ─── Heat generation (Loop 1) ───
        # IT heat splits between 2 CDUs
        heat_per_cdu = self.it_load_kw / 2.0
        if self.cdu1_flow > 0:
            dt_cdu1 = heat_per_cdu / (self.cdu1_flow * WATER_DENSITY / 3600 * GLYCOL_CP)
            self.cdu1_return_t = self._smooth(self.cdu1_return_t,
                                               self.cdu1_supply_t + dt_cdu1, 0.1)
        if self.cdu2_flow > 0:
            dt_cdu2 = heat_per_cdu / (self.cdu2_flow * WATER_DENSITY / 3600 * GLYCOL_CP)
            self.cdu2_return_t = self._smooth(self.cdu2_return_t,
                                               self.cdu2_supply_t + dt_cdu2, 0.1)

        # ─── Loop 2 supply temp (from CDU returns) ───
        target_l2s = (self.cdu1_return_t + self.cdu2_return_t) / 2.0
        self.l2_supply_t = self._smooth(self.l2_supply_t, target_l2s, 0.05)

        # ─── Heat export / rejection ───
        total_heat = self.it_load_kw * 0.95  # ~95% of IT load becomes heat

        if self.mode == Mode.EXPORT:
            # All heat to HX
            exported = total_heat
            rejected = 0
        elif self.mode == Mode.REJECT:
            exported = 0
            rejected = total_heat
        elif self.mode == Mode.MIXED:
            export_frac = self.v_exp_pos / 100.0
            exported = total_heat * export_frac
            rejected = total_heat * (1 - export_frac)
        else:  # MAINTENANCE
            exported = 0
            rejected = 0

        # ─── HX model ───
        if exported > 0 and self.l2_flow > 0:
            effectiveness = 0.85 / self.hx_fouling_factor
            self.hx_primary_in = self.l2_supply_t
            dt_hx = exported / (self.l2_flow * WATER_DENSITY / 3600 * GLYCOL_CP)
            self.hx_primary_out = self.hx_primary_in - (dt_hx * effectiveness)
            self.hx_secondary_out = self.hx_primary_in - (5.0 * self.hx_fouling_factor)
            self.hx_approach = self.hx_primary_in - self.hx_secondary_out
            self.bil_kwt = exported
            self.bil_kwht += exported * (dt / 3600.0)
            self.bil_flow = self.l3_flow
            self.l3_supply_t = self._smooth(self.l3_supply_t, self.hx_secondary_out, 0.1)
        else:
            self.hx_primary_in = self.l2_supply_t
            self.hx_primary_out = self.l2_supply_t
            self.hx_secondary_out = self.hx_secondary_in
            self.bil_kwt = 0.0
            self.bil_flow = 0.0
            self.l3_supply_t = self._smooth(self.l3_supply_t, self.hx_secondary_in, 0.05)

        # ─── Dry cooler model ───
        if rejected > 0:
            self.dc_inlet_t = self.l2_supply_t
            # Dry cooler effectiveness depends on ambient + fan speed
            dc_capacity = (self.dc_fan_speed / 1200.0) * 1200.0  # Max 1200 kW rejection
            approach_min = max(3.0, self.ambient_t + 5.0)
            if dc_capacity > 0:
                self.dc_outlet_t = self._smooth(
                    self.dc_outlet_t,
                    max(approach_min, self.dc_inlet_t - (rejected / dc_capacity) * 15.0),
                    0.08,
                )
            self.dc_kw = (self.dc_fan_speed / 1200.0) * 15.0  # Max 15kW fans
            self.dc_fan_speed = min(1200, max(200, rejected / 1.0))
        else:
            self.dc_fan_speed = 0
            self.dc_kw = 0
            self.dc_outlet_t = self._smooth(self.dc_outlet_t, self.ambient_t + 5, 0.02)

        # ─── Loop 2 return (mixed from HX and DC) ───
        if self.mode == Mode.EXPORT:
            target_l2r = self.hx_primary_out
        elif self.mode == Mode.REJECT:
            target_l2r = self.dc_outlet_t
        elif self.mode == Mode.MIXED:
            exp_frac = self.v_exp_pos / 100.0
            target_l2r = (self.hx_primary_out * exp_frac +
                          self.dc_outlet_t * (1 - exp_frac))
        else:
            target_l2r = self.l2_supply_t - 2.0  # Minimal cooling in maintenance

        self.l2_return_t = self._smooth(self.l2_return_t, target_l2r, 0.08)

        # CDU supply = Loop 2 return (CDUs cool the IT side using facility water)
        self.cdu1_supply_t = self._smooth(self.cdu1_supply_t, self.l2_return_t, 0.1)
        self.cdu2_supply_t = self._smooth(self.cdu2_supply_t, self.l2_return_t, 0.1)

        # ─── Electrical model ───
        cooling_overhead = self.pp01_kw + self.dc_kw + 5.0  # Pumps + fans + controls
        self.revenue_kw = self.it_load_kw + cooling_overhead
        self.revenue_kva = self.revenue_kw / self.power_factor
        self.revenue_kwh += self.revenue_kw * (dt / 3600.0)
        self.transformer_load = (self.revenue_kw / 1500.0) * 100.0
        self.transformer_t_pri = 40.0 + self.transformer_load * 0.8
        self.transformer_t_sec = 35.0 + self.transformer_load * 0.6

        # ─── Pump physics ───
        if self.pp01_running:
            self.pp01_hours += dt / 3600.0
            self.pp01_kw = 1.0 + (self.pp01_speed / 100.0) * 3.0
            self.pp01_bearing_t = 35.0 + (self.pp01_speed / 100.0) * 20.0
            # Vibration: base + random walk
            self.pp01_vibration += random.gauss(0, 0.05)
            self.pp01_vibration = max(1.0, min(15.0, self.pp01_vibration))

        # ─── Expansion vessel ───
        self.exp_temp = self._smooth(self.exp_temp, self.l2_return_t, 0.02)
        # Pressure follows temperature (thermal expansion)
        self.exp_pressure = 200.0 + (self.exp_temp - 20.0) * 3.0

        # ─── Environmental ───
        for i in range(14):
            target = 22.0 + (self.it_load_kw / 1000.0) * 5.0
            self.rack_inlet_temps[i] = self._smooth(
                self.rack_inlet_temps[i],
                target + random.uniform(-0.5, 0.5), 0.05,
            )

        # ─── Safety PLC watchdog ───
        if self.plc_running:
            self.plc_watchdog = (self.plc_watchdog + 1) % 65536

        # ─── Host model ───
        if self.host_demand and self.mode == Mode.EXPORT:
            # Tank fills with our heat
            self.host_hw_tank_t = self._smooth(self.host_hw_tank_t, 58.0, 0.01)
            self.host_boiler_on = False
        else:
            # Tank cools (brewery consuming hot water)
            self.host_hw_tank_t = self._smooth(self.host_hw_tank_t, 35.0, 0.005)
            self.host_boiler_on = self.host_hw_tank_t < 45.0

        # ─── Ambient outdoor (sinusoidal daily cycle) ───
        hour = (time.time() % 86400) / 3600.0  # Hour of day
        self.ambient_t = 10.0 + 10.0 * math.sin((hour - 6) * math.pi / 12)
        self.ambient_wb = self.ambient_t - 3.0
        self.freeze_l2_t = max(self.ambient_t + 5, self.l2_return_t - 2)
        self.freeze_l3_t = max(self.ambient_t + 3, self.l3_return_t - 2)
        self.freeze_dc_t = self.ambient_t + 2

        # ─── Fault injection effects ───
        if self._faults.get("leak_zone1"):
            self.leak_zones[0] = True
            self.leak_zones[1] = True
        if self._faults.get("pump_trip"):
            self.pp01_running = False
            self.pp01_speed = 0
            self.pp02_running = True
            self.pp02_speed = self.pp01_speed
        if self._faults.get("sensor_drift"):
            self.l2_supply_t += random.gauss(0, 0.5)
        if self._faults.get("hx_fouling"):
            self.hx_fouling_factor = min(2.0, self.hx_fouling_factor + 0.001)
        if self._faults.get("ups_battery"):
            self.ups_on_battery = True
            self.ups_batt_pct = max(0, self.ups_batt_pct - 0.5)
            self.ups_runtime_min = max(0, self.ups_runtime_min - 0.1)

        # ─── Add sensor noise ───
        self._add_noise()

    def _smooth(self, current: float, target: float, rate: float) -> float:
        """Exponential smoothing — simulates thermal inertia."""
        return current + (target - current) * rate

    def _add_noise(self):
        """Add realistic sensor noise to readings."""
        self.voltage_l1 = 480.0 + random.gauss(0, 0.3)
        self.voltage_l2 = 479.5 + random.gauss(0, 0.3)
        self.voltage_l3 = 480.2 + random.gauss(0, 0.3)
        self.frequency = 60.0 + random.gauss(0, 0.005)
        self.power_factor = 0.94 + random.gauss(0, 0.005)
        self.humidity_front = 45.0 + random.gauss(0, 1.5)
        self.humidity_rear = 48.0 + random.gauss(0, 1.5)
        self.ph = 8.1 + random.gauss(0, 0.05)
        self.conductivity = 800.0 + random.gauss(0, 20)

    # ─── Mode control ─────────────────────────────────────────

    def set_mode(self, mode: Mode):
        """Change thermal mode."""
        old = self.mode
        self.mode = mode
        self.mode_timer = 0

        if mode == Mode.EXPORT:
            self.v_exp_pos = 100.0
            self.v_rej_pos = 0.0
        elif mode == Mode.REJECT:
            self.v_exp_pos = 0.0
            self.v_rej_pos = 100.0
        elif mode == Mode.MIXED:
            self.v_exp_pos = 50.0
            self.v_rej_pos = 50.0
        elif mode == Mode.MAINTENANCE:
            self.v_exp_pos = 0.0
            self.v_rej_pos = 0.0

        logger.info(f"Mode: {Mode(old).name} → {mode.name}")

    def auto_mode_transitions(self):
        """Evaluate automatic mode transitions (mimics Safety PLC logic)."""
        # Emergency → REJECT
        if any(self.leak_zones):
            if self.mode != Mode.REJECT:
                self.set_mode(Mode.REJECT)
                return

        if self.mode == Mode.REJECT and self.mode_timer > 300:
            # Try to go to EXPORT after 5 min dwell
            if (self.l2_supply_t > 40 and self.l2_supply_t < 52 and
                    self.host_demand and self.plc_running and
                    not any(self.leak_zones)):
                self.set_mode(Mode.EXPORT)

        elif self.mode == Mode.EXPORT:
            if not self.host_demand:
                self.set_mode(Mode.REJECT)
            elif self.l2_supply_t > 52:
                self.set_mode(Mode.MIXED)

        elif self.mode == Mode.MIXED:
            if self.l2_supply_t < 45 and self.mode_timer > 180:
                self.set_mode(Mode.EXPORT)
            elif not self.host_demand:
                self.set_mode(Mode.REJECT)

    def inject_fault(self, fault_name: str):
        """Inject a named fault."""
        self._faults[fault_name] = True
        logger.warning(f"FAULT INJECTED: {fault_name}")

    def clear_fault(self, fault_name: str):
        """Clear a named fault."""
        self._faults.pop(fault_name, None)
        # Reset affected state
        if fault_name == "leak_zone1":
            self.leak_zones[0] = False
            self.leak_zones[1] = False
        if fault_name == "pump_trip":
            self.pp01_running = True
            self.pp01_speed = 75.0
            self.pp02_running = False
        if fault_name == "ups_battery":
            self.ups_on_battery = False
            self.ups_batt_pct = 100.0
            self.ups_runtime_min = 30.0
        logger.info(f"FAULT CLEARED: {fault_name}")


# ─── Register mapping ─────────────────────────────────────────────────────

def float_to_registers(value: float) -> tuple:
    """Convert float to two 16-bit registers (big-endian)."""
    packed = struct.pack(">f", value)
    r0 = struct.unpack(">H", packed[0:2])[0]
    r1 = struct.unpack(">H", packed[2:4])[0]
    return r0, r1


def build_register_block(model: ThermalModel) -> dict:
    """Build the complete register map from current model state.

    Returns dict of {address: value} for all holding registers.
    Addresses match the Master Point Schedule.
    """
    regs = {}

    def set_float(addr, val):
        r0, r1 = float_to_registers(val)
        regs[addr - 40001] = r0
        regs[addr - 40001 + 1] = r1

    def set_uint16(addr, val):
        regs[addr - 40001] = int(val) & 0xFFFF

    # ─── ELECTRICAL (40001-40399) ───
    set_float(40001, model.revenue_kw)
    set_float(40003, model.revenue_kva)
    set_float(40005, model.revenue_kw * 0.05)  # kVAr
    set_float(40007, model.voltage_l1)
    set_float(40009, model.voltage_l2)
    set_float(40011, model.voltage_l3)
    set_float(40013, model.revenue_kw / model.voltage_l1 / 1.732)  # A-L1
    set_float(40015, model.revenue_kw / model.voltage_l2 / 1.732)
    set_float(40017, model.revenue_kw / model.voltage_l3 / 1.732)
    set_float(40019, model.power_factor)
    set_float(40021, model.frequency)
    set_float(40023, model.revenue_kwh)
    set_float(40025, 2.1 + random.gauss(0, 0.1))  # THD
    # Transformer
    set_float(40101, model.transformer_t_pri)
    set_float(40103, model.transformer_t_sec)
    set_float(40105, model.ambient_t)
    set_float(40107, model.transformer_load)
    set_uint16(40109, 1 if model.transformer_load > 75 else 0)  # Fan
    # MSB
    set_float(40201, model.voltage_l1)
    set_float(40203, model.voltage_l2)
    set_float(40205, model.voltage_l3)
    set_float(40207, model.revenue_kw / model.voltage_l1 / 1.732 * 3)
    set_float(40209, model.revenue_kw)
    set_float(40211, 45.0 + model.transformer_load * 0.3)  # Busbar temp
    set_uint16(40213, 1)  # Main CB closed
    set_uint16(40214, 1)  # SPD healthy

    # ─── THERMAL LOOP 1 — CDUs (41001-41299) ───
    set_float(41001, model.cdu1_supply_t)
    set_float(41003, model.cdu1_return_t)
    set_float(41005, model.cdu1_flow)
    set_float(41007, 80.0)     # CDU dP
    set_float(41009, model.cdu1_supply_t)  # Facility supply = L2 return
    set_float(41011, model.cdu1_return_t)  # Facility return
    set_float(41013, 75.0)     # Pump speed
    set_uint16(41015, 1)       # CDU status running
    set_float(41101, model.cdu2_supply_t)
    set_float(41103, model.cdu2_return_t)
    set_float(41105, model.cdu2_flow)
    set_float(41107, 80.0)
    set_float(41109, model.cdu2_supply_t)
    set_float(41111, model.cdu2_return_t)
    set_float(41113, 75.0)
    set_uint16(41115, 1)

    # Per-rack coolant temps
    for i in range(14):
        addr = 41201 + (i * 2)
        rack_t = model.cdu1_return_t + random.uniform(-2, 2)
        set_float(addr, rack_t)

    # ─── THERMAL LOOP 2 (42001-42499) ───
    set_float(42001, model.l2_supply_t)
    set_float(42003, model.l2_return_t)
    set_float(42005, model.l2_flow)
    set_float(42007, model.l2_pressure_supply)
    set_float(42009, model.l2_pressure_return)
    set_float(42011, model.l2_dp)
    # PP-01
    set_uint16(42101, 1 if model.pp01_running else 0)
    set_float(42103, model.pp01_speed)
    set_float(42105, model.pp01_kw)
    set_float(42107, model.pp01_hours)
    set_float(42109, model.pp01_vibration)
    set_float(42111, model.pp01_bearing_t)
    set_uint16(42113, 0)  # No fault
    # PP-02
    set_uint16(42201, 1 if model.pp02_running else 0)
    set_float(42203, model.pp02_speed)
    set_float(42205, 0.0 if not model.pp02_running else 2.5)
    set_float(42207, 100.0)
    set_float(42209, 3.0 + random.gauss(0, 0.1))
    set_float(42211, 35.0)
    set_uint16(42213, 0)
    # Expansion
    set_float(42301, model.exp_pressure)
    set_float(42303, model.exp_level)
    set_float(42305, model.exp_temp)
    # Chemistry
    set_float(42401, model.ph)
    set_float(42403, model.conductivity)
    set_float(42405, model.glycol_pct)

    # ─── THERMAL HX (43001-43199) ───
    set_float(43001, model.hx_primary_in)
    set_float(43003, model.hx_primary_out)
    set_float(43005, model.hx_secondary_in)
    set_float(43007, model.hx_secondary_out)
    set_float(43009, model.l2_pressure_supply)
    set_float(43011, model.l2_pressure_supply - model.hx_dp)
    set_float(43013, model.hx_dp)
    set_float(43015, model.hx_approach)
    # Billing meter
    set_float(43101, model.bil_flow)
    set_float(43103, model.l3_supply_t)
    set_float(43105, model.l3_return_t)
    set_float(43107, model.bil_kwt)
    set_float(43109, model.bil_kwht)
    set_uint16(43111, 1)  # Billing meter healthy

    # ─── THERMAL LOOP 3 (43201-43399) ───
    set_float(43201, model.l3_supply_t)
    set_float(43203, model.l3_return_t)
    set_float(43205, model.l2_pressure_supply - 20)
    set_float(43207, model.l2_pressure_return + 10)
    set_float(43209, model.l3_flow)
    set_float(43211, model.v_exp_pos)     # V-ISO-ML
    set_float(43213, 100.0 if model.host_demand else 0.0)  # V-ISO-HOST
    set_float(43215, 25.0)                # BFP dP
    set_uint16(43217, 1)                  # BFP passed
    # Mode valves
    set_float(43301, model.v_exp_pos)
    set_float(43303, model.v_exp_pos)     # Command = actual (simulator)
    set_float(43305, model.v_rej_pos)
    set_float(43307, model.v_rej_pos)
    set_uint16(43309, 0)                  # No V-EXP fault
    set_uint16(43311, 0)                  # No V-REJ fault

    # ─── THERMAL REJECT (44001-44099) ───
    set_float(44001, model.dc_inlet_t)
    set_float(44003, model.dc_outlet_t)
    set_float(44005, model.ambient_t)
    set_float(44007, model.ambient_wb)
    set_float(44009, model.ambient_rh)
    for i in range(4):
        set_float(44011 + i * 2, model.dc_fan_speed)
    set_uint16(44019, 0)                  # No fan faults
    set_float(44021, model.dc_kw)
    set_float(44023, 0.0)                 # Spray flow
    set_float(44025, 0.0)                 # Spray valve
    set_float(44027, 45.0 + model.dc_fan_speed / 1200 * 15)  # Noise

    # ─── THERMAL SAFETY (45001-45499) ───
    for i in range(8):
        set_uint16(45001 + i, 1 if model.leak_zones[i] else 0)
    set_float(45101, model.freeze_l2_t)
    set_float(45103, model.freeze_l3_t)
    set_float(45105, model.freeze_dc_t)
    set_uint16(45107, 1 if model.freeze_l3_t < 3 else 0)  # Trace heating
    set_uint16(45201, 1 if model.prv_l2_open else 0)
    set_uint16(45203, 0)
    set_uint16(45205, 0)  # PRV cycles
    set_float(45301, 0.0)  # Bund level
    set_uint16(45303, 0)   # Bund pump off
    set_uint16(45401, 1 if model.plc_running else 0)
    set_uint16(45403, int(model.mode))
    set_uint16(45405, model.plc_watchdog)
    set_uint16(45407, model.plc_faults)

    # ─── MODE (45409-45420) ───
    set_uint16(45409, 1 if model.mode == Mode.MAINTENANCE else 0)
    set_uint16(45411, 0)
    set_float(45413, model.mode_timer / 3600 if model.mode == Mode.EXPORT else 0)
    set_float(45415, model.mode_timer / 3600 if model.mode == Mode.REJECT else 0)
    set_uint16(45417, 0)

    # ─── ENVIRONMENTAL (46001-46199) ───
    for i in range(14):
        set_float(46001 + i * 2, model.rack_inlet_temps[i])
    set_float(46029, model.rack_outlet_temps[0])
    set_float(46031, model.rack_outlet_temps[1])
    set_float(46033, model.rack_outlet_temps[2])
    set_float(46101, model.humidity_front)
    set_float(46103, model.humidity_rear)
    set_float(46105, 2.5 + random.gauss(0, 0.3))  # dP
    set_float(46107, sum(model.rack_inlet_temps) / 14)  # Room avg

    return regs


# ─── Modbus server ─────────────────────────────────────────────────────────

class SimulatorServer:
    """Runs Modbus TCP servers mimicking real devices."""

    def __init__(self, model: ThermalModel):
        self.model = model
        self.contexts = {}
        self._servers = []

    def _build_datablock(self) -> ModbusSequentialDataBlock:
        """Build a register data block covering addresses 0-9999."""
        # Pre-fill with zeros
        values = [0] * 10000
        return ModbusSequentialDataBlock(0, values)

    def _update_registers(self):
        """Update all Modbus registers from current model state."""
        regs = build_register_block(self.model)
        for context_name, context in self.contexts.items():
            for slave_id in context:
                store = context[slave_id].store["h"]  # Holding registers
                for addr, value in regs.items():
                    if 0 <= addr < 10000:
                        store.setValues(addr, [value])

    async def start(self, ports: List[int]):
        """Start Modbus TCP servers on specified ports."""
        for port in ports:
            # Create slave context
            slave = ModbusSlaveContext(
                hr=self._build_datablock(),  # Holding registers
                ir=self._build_datablock(),  # Input registers
                co=ModbusSequentialDataBlock(0, [0] * 1000),
                di=ModbusSequentialDataBlock(0, [0] * 1000),
            )
            context = ModbusServerContext(
                slaves={1: slave, 2: slave, 3: slave},
                single=False,
            )
            self.contexts[port] = context

            identity = ModbusDeviceIdentification()
            identity.VendorName = "MicroLink Simulator"
            identity.ProductName = f"MCS-SIM port {port}"
            identity.ModelName = "SIM-1MW"

            logger.info(f"Starting Modbus TCP server on port {port}")
            asyncio.create_task(
                StartAsyncTcpServer(
                    context=context,
                    identity=identity,
                    address=("0.0.0.0", port),
                )
            )

    async def run_update_loop(self):
        """Continuously update registers from model state."""
        logger.info("Register update loop started")
        while True:
            self.model.tick(SIM_TICK_S)
            self.model.auto_mode_transitions()
            self._update_registers()
            await asyncio.sleep(SIM_TICK_S)


# ─── Scenarios ─────────────────────────────────────────────────────────────

SCENARIOS = {
    "steady-export": {
        "description": "Steady-state EXPORT mode, 700kW IT load, host demanding heat",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            setattr(m, "it_load_target_kw", 700),
            m.set_mode(Mode.EXPORT),
            setattr(m, "host_demand", True),
            setattr(m, "l2_supply_t", 45),
        ),
    },
    "steady-reject": {
        "description": "Steady-state REJECT mode, dry cooler active",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            m.set_mode(Mode.REJECT),
            setattr(m, "host_demand", False),
        ),
    },
    "startup": {
        "description": "Cold start — ramp from 0 to 700kW, boot to REJECT then EXPORT",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 0),
            setattr(m, "it_load_target_kw", 700),
            setattr(m, "l2_supply_t", 22),
            setattr(m, "l2_return_t", 20),
            m.set_mode(Mode.REJECT),
            setattr(m, "host_demand", True),
        ),
    },
    "ramp-up": {
        "description": "Load ramp from 300kW to 1000kW (expansion scenario)",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 300),
            setattr(m, "it_load_target_kw", 1000),
            m.set_mode(Mode.EXPORT),
            setattr(m, "host_demand", True),
        ),
    },
    "fault-leak": {
        "description": "Leak detection in zone 1 → emergency REJECT",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            m.set_mode(Mode.EXPORT),
            # Fault injected after 30s via timer
        ),
        "timed_events": [
            (30, lambda m: m.inject_fault("leak_zone1")),
            (120, lambda m: m.clear_fault("leak_zone1")),
        ],
    },
    "fault-pump": {
        "description": "Duty pump trip → standby auto-starts",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            m.set_mode(Mode.EXPORT),
        ),
        "timed_events": [
            (30, lambda m: m.inject_fault("pump_trip")),
            (180, lambda m: m.clear_fault("pump_trip")),
        ],
    },
    "fault-hx-fouling": {
        "description": "Gradual HX fouling — approach ΔT increases over time",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            m.set_mode(Mode.EXPORT),
            m.inject_fault("hx_fouling"),
        ),
    },
    "host-demand-cycle": {
        "description": "Host toggles demand on/off every 5 minutes",
        "setup": lambda m: (
            setattr(m, "it_load_kw", 700),
            m.set_mode(Mode.EXPORT),
            setattr(m, "host_demand", True),
        ),
        "timed_events": [
            (300, lambda m: setattr(m, "host_demand", False)),
            (600, lambda m: setattr(m, "host_demand", True)),
            (900, lambda m: setattr(m, "host_demand", False)),
            (1200, lambda m: setattr(m, "host_demand", True)),
        ],
    },
}


# ─── Interactive CLI ───────────────────────────────────────────────────────

def interactive_cli(model: ThermalModel):
    """CLI for live fault injection and mode control."""
    print("\n╔══════════════════════════════════════════════╗")
    print("║  MicroLink Modbus Simulator — Interactive    ║")
    print("╚══════════════════════════════════════════════╝")
    print("\nCommands:")
    print("  mode export|reject|mixed|maintenance")
    print("  load <kW>           — set IT load target")
    print("  fault <name>        — inject fault")
    print("  clear <name>        — clear fault")
    print("  host on|off         — toggle host demand")
    print("  status              — show current state")
    print("  faults              — list available faults")
    print("  quit\n")

    while True:
        try:
            cmd = input("sim> ").strip().lower()
            if not cmd:
                continue

            parts = cmd.split()

            if parts[0] == "quit":
                break
            elif parts[0] == "mode" and len(parts) > 1:
                mode_map = {"export": Mode.EXPORT, "reject": Mode.REJECT,
                            "mixed": Mode.MIXED, "maintenance": Mode.MAINTENANCE}
                if parts[1] in mode_map:
                    model.set_mode(mode_map[parts[1]])
                else:
                    print(f"Unknown mode: {parts[1]}")
            elif parts[0] == "load" and len(parts) > 1:
                model.it_load_target_kw = float(parts[1])
                print(f"IT load target → {parts[1]} kW")
            elif parts[0] == "fault" and len(parts) > 1:
                model.inject_fault(parts[1])
            elif parts[0] == "clear" and len(parts) > 1:
                model.clear_fault(parts[1])
            elif parts[0] == "host" and len(parts) > 1:
                model.host_demand = parts[1] == "on"
                print(f"Host demand → {'ON' if model.host_demand else 'OFF'}")
            elif parts[0] == "status":
                print(f"\n  Mode:        {Mode(model.mode).name}")
                print(f"  IT Load:     {model.it_load_kw:.0f} kW "
                      f"(target: {model.it_load_target_kw:.0f})")
                print(f"  L2 Supply:   {model.l2_supply_t:.1f}°C")
                print(f"  L2 Return:   {model.l2_return_t:.1f}°C")
                print(f"  HX Approach: {model.hx_approach:.1f}°C")
                print(f"  Bil kWt:     {model.bil_kwt:.0f}")
                print(f"  Bil kWht:    {model.bil_kwht:.1f}")
                print(f"  Ambient:     {model.ambient_t:.1f}°C")
                print(f"  Revenue kW:  {model.revenue_kw:.0f}")
                print(f"  Host demand: {'ON' if model.host_demand else 'OFF'}")
                print(f"  Faults:      {list(model._faults.keys()) or 'none'}")
                print(f"  Leaks:       {[i for i,v in enumerate(model.leak_zones) if v] or 'none'}\n")
            elif parts[0] == "faults":
                print("  leak_zone1    — Trigger leak detectors in zone 1")
                print("  pump_trip     — Trip duty pump PP-01")
                print("  sensor_drift  — Add random drift to L2 supply temp")
                print("  hx_fouling    — Gradual HX fouling (approach ΔT rises)")
                print("  ups_battery   — Simulate UPS battery discharge")
            else:
                print(f"Unknown command: {cmd}")

        except (KeyboardInterrupt, EOFError):
            break


# ─── Main ──────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="MicroLink Modbus Simulator")
    parser.add_argument(
        "--scenario", type=str, default="steady-export",
        choices=list(SCENARIOS.keys()),
        help="Simulation scenario",
    )
    parser.add_argument(
        "--ports", type=str, default="502,10502,10503",
        help="Comma-separated Modbus TCP ports (matches real device ports)",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Enable interactive CLI for live control",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.log_level))

    ports = [int(p) for p in args.ports.split(",")]
    scenario = SCENARIOS[args.scenario]

    logger.info(f"Scenario: {args.scenario} — {scenario['description']}")

    # Create model and apply scenario setup
    model = ThermalModel()
    setup_fn = scenario.get("setup")
    if setup_fn:
        setup_fn(model)

    # Start server
    server = SimulatorServer(model)
    await server.start(ports)

    # Build tasks
    tasks = [asyncio.create_task(server.run_update_loop())]

    # Timed events
    timed_events = scenario.get("timed_events", [])
    if timed_events:
        async def run_timed_events():
            start = time.monotonic()
            pending = list(timed_events)
            while pending:
                elapsed = time.monotonic() - start
                due = [e for e in pending if e[0] <= elapsed]
                for event in due:
                    event[1](model)
                    pending.remove(event)
                await asyncio.sleep(1)
        tasks.append(asyncio.create_task(run_timed_events()))

    # Interactive CLI in background thread
    if args.interactive:
        cli_thread = threading.Thread(target=interactive_cli, args=(model,),
                                       daemon=True)
        cli_thread.start()

    # Status printer
    async def print_status():
        while True:
            await asyncio.sleep(10)
            logger.info(
                f"[{Mode(model.mode).name}] IT={model.it_load_kw:.0f}kW "
                f"L2s={model.l2_supply_t:.1f}°C L2r={model.l2_return_t:.1f}°C "
                f"HX={model.hx_approach:.1f}°C Bil={model.bil_kwt:.0f}kWt "
                f"Amb={model.ambient_t:.1f}°C Rev={model.revenue_kw:.0f}kW"
            )
    tasks.append(asyncio.create_task(print_status()))

    logger.info(f"Simulator running on ports {ports}. Press Ctrl+C to stop.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
