"""
MCS Stream C — Task 3: kWht Heat Credit Calculator
====================================================
Service that calculates thermal energy exported to the host and
determines heat credits or revenue share amounts.

Consumes Stream B's REST API for thermal meter readings (flow + temps)
and mode state events.

Usage:
    calculator = KWhtCalculator(api_client, session)
    result = await calculator.calculate(site_id="BALD-01", year=2026, month=1)
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any, Tuple

import httpx

# Local imports — Task 1 models
from billing_models import (
    Session,
    Contract, HostAgreement, RateSchedule,
    ContractType, ContractStatus, RateType, HeatPricingModel,
    get_host_agreement_for_site, get_active_contracts_for_site, get_rate,
)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

INTERVAL_MINUTES = 5
INTERVAL_HOURS = Decimal(str(INTERVAL_MINUTES)) / Decimal("60")

# Thermal calculation constants
WATER_DENSITY_KG_M3 = Decimal("997")    # kg/m³ at ~25°C
SPECIFIC_HEAT_KJ_KGK = Decimal("4.18")  # kJ/(kg·K) for water
KJ_H_TO_KW_DIVISOR = Decimal("3600")     # 1 kW = 3600 kJ/h → (m³/h × kg/m³ × kJ/kg·K × K) = kJ/h ÷ 3600 = kW

# Interpolation fallback when thermal meter is down
ELECTRICAL_TO_THERMAL_RECOVERY = Decimal("0.70")  # 70% of IT load recoverable as heat

# Modes where heat export counts
EXPORT_MODES = {"EXPORT", "MIXED"}

# Data quality
MISSING_DATA_THRESHOLD = Decimal("0.05")
NEGATIVE_DT_THRESHOLD = Decimal("-0.5")  # °C — anything below this is flagged as anomaly


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class ThermalReading:
    """Single 5-minute thermal interval from Stream B."""
    timestamp: datetime
    flow_m3h: Decimal        # volumetric flow rate (m³/h)
    temp_supply: Decimal     # TT-HXsOut — hot side out (°C)
    temp_return: Decimal     # TT-HXsIn — cold side in (°C)
    quality: str = "GOOD"


@dataclass
class ModeEvent:
    """Mode state change from Stream B events API."""
    timestamp: datetime
    mode: str                # EXPORT, MIXED, RECIRCULATE, BYPASS, etc.


@dataclass
class ThermalDataQuality:
    """Quality assessment of thermal meter data."""
    total_expected_intervals: int = 0
    total_received_intervals: int = 0
    missing_intervals: int = 0
    missing_pct: Decimal = Decimal("0")
    bad_quality_intervals: int = 0
    negative_dt_intervals: int = 0
    interpolated_intervals: int = 0
    needs_manual_review: bool = False
    flags: List[str] = field(default_factory=list)


@dataclass
class DailyHeatExport:
    """Daily heat export total for trending."""
    date: date
    kwht: Decimal
    hours_exporting: Decimal
    avg_dt_k: Decimal
    avg_flow_m3h: Decimal


@dataclass
class KWhtResult:
    """Complete thermal billing calculation output."""
    site_id: str
    period_start: date
    period_end: date
    currency: str = "USD"

    # Thermal energy
    total_kwht: Decimal = Decimal("0")
    total_export_hours: Decimal = Decimal("0")
    avg_thermal_kw: Decimal = Decimal("0")
    peak_thermal_kw: Decimal = Decimal("0")
    avg_delta_t: Decimal = Decimal("0")
    avg_flow_m3h: Decimal = Decimal("0")

    # Host billing
    heat_pricing_model: str = ""
    heat_credit_amount: Decimal = Decimal("0")
    host_energy_rate: Decimal = Decimal("0")
    displacement_efficiency: Decimal = Decimal("0")
    revenue_share_pct: Decimal = Decimal("0")
    revenue_share_amount: Decimal = Decimal("0")
    heat_value_at_avoided_cost: Decimal = Decimal("0")
    total_host_value: Decimal = Decimal("0")

    # Budget neutral check
    budget_neutral_threshold: Decimal = Decimal("0")
    budget_neutral_met: bool = True
    budget_neutral_shortfall: Decimal = Decimal("0")

    # ESG preview (detailed in Task 6)
    co2_offset_kg: Decimal = Decimal("0")

    # Daily profile
    daily_exports: List[DailyHeatExport] = field(default_factory=list)

    # Data quality
    quality: ThermalDataQuality = field(default_factory=ThermalDataQuality)

    # Per-interval detail (optional, for audit)
    interval_count: int = 0


# ─────────────────────────────────────────────
# Stream B API extensions (thermal endpoints)
# ─────────────────────────────────────────────

class StreamBClient:
    """
    HTTP client for Stream B's REST API.
    Reuses the same pattern as Task 2.
    """

    def __init__(self, base_url: str = "http://localhost:8001/api/v1", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                timeout=30.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_telemetry(
        self, sensor_id: str, start: str, end: str, agg: str = "5min",
    ) -> List[Dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get(
            "/telemetry",
            params={"sensor_id": sensor_id, "start": start, "end": end, "agg": agg},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_mode_events(
        self, block_id: str, start: str, end: str,
    ) -> List[Dict[str, Any]]:
        """GET /events?block_id=X&type=mode_change&start=T1&end=T2"""
        client = await self._get_client()
        resp = await client.get(
            "/events",
            params={
                "block_id": block_id,
                "type": "mode_change",
                "start": start,
                "end": end,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_telemetry_latest(self, block_id: str) -> Dict[str, Any]:
        client = await self._get_client()
        resp = await client.get("/telemetry/latest", params={"block_id": block_id})
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────
# Stub data
# ─────────────────────────────────────────────

class StubStreamBClient(StreamBClient):
    """Synthetic thermal data for testing."""

    def __init__(self):
        super().__init__(base_url="stub://")

    async def get_telemetry(
        self, sensor_id: str, start: str, end: str, agg: str = "5min",
    ) -> List[Dict[str, Any]]:
        import random
        random.seed(hash(sensor_id + start) % 2**31)

        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

        readings = []
        t = start_dt

        # Determine sensor type from tag
        is_flow = "FT" in sensor_id
        is_temp_supply = "HXsOut" in sensor_id or "supply" in sensor_id.lower()
        is_temp_return = "HXsIn" in sensor_id or "return" in sensor_id.lower()

        while t < end_dt:
            hour = t.hour
            # Brewing load varies by time of day — more hot water demand 06:00-18:00
            brew_factor = 1.0 + 0.3 * math.sin(math.pi * max(0, hour - 6) / 12) if 6 <= hour <= 18 else 0.7
            noise = random.gauss(0, 0.02)

            if is_flow:
                # Flow rate ~8-12 m³/h during export
                value = round((10.0 * brew_factor + random.gauss(0, 0.5)), 3)
            elif is_temp_supply:
                # Supply temp ~38-42°C (heat from servers)
                value = round(40.0 + random.gauss(0, 0.5), 2)
            elif is_temp_return:
                # Return temp ~25-28°C
                value = round(26.5 + random.gauss(0, 0.3), 2)
            else:
                value = round(50 + random.gauss(0, 2), 3)

            quality = "GOOD"
            if random.random() < 0.0005:
                quality = "BAD"

            readings.append({
                "timestamp": t.isoformat(),
                "value": value,
                "quality": quality,
            })
            t += timedelta(minutes=INTERVAL_MINUTES)

        return readings

    async def get_mode_events(
        self, block_id: str, start: str, end: str,
    ) -> List[Dict[str, Any]]:
        """Simulate: system is in EXPORT mode ~90% of the time."""
        import random
        random.seed(42)

        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

        events = [{"timestamp": start_dt.isoformat(), "mode": "EXPORT"}]
        t = start_dt + timedelta(hours=random.randint(48, 96))

        while t < end_dt:
            # Occasional switch to RECIRCULATE (maintenance) then back to EXPORT
            events.append({"timestamp": t.isoformat(), "mode": "RECIRCULATE"})
            t += timedelta(hours=random.randint(1, 4))
            if t < end_dt:
                events.append({"timestamp": t.isoformat(), "mode": "EXPORT"})
            t += timedelta(hours=random.randint(48, 168))

        return events


# ─────────────────────────────────────────────
# Core calculator
# ─────────────────────────────────────────────

class KWhtCalculator:
    """
    Calculates thermal energy export (kWht) and host credits
    for a site over a billing period.

    Steps:
      1. Load host agreement (pricing model, rates, thresholds)
      2. Fetch thermal meter readings (flow + temp pair → kWt)
      3. Fetch mode events — only count export in EXPORT/MIXED modes
      4. Calculate kWht interval by interval
      5. Apply host pricing model (credit or revenue share)
      6. Verify budget-neutral promise
    """

    def __init__(self, api_client: StreamBClient, session: Session):
        self.api = api_client
        self.session = session

    async def calculate(
        self,
        site_id: str,
        year: int,
        month: int,
        colo_revenue: Optional[Decimal] = None,
    ) -> KWhtResult:
        """
        Main entry point.

        Args:
            site_id: Site identifier
            year, month: Billing period
            colo_revenue: Total colo revenue for the month (needed for revenue share model).
                          If None, will attempt to calculate from contracts.
        """

        # ── 1. Host agreement ──
        host_agreement = get_host_agreement_for_site(self.session, site_id)
        if host_agreement is None:
            raise ValueError(f"No active host agreement for site {site_id}")

        contract = host_agreement.contract

        # ── Period bounds ──
        period_start = date(year, month, 1)
        if month == 12:
            period_end = date(year + 1, 1, 1)
        else:
            period_end = date(year, month + 1, 1)

        start_iso = datetime(year, month, 1, tzinfo=timezone.utc).isoformat()
        end_iso = datetime(
            period_end.year, period_end.month, period_end.day,
            tzinfo=timezone.utc,
        ).isoformat()

        result = KWhtResult(
            site_id=site_id,
            period_start=period_start,
            period_end=period_end,
            currency=contract.rate_schedules[0].currency if contract.rate_schedules else "USD",
            heat_pricing_model=host_agreement.heat_pricing_model.value,
            host_energy_rate=host_agreement.host_energy_rate,
            displacement_efficiency=host_agreement.displacement_efficiency,
            revenue_share_pct=host_agreement.revenue_share_pct,
            budget_neutral_threshold=host_agreement.budget_neutral_threshold,
        )

        # ── 2. Fetch thermal meter readings ──
        # Sensor tags from Stream A point schedule:
        #   FT-BIL  — billing-grade flow meter (m³/h)
        #   TT-HXsOut — HX secondary supply temp (°C)
        #   TT-HXsIn  — HX secondary return temp (°C)
        flow_data = await self.api.get_telemetry("FT-BIL", start_iso, end_iso)
        temp_supply_data = await self.api.get_telemetry("TT-HXsOut", start_iso, end_iso)
        temp_return_data = await self.api.get_telemetry("TT-HXsIn", start_iso, end_iso)

        # ── 3. Fetch mode events ──
        # We need to know the block_id — get from contracts at this site
        block_ids = set()
        for c in get_active_contracts_for_site(self.session, site_id):
            for ra in c.rack_assignments:
                block_ids.add(ra.block_id)

        # If no rack assignments found, use site_id as block proxy
        if not block_ids:
            block_ids = {f"{site_id}-BLK-01"}

        all_mode_events: List[ModeEvent] = []
        for block_id in block_ids:
            raw_events = await self.api.get_mode_events(block_id, start_iso, end_iso)
            for e in raw_events:
                all_mode_events.append(ModeEvent(
                    timestamp=datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")),
                    mode=e["mode"],
                ))

        all_mode_events.sort(key=lambda e: e.timestamp)

        # ── 4. Align readings by timestamp ──
        thermal_readings = self._align_readings(flow_data, temp_supply_data, temp_return_data)

        # ── 5. Data quality ──
        total_minutes = (period_end - period_start).days * 24 * 60
        expected_intervals = total_minutes // INTERVAL_MINUTES
        result.quality = self._assess_quality(thermal_readings, expected_intervals)

        # ── 6. Calculate kWht interval by interval ──
        (
            result.total_kwht,
            result.peak_thermal_kw,
            result.avg_delta_t,
            result.avg_flow_m3h,
            result.total_export_hours,
            result.daily_exports,
            result.interval_count,
        ) = self._calculate_kwht(thermal_readings, all_mode_events, period_start, period_end)

        if result.total_export_hours > 0:
            result.avg_thermal_kw = (
                result.total_kwht / result.total_export_hours
            ).quantize(Decimal("0.01"), ROUND_HALF_UP)

        # ── 7. Apply host pricing model ──
        self._apply_pricing(result, host_agreement, colo_revenue)

        # ── 8. CO₂ offset preview ──
        result.co2_offset_kg = (
            result.total_kwht
            * host_agreement.grid_emission_factor
            * (Decimal("1") - host_agreement.host_existing_efficiency)
        ).quantize(Decimal("0.01"), ROUND_HALF_UP)

        return result

    def _align_readings(
        self,
        flow_data: List[Dict],
        temp_supply_data: List[Dict],
        temp_return_data: List[Dict],
    ) -> List[ThermalReading]:
        """
        Align three sensor streams by timestamp into ThermalReading objects.
        Missing sensors at a timestamp → skip that interval (count as missing).
        """
        # Index by timestamp
        flow_map = {r["timestamp"]: r for r in flow_data}
        supply_map = {r["timestamp"]: r for r in temp_supply_data}
        return_map = {r["timestamp"]: r for r in temp_return_data}

        # Union of all timestamps
        all_ts = sorted(set(flow_map.keys()) | set(supply_map.keys()) | set(return_map.keys()))

        readings = []
        for ts in all_ts:
            f = flow_map.get(ts)
            s = supply_map.get(ts)
            r = return_map.get(ts)

            if f is None or s is None or r is None:
                continue  # Incomplete triple — skip

            # Worst quality wins
            qualities = [f.get("quality", "GOOD"), s.get("quality", "GOOD"), r.get("quality", "GOOD")]
            quality = "BAD" if "BAD" in qualities else ("SUSPECT" if "SUSPECT" in qualities else "GOOD")

            readings.append(ThermalReading(
                timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")),
                flow_m3h=Decimal(str(f["value"])),
                temp_supply=Decimal(str(s["value"])),
                temp_return=Decimal(str(r["value"])),
                quality=quality,
            ))

        return readings

    def _get_mode_at_time(self, mode_events: List[ModeEvent], t: datetime) -> str:
        """Get the active mode at a given timestamp (last event before t)."""
        active_mode = "UNKNOWN"
        for event in mode_events:
            if event.timestamp <= t:
                active_mode = event.mode
            else:
                break
        return active_mode

    def _calculate_kwht(
        self,
        readings: List[ThermalReading],
        mode_events: List[ModeEvent],
        period_start: date,
        period_end: date,
    ) -> Tuple[Decimal, Decimal, Decimal, Decimal, Decimal, List[DailyHeatExport], int]:
        """
        Core kWht calculation.

        For each 5-min interval:
          instantaneous_kWt = flow_m3h × 997 × 4.18 × ΔT / 3.6
          interval_kWht = kWt × (5/60)

        Only counted when mode ∈ {EXPORT, MIXED}.
        Negative ΔT → zero export, flagged as anomaly.
        """
        total_kwht = Decimal("0")
        peak_kw = Decimal("0")
        dt_sum = Decimal("0")
        flow_sum = Decimal("0")
        export_intervals = 0
        counted = 0

        # Daily accumulators
        daily: Dict[date, Dict[str, Decimal]] = {}

        for reading in readings:
            if reading.quality == "BAD":
                continue

            # Check mode
            mode = self._get_mode_at_time(mode_events, reading.timestamp)
            if mode not in EXPORT_MODES:
                continue

            # ΔT
            delta_t = reading.temp_supply - reading.temp_return

            # Handle negative ΔT (reverse flow anomaly)
            if delta_t < NEGATIVE_DT_THRESHOLD:
                # Flag but treat as zero
                continue

            # Clamp small negatives to zero
            if delta_t < 0:
                delta_t = Decimal("0")

            # Instantaneous thermal power (kWt)
            # kWt = flow_m3h × 997 × 4.18 × ΔT_K / 3600
            kw_t = (
                reading.flow_m3h
                * WATER_DENSITY_KG_M3
                * SPECIFIC_HEAT_KJ_KGK
                * delta_t
                / KJ_H_TO_KW_DIVISOR
            )

            # Interval energy
            interval_kwht = kw_t * INTERVAL_HOURS

            total_kwht += interval_kwht
            export_intervals += 1

            if kw_t > peak_kw:
                peak_kw = kw_t

            dt_sum += delta_t
            flow_sum += reading.flow_m3h
            counted += 1

            # Daily accumulator
            day = reading.timestamp.date()
            if day not in daily:
                daily[day] = {"kwht": Decimal("0"), "intervals": 0, "dt_sum": Decimal("0"), "flow_sum": Decimal("0")}
            daily[day]["kwht"] += interval_kwht
            daily[day]["intervals"] += 1
            daily[day]["dt_sum"] += delta_t
            daily[day]["flow_sum"] += reading.flow_m3h

        # Averages
        avg_dt = (dt_sum / Decimal(str(counted))).quantize(Decimal("0.01"), ROUND_HALF_UP) if counted > 0 else Decimal("0")
        avg_flow = (flow_sum / Decimal(str(counted))).quantize(Decimal("0.01"), ROUND_HALF_UP) if counted > 0 else Decimal("0")

        total_kwht = total_kwht.quantize(Decimal("0.01"), ROUND_HALF_UP)
        peak_kw = peak_kw.quantize(Decimal("0.01"), ROUND_HALF_UP)
        export_hours = (Decimal(str(export_intervals)) * INTERVAL_HOURS).quantize(Decimal("0.01"), ROUND_HALF_UP)

        # Build daily export list
        daily_exports = []
        d = period_start
        while d < period_end:
            dd = daily.get(d)
            if dd and dd["intervals"] > 0:
                daily_exports.append(DailyHeatExport(
                    date=d,
                    kwht=dd["kwht"].quantize(Decimal("0.01"), ROUND_HALF_UP),
                    hours_exporting=(Decimal(str(dd["intervals"])) * INTERVAL_HOURS).quantize(Decimal("0.01"), ROUND_HALF_UP),
                    avg_dt_k=(dd["dt_sum"] / Decimal(str(dd["intervals"]))).quantize(Decimal("0.01"), ROUND_HALF_UP),
                    avg_flow_m3h=(dd["flow_sum"] / Decimal(str(dd["intervals"]))).quantize(Decimal("0.01"), ROUND_HALF_UP),
                ))
            else:
                daily_exports.append(DailyHeatExport(
                    date=d, kwht=Decimal("0"), hours_exporting=Decimal("0"),
                    avg_dt_k=Decimal("0"), avg_flow_m3h=Decimal("0"),
                ))
            d += timedelta(days=1)

        return total_kwht, peak_kw, avg_dt, avg_flow, export_hours, daily_exports, counted

    def _apply_pricing(
        self,
        result: KWhtResult,
        agreement: HostAgreement,
        colo_revenue: Optional[Decimal],
    ):
        """Apply the host's pricing model to the calculated kWht."""

        if agreement.heat_pricing_model == HeatPricingModel.CREDIT:
            # Credit = kWht × host_energy_rate × displacement_efficiency
            result.heat_credit_amount = (
                result.total_kwht
                * agreement.host_energy_rate
                * agreement.displacement_efficiency
            ).quantize(Decimal("0.01"), ROUND_HALF_UP)

            result.total_host_value = result.heat_credit_amount

        elif agreement.heat_pricing_model == HeatPricingModel.REVENUE_SHARE:
            # Revenue share = colo_revenue × share_pct
            if colo_revenue is None:
                # Attempt to estimate from site contracts
                colo_revenue = self._estimate_colo_revenue(result.site_id)

            result.revenue_share_amount = (
                colo_revenue * agreement.revenue_share_pct
            ).quantize(Decimal("0.01"), ROUND_HALF_UP)

            # Plus heat value at avoided cost
            result.heat_value_at_avoided_cost = (
                result.total_kwht
                * agreement.host_energy_rate
                * agreement.displacement_efficiency
            ).quantize(Decimal("0.01"), ROUND_HALF_UP)

            result.total_host_value = result.revenue_share_amount + result.heat_value_at_avoided_cost

        # Budget-neutral check
        if agreement.budget_neutral_threshold > 0:
            if result.total_host_value < agreement.budget_neutral_threshold:
                result.budget_neutral_met = False
                result.budget_neutral_shortfall = (
                    agreement.budget_neutral_threshold - result.total_host_value
                ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    def _estimate_colo_revenue(self, site_id: str) -> Decimal:
        """
        Fallback: estimate monthly colo revenue from active contracts.
        In production, Task 2's results feed this directly.
        """
        contracts = get_active_contracts_for_site(self.session, site_id)
        total = Decimal("0")
        for c in contracts:
            if c.contract_type == ContractType.COLO_MSA:
                for ra in c.rack_assignments:
                    rate = get_rate(self.session, c.id, RateType.COLO_PER_KW, date.today())
                    if rate:
                        total += ra.committed_kw * rate.rate_value
        return total

    def _assess_quality(
        self,
        readings: List[ThermalReading],
        expected_intervals: int,
    ) -> ThermalDataQuality:
        """Assess thermal data quality."""
        q = ThermalDataQuality()
        q.total_expected_intervals = expected_intervals
        q.total_received_intervals = len(readings)
        q.missing_intervals = max(0, expected_intervals - len(readings))

        if expected_intervals > 0:
            q.missing_pct = (
                Decimal(str(q.missing_intervals)) / Decimal(str(expected_intervals))
            ).quantize(Decimal("0.0001"), ROUND_HALF_UP)

        q.bad_quality_intervals = sum(1 for r in readings if r.quality == "BAD")
        q.negative_dt_intervals = sum(
            1 for r in readings
            if (r.temp_supply - r.temp_return) < NEGATIVE_DT_THRESHOLD
        )

        if q.missing_pct > MISSING_DATA_THRESHOLD:
            q.needs_manual_review = True
            q.flags.append(
                f"Missing data: {q.missing_pct * 100:.1f}% "
                f"({q.missing_intervals} of {expected_intervals} intervals)"
            )

        if q.negative_dt_intervals > 0:
            q.flags.append(
                f"Negative ΔT anomalies: {q.negative_dt_intervals} intervals (treated as zero export)"
            )

        if q.bad_quality_intervals > 0:
            q.flags.append(f"Bad quality readings: {q.bad_quality_intervals} intervals")

        return q


# ─────────────────────────────────────────────
# Thermal meter downtime interpolation
# ─────────────────────────────────────────────

async def interpolate_from_electrical(
    api_client: StreamBClient,
    block_id: str,
    start_iso: str,
    end_iso: str,
) -> Decimal:
    """
    Fallback when thermal meter is down:
    Estimate kWht from electrical load × recovery factor.

    kWht_est = IT_kWh × 0.70
    """
    readings = await api_client.get_telemetry(
        sensor_id=f"MET-01",
        start=start_iso,
        end=end_iso,
    )
    total_kwh = sum(
        Decimal(str(r.get("avg_kw", r.get("value", 0)))) * INTERVAL_HOURS
        for r in readings
        if r.get("quality", "GOOD") != "BAD"
    )
    return (total_kwh * ELECTRICAL_TO_THERMAL_RECOVERY).quantize(Decimal("0.01"), ROUND_HALF_UP)


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """Integration test with stub data."""
    from unittest.mock import MagicMock

    print("=" * 60)
    print("kWht Heat Credit Calculator — Stub Test")
    print("=" * 60)

    # ── Mock session & DB objects ──
    mock_session = MagicMock(spec=Session)

    mock_contract = MagicMock(spec=Contract)
    mock_contract.id = 1
    mock_contract.contract_type = ContractType.HOST_AGREEMENT
    mock_contract.site_id = "BALD-01"
    mock_contract.status = ContractStatus.ACTIVE
    mock_contract.rack_assignments = []
    mock_contract.rate_schedules = []

    mock_agreement = MagicMock(spec=HostAgreement)
    mock_agreement.contract = mock_contract
    mock_agreement.heat_pricing_model = HeatPricingModel.CREDIT
    mock_agreement.host_energy_rate = Decimal("0.065")     # $0.065/kWh gas equivalent
    mock_agreement.displacement_efficiency = Decimal("0.85")
    mock_agreement.revenue_share_pct = Decimal("0.05")
    mock_agreement.budget_neutral_threshold = Decimal("0")
    mock_agreement.host_existing_efficiency = Decimal("0.85")  # gas boiler
    mock_agreement.grid_emission_factor = Decimal("0.24")      # US-NY

    # Patch module-level functions
    import sys
    this_module = sys.modules[__name__]

    _orig_get_host = get_host_agreement_for_site
    _orig_get_contracts = get_active_contracts_for_site

    this_module.get_host_agreement_for_site = lambda s, sid: mock_agreement
    this_module.get_active_contracts_for_site = lambda s, sid: [mock_contract]

    try:
        stub_api = StubStreamBClient()
        calculator = KWhtCalculator(stub_api, mock_session)
        result = await calculator.calculate(site_id="BALD-01", year=2026, month=1)

        # ── Display results ──
        print(f"\nPeriod: {result.period_start} → {result.period_end}")
        print(f"Pricing model: {result.heat_pricing_model}")
        print()
        print(f"Total kWht exported: {result.total_kwht:>12,.1f}")
        print(f"Export hours:        {result.total_export_hours:>12,.1f}")
        print(f"Avg thermal kW:      {result.avg_thermal_kw:>12,.1f}")
        print(f"Peak thermal kW:     {result.peak_thermal_kw:>12,.1f}")
        print(f"Avg ΔT:              {result.avg_delta_t:>12,.1f} K")
        print(f"Avg flow:            {result.avg_flow_m3h:>12,.1f} m³/h")
        print()
        print(f"─── Host Billing ───────────────────────────────────")
        print(f"  Heat credit:       ${result.heat_credit_amount:>10,.2f}")
        print(f"  Host energy rate:    ${result.host_energy_rate}/kWh")
        print(f"  Displacement eff:    {result.displacement_efficiency}")
        print(f"  Total host value:  ${result.total_host_value:>10,.2f}")
        print(f"  Budget neutral:      {result.budget_neutral_met}")
        print()
        print(f"─── ESG Preview ────────────────────────────────────")
        print(f"  CO₂ offset:        {result.co2_offset_kg:>10,.1f} kg")
        print()

        # Daily summary (first 5 days + last day)
        print(f"─── Daily Profile (first 5 days) ───────────────────")
        for d in result.daily_exports[:5]:
            print(f"  {d.date}:  {d.kwht:>8,.1f} kWht  |  {d.hours_exporting:>5.1f}h  |  ΔT {d.avg_dt_k:.1f}K  |  {d.avg_flow_m3h:.1f} m³/h")
        print(f"  ... ({len(result.daily_exports)} days total)")
        print()

        # Quality
        q = result.quality
        print(f"─── Data Quality ───────────────────────────────────")
        print(f"  Expected:   {q.total_expected_intervals}")
        print(f"  Received:   {q.total_received_intervals}")
        print(f"  Missing:    {q.missing_intervals} ({q.missing_pct * 100:.2f}%)")
        print(f"  Bad:        {q.bad_quality_intervals}")
        print(f"  Neg ΔT:     {q.negative_dt_intervals}")
        if q.flags:
            for flag in q.flags:
                print(f"    ⚠ {flag}")

        # ── Assertions ──
        print()
        assert result.total_kwht > 0, "kWht should be > 0"
        assert result.heat_credit_amount > 0, "Heat credit should be > 0"
        assert result.avg_delta_t > Decimal("10"), "ΔT should be > 10K (supply ~40°C, return ~26°C)"
        assert len(result.daily_exports) == 31, "January should have 31 days"
        assert result.co2_offset_kg > 0, "CO₂ offset should be > 0"

        # Sanity: ~700kWt target × ~700 export hours ≈ ~490,000 kWht
        # Stub generates ~10 m³/h × 13.5K ΔT → ~155 kWt → lower than real site
        # With ~90% export time: ~155kW × 670h ≈ ~104,000 kWht
        assert float(result.total_kwht) > 50_000, f"kWht {result.total_kwht} seems too low"

        print("✓ All assertions passed")

    finally:
        this_module.get_host_agreement_for_site = _orig_get_host
        this_module.get_active_contracts_for_site = _orig_get_contracts
        await stub_api.close()


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
