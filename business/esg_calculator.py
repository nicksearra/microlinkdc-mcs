"""
MCS Stream C — Task 6: ESG / Carbon Calculator
================================================
Service that calculates environmental metrics for reporting:
CO₂ offsets, Scope 1/2 emissions, PUE, WUE, net carbon position,
and NYSERDA Carbon Neutral Brewery alignment.

Consumes Stream B telemetry + Task 3 kWht data.

Usage:
    calculator = ESGCalculator(api_client, session)
    report = await calculator.calculate(site_id="BALD-01", year=2026, month=1)
    annual = await calculator.calculate_annual(site_id="BALD-01", year=2026)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any

import httpx

# Local imports
from billing_models import (
    Session, EmissionFactor,
    get_host_agreement_for_site, get_active_contracts_for_site,
)
from kwht_calculator import KWhtCalculator, KWhtResult, StreamBClient


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

INTERVAL_MINUTES = 5
INTERVAL_HOURS = Decimal(str(INTERVAL_MINUTES)) / Decimal("60")

# Diesel emission factor (kg CO₂ per litre)
DIESEL_EMISSION_FACTOR = Decimal("2.68")

# Default emission factors (fallback if DB lookup fails)
DEFAULT_EMISSION_FACTORS = {
    "US-NY": Decimal("0.2400"),
    "US-AVG": Decimal("0.3900"),
    "EU": Decimal("0.2300"),
    "ZA": Decimal("0.9500"),
    "UK": Decimal("0.2100"),
}

# NYSERDA CNB programme metrics
NYSERDA_CNB_TARGETS = {
    "heat_recovery_efficiency_min": Decimal("0.60"),   # min % of waste heat recovered
    "pue_target": Decimal("1.20"),                      # max PUE
    "renewable_energy_pct_target": Decimal("0.50"),     # 50% renewable by 2028
    "co2_reduction_target_pct": Decimal("0.25"),        # 25% vs baseline
}


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class ScopeEmissions:
    """Greenhouse gas emissions by scope."""
    scope_1_kg: Decimal = Decimal("0")     # direct (diesel gen)
    scope_2_kg: Decimal = Decimal("0")     # electricity (grid)
    scope_3_kg: Decimal = Decimal("0")     # upstream (out of scope initially)
    total_kg: Decimal = Decimal("0")

    # Detail
    diesel_litres: Decimal = Decimal("0")
    generator_runtime_hours: Decimal = Decimal("0")
    total_facility_kwh: Decimal = Decimal("0")
    it_kwh: Decimal = Decimal("0")


@dataclass
class CarbonOffset:
    """Heat recovery CO₂ offset calculation."""
    kwht_exported: Decimal = Decimal("0")
    grid_emission_factor: Decimal = Decimal("0")
    host_existing_efficiency: Decimal = Decimal("0")
    offset_kg: Decimal = Decimal("0")
    methodology: str = ""


@dataclass
class EfficiencyMetrics:
    """Operational efficiency metrics."""
    pue: Decimal = Decimal("0")            # Power Usage Effectiveness
    wue: Decimal = Decimal("0")            # Water Usage Effectiveness (L/kWh)
    it_kwh: Decimal = Decimal("0")
    total_facility_kwh: Decimal = Decimal("0")
    cooling_kwh: Decimal = Decimal("0")
    water_litres: Decimal = Decimal("0")
    heat_recovery_pct: Decimal = Decimal("0")  # % of waste heat captured


@dataclass
class MonthlyESGSnapshot:
    """Single month of ESG data for trending."""
    year: int
    month: int
    pue: Decimal = Decimal("0")
    wue: Decimal = Decimal("0")
    scope_1_kg: Decimal = Decimal("0")
    scope_2_kg: Decimal = Decimal("0")
    heat_offset_kg: Decimal = Decimal("0")
    net_carbon_kg: Decimal = Decimal("0")
    kwht_exported: Decimal = Decimal("0")
    renewable_pct: Decimal = Decimal("0")


@dataclass
class NYSERDAReport:
    """NYSERDA Carbon Neutral Brewery alignment metrics."""
    programme: str = "NYSERDA Carbon Neutral Brewery"
    site_id: str = ""
    period: str = ""
    heat_recovery_efficiency: Decimal = Decimal("0")
    heat_recovery_target_met: bool = False
    pue: Decimal = Decimal("0")
    pue_target_met: bool = False
    renewable_energy_pct: Decimal = Decimal("0")
    renewable_target_met: bool = False
    co2_reduction_vs_baseline_pct: Decimal = Decimal("0")
    co2_reduction_target_met: bool = False
    overall_alignment: str = "partial"
    notes: List[str] = field(default_factory=list)


@dataclass
class ESGReport:
    """Complete ESG report for a site/period."""
    site_id: str
    period_start: date
    period_end: date
    period_type: str  # "month" or "year"

    # Core metrics
    emissions: ScopeEmissions = field(default_factory=ScopeEmissions)
    offset: CarbonOffset = field(default_factory=CarbonOffset)
    efficiency: EfficiencyMetrics = field(default_factory=EfficiencyMetrics)

    # Net position
    net_carbon_kg: Decimal = Decimal("0")
    carbon_negative: bool = False

    # Renewable
    renewable_energy_pct: Decimal = Decimal("0")
    renewable_source: str = ""  # PPA / REC / on-site

    # NYSERDA
    nyserda: Optional[NYSERDAReport] = None

    # 12-month rolling trend
    trend: List[MonthlyESGSnapshot] = field(default_factory=list)

    # Methodology documentation
    methodology: Dict[str, str] = field(default_factory=dict)

    # Region
    region_code: str = ""
    grid_emission_factor: Decimal = Decimal("0")


# ─────────────────────────────────────────────
# Stub client (extends base for generator events + water)
# ─────────────────────────────────────────────

class StubStreamBClient(StreamBClient):
    """Synthetic data for ESG testing."""

    def __init__(self):
        super().__init__(base_url="stub://")

    async def get_telemetry(
        self, sensor_id: str, start: str, end: str, agg: str = "5min",
    ) -> List[Dict[str, Any]]:
        import random, math
        random.seed(hash(sensor_id + start) % 2**31)

        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

        readings = []
        t = start_dt

        while t < end_dt:
            hour = t.hour

            if "MET-01" in sensor_id or "facility" in sensor_id.lower():
                # Facility total power ~1050-1150 kW (IT + cooling)
                value = round(1100 + random.gauss(0, 20), 3)
            elif "IT" in sensor_id or "PDU" in sensor_id:
                # IT load ~950-1000 kW
                value = round(975 + random.gauss(0, 15), 3)
            elif "FT-SPRAY" in sensor_id or "spray" in sensor_id.lower():
                # Adiabatic spray: 0 at night/cool, ~5-15 L/h when hot
                if 10 <= hour <= 18 and t.month in (5, 6, 7, 8, 9):
                    value = round(10 + random.gauss(0, 2), 3)
                else:
                    value = 0.0
            elif "FT" in sensor_id:
                # Flow meter — thermal
                brew_factor = 1.0 + 0.3 * math.sin(math.pi * max(0, hour - 6) / 12) if 6 <= hour <= 18 else 0.7
                value = round(10.0 * brew_factor + random.gauss(0, 0.5), 3)
            elif "HXsOut" in sensor_id:
                value = round(40.0 + random.gauss(0, 0.5), 2)
            elif "HXsIn" in sensor_id:
                value = round(26.5 + random.gauss(0, 0.3), 2)
            else:
                value = round(50 + random.gauss(0, 2), 3)

            readings.append({
                "timestamp": t.isoformat(),
                "value": value,
                "quality": "GOOD",
            })
            t += timedelta(minutes=INTERVAL_MINUTES)

        return readings

    async def get_mode_events(
        self, block_id: str, start: str, end: str,
    ) -> List[Dict[str, Any]]:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return [{"timestamp": start_dt.isoformat(), "mode": "EXPORT"}]

    async def get_events(
        self, block_id: str, event_type: str, start: str, end: str,
    ) -> List[Dict[str, Any]]:
        """Generator runtime events — simulate 2 hours of gen run in the month."""
        import random
        random.seed(99)
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))

        gen_start = start_dt + timedelta(days=12, hours=3)
        gen_end = gen_start + timedelta(hours=2)

        return [
            {"timestamp": gen_start.isoformat(), "mode": "REJECT", "generator": True,
             "diesel_litres_per_hour": 250},
            {"timestamp": gen_end.isoformat(), "mode": "NORMAL", "generator": False},
        ]


# ─────────────────────────────────────────────
# Core calculator
# ─────────────────────────────────────────────

class ESGCalculator:
    """
    Calculates environmental metrics for a site over a period.

    Metrics:
      1. Heat offset CO₂
      2. Scope 1 (diesel)
      3. Scope 2 (grid electricity)
      4. PUE
      5. WUE
      6. Net carbon position
      7. Renewable energy %
      8. NYSERDA CNB alignment
    """

    def __init__(self, api_client: StreamBClient, session: Session):
        self.api = api_client
        self.session = session
        self.kwht_calc = KWhtCalculator(api_client, session)

    async def calculate(
        self,
        site_id: str,
        year: int,
        month: int,
    ) -> ESGReport:
        """Calculate ESG metrics for a single month."""

        period_start = date(year, month, 1)
        period_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        start_iso = datetime(year, month, 1, tzinfo=timezone.utc).isoformat()
        end_iso = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc).isoformat()

        # ── Host agreement (for emission factor + host efficiency) ──
        host_agreement = get_host_agreement_for_site(self.session, site_id)

        if host_agreement:
            grid_factor = host_agreement.grid_emission_factor
            host_efficiency = host_agreement.host_existing_efficiency
            region_code = self._infer_region(site_id, grid_factor)
        else:
            region_code = self._infer_region(site_id)
            grid_factor = self._get_emission_factor(region_code, year)
            host_efficiency = Decimal("0.85")

        report = ESGReport(
            site_id=site_id,
            period_start=period_start,
            period_end=period_end,
            period_type="month",
            region_code=region_code,
            grid_emission_factor=grid_factor,
        )

        # ── 1. Heat offset CO₂ ──
        kwht_result = await self.kwht_calc.calculate(site_id, year, month)

        report.offset = CarbonOffset(
            kwht_exported=kwht_result.total_kwht,
            grid_emission_factor=grid_factor,
            host_existing_efficiency=host_efficiency,
            offset_kg=(
                kwht_result.total_kwht * grid_factor * (Decimal("1") - host_efficiency)
            ).quantize(Decimal("0.01"), ROUND_HALF_UP),
            methodology=(
                f"CO2_offset = kWht_exported × grid_emission_factor × (1 - host_efficiency)\n"
                f"= {kwht_result.total_kwht:,.1f} × {grid_factor} × (1 - {host_efficiency})\n"
                f"Grid factor source: regional grid average ({region_code})\n"
                f"Host efficiency: existing heating system COP/efficiency"
            ),
        )

        # ── 2. Scope 1 — diesel generator ──
        gen_events = await self._get_generator_events(site_id, start_iso, end_iso)
        diesel_litres, gen_hours = self._calc_diesel(gen_events)

        scope1 = (diesel_litres * DIESEL_EMISSION_FACTOR).quantize(Decimal("0.01"), ROUND_HALF_UP)

        # ── 3. Scope 2 — grid electricity ──
        facility_kwh = await self._get_facility_kwh(site_id, start_iso, end_iso)
        it_kwh = await self._get_it_kwh(site_id, start_iso, end_iso)

        scope2 = (facility_kwh * grid_factor).quantize(Decimal("0.01"), ROUND_HALF_UP)

        report.emissions = ScopeEmissions(
            scope_1_kg=scope1,
            scope_2_kg=scope2,
            scope_3_kg=Decimal("0"),  # out of initial scope
            total_kg=(scope1 + scope2).quantize(Decimal("0.01")),
            diesel_litres=diesel_litres,
            generator_runtime_hours=gen_hours,
            total_facility_kwh=facility_kwh,
            it_kwh=it_kwh,
        )

        # ── 4. PUE ──
        pue = Decimal("0")
        if it_kwh > 0:
            pue = (facility_kwh / it_kwh).quantize(Decimal("0.001"), ROUND_HALF_UP)

        # ── 5. WUE ──
        water_litres = await self._get_water_consumption(site_id, start_iso, end_iso)
        wue = Decimal("0")
        if it_kwh > 0:
            wue = (water_litres / it_kwh).quantize(Decimal("0.0001"), ROUND_HALF_UP)

        # Heat recovery %
        cooling_kwh = facility_kwh - it_kwh
        heat_recovery_pct = Decimal("0")
        if cooling_kwh > 0:
            # kWht exported / total waste heat (cooling load)
            heat_recovery_pct = min(
                Decimal("1"),
                (kwht_result.total_kwht / cooling_kwh).quantize(Decimal("0.001"), ROUND_HALF_UP),
            )

        report.efficiency = EfficiencyMetrics(
            pue=pue,
            wue=wue,
            it_kwh=it_kwh,
            total_facility_kwh=facility_kwh,
            cooling_kwh=cooling_kwh,
            water_litres=water_litres,
            heat_recovery_pct=heat_recovery_pct,
        )

        # ── 6. Net carbon position ──
        report.net_carbon_kg = (
            report.emissions.total_kg - report.offset.offset_kg
        ).quantize(Decimal("0.01"), ROUND_HALF_UP)
        report.carbon_negative = report.net_carbon_kg < 0

        # ── 7. Renewable energy ──
        report.renewable_energy_pct = await self._get_renewable_pct(site_id, year)
        report.renewable_source = "PPA/REC"  # placeholder

        # ── 8. NYSERDA CNB alignment ──
        if "BALD" in site_id.upper():
            report.nyserda = self._calc_nyserda(report, kwht_result)

        # ── 9. Methodology docs ──
        report.methodology = self._build_methodology(report)

        # ── 10. Trending (placeholder — populated from historical data) ──
        report.trend = await self._get_trend(site_id, year, month)

        return report

    async def calculate_annual(
        self,
        site_id: str,
        year: int,
    ) -> ESGReport:
        """
        Calculate annual ESG metrics by aggregating monthly reports.
        """
        monthly_reports = []
        for month in range(1, 13):
            try:
                report = await self.calculate(site_id, year, month)
                monthly_reports.append(report)
            except Exception as e:
                print(f"[ESG] Skipping {year}-{month:02d}: {e}")

        if not monthly_reports:
            raise ValueError(f"No ESG data available for {site_id} in {year}")

        # Aggregate
        annual = ESGReport(
            site_id=site_id,
            period_start=date(year, 1, 1),
            period_end=date(year + 1, 1, 1),
            period_type="year",
            region_code=monthly_reports[0].region_code,
            grid_emission_factor=monthly_reports[0].grid_emission_factor,
        )

        total_scope1 = sum(r.emissions.scope_1_kg for r in monthly_reports)
        total_scope2 = sum(r.emissions.scope_2_kg for r in monthly_reports)
        total_offset = sum(r.offset.offset_kg for r in monthly_reports)
        total_facility = sum(r.efficiency.total_facility_kwh for r in monthly_reports)
        total_it = sum(r.efficiency.it_kwh for r in monthly_reports)
        total_water = sum(r.efficiency.water_litres for r in monthly_reports)
        total_kwht = sum(r.offset.kwht_exported for r in monthly_reports)
        total_diesel = sum(r.emissions.diesel_litres for r in monthly_reports)
        total_gen_hrs = sum(r.emissions.generator_runtime_hours for r in monthly_reports)

        annual.emissions = ScopeEmissions(
            scope_1_kg=total_scope1,
            scope_2_kg=total_scope2,
            total_kg=total_scope1 + total_scope2,
            diesel_litres=total_diesel,
            generator_runtime_hours=total_gen_hrs,
            total_facility_kwh=total_facility,
            it_kwh=total_it,
        )

        annual.offset = CarbonOffset(
            kwht_exported=total_kwht,
            grid_emission_factor=annual.grid_emission_factor,
            host_existing_efficiency=monthly_reports[0].offset.host_existing_efficiency,
            offset_kg=total_offset,
        )

        if total_it > 0:
            annual.efficiency = EfficiencyMetrics(
                pue=(total_facility / total_it).quantize(Decimal("0.001")),
                wue=(total_water / total_it).quantize(Decimal("0.0001")),
                it_kwh=total_it,
                total_facility_kwh=total_facility,
                cooling_kwh=total_facility - total_it,
                water_litres=total_water,
                heat_recovery_pct=min(
                    Decimal("1"),
                    (total_kwht / max(total_facility - total_it, Decimal("1"))).quantize(Decimal("0.001")),
                ),
            )

        annual.net_carbon_kg = (annual.emissions.total_kg - annual.offset.offset_kg).quantize(Decimal("0.01"))
        annual.carbon_negative = annual.net_carbon_kg < 0

        # Trend = each month
        annual.trend = [
            MonthlyESGSnapshot(
                year=r.period_start.year, month=r.period_start.month,
                pue=r.efficiency.pue, wue=r.efficiency.wue,
                scope_1_kg=r.emissions.scope_1_kg, scope_2_kg=r.emissions.scope_2_kg,
                heat_offset_kg=r.offset.offset_kg, net_carbon_kg=r.net_carbon_kg,
                kwht_exported=r.offset.kwht_exported,
                renewable_pct=r.renewable_energy_pct,
            )
            for r in monthly_reports
        ]

        annual.methodology = self._build_methodology(annual)

        return annual

    # ─────────────────────────────────────────
    # Data fetching helpers
    # ─────────────────────────────────────────

    async def _get_generator_events(
        self, site_id: str, start_iso: str, end_iso: str,
    ) -> List[Dict[str, Any]]:
        """Fetch generator runtime events from Stream B."""
        block_ids = self._get_block_ids(site_id)
        events = []
        for block_id in block_ids:
            try:
                raw = await self.api.get_events(block_id, "mode_change", start_iso, end_iso)
                events.extend(raw)
            except Exception:
                # get_events may not exist on all client stubs
                pass
        return events

    async def _get_facility_kwh(
        self, site_id: str, start_iso: str, end_iso: str,
    ) -> Decimal:
        """Total facility kWh from MET-01 (revenue-grade meter)."""
        readings = await self.api.get_telemetry("MET-01", start_iso, end_iso)
        return self._sum_kwh(readings)

    async def _get_it_kwh(
        self, site_id: str, start_iso: str, end_iso: str,
    ) -> Decimal:
        """IT-only kWh from PDU sub-meters."""
        readings = await self.api.get_telemetry("PDU-TOTAL", start_iso, end_iso)
        return self._sum_kwh(readings)

    async def _get_water_consumption(
        self, site_id: str, start_iso: str, end_iso: str,
    ) -> Decimal:
        """Adiabatic spray water consumption in litres."""
        readings = await self.api.get_telemetry("FT-SPRAY", start_iso, end_iso)
        # FT-SPRAY reports L/h — convert to total litres
        total = sum(
            Decimal(str(r.get("value", 0))) * INTERVAL_HOURS
            for r in readings
            if r.get("quality", "GOOD") != "BAD"
        )
        return total.quantize(Decimal("0.01"), ROUND_HALF_UP)

    async def _get_renewable_pct(self, site_id: str, year: int) -> Decimal:
        """
        Renewable energy percentage — from PPA/REC data.
        Placeholder: returns 0 until PPA tracking is configured.
        """
        # In production: query contract/PPA table for renewable certificates
        return Decimal("0")

    def _sum_kwh(self, readings: List[Dict]) -> Decimal:
        """Sum 5-min kW readings into kWh."""
        total = sum(
            Decimal(str(r.get("value", r.get("avg_kw", 0)))) * INTERVAL_HOURS
            for r in readings
            if r.get("quality", "GOOD") != "BAD"
        )
        return total.quantize(Decimal("0.01"), ROUND_HALF_UP)

    def _get_block_ids(self, site_id: str) -> List[str]:
        """Get block IDs for a site from active contracts."""
        block_ids = set()
        for c in get_active_contracts_for_site(self.session, site_id):
            for ra in c.rack_assignments:
                block_ids.add(ra.block_id)
        return list(block_ids) or [f"{site_id}-BLK-01"]

    def _calc_diesel(
        self, events: List[Dict],
    ) -> tuple[Decimal, Decimal]:
        """Calculate diesel consumption from generator events."""
        total_litres = Decimal("0")
        total_hours = Decimal("0")

        gen_start = None
        litres_per_hour = Decimal("250")  # default for 1MW gen

        for event in sorted(events, key=lambda e: e.get("timestamp", "")):
            if event.get("generator") and event.get("mode") == "REJECT":
                gen_start = datetime.fromisoformat(
                    event["timestamp"].replace("Z", "+00:00")
                )
                litres_per_hour = Decimal(str(event.get("diesel_litres_per_hour", 250)))
            elif gen_start and not event.get("generator"):
                gen_end = datetime.fromisoformat(
                    event["timestamp"].replace("Z", "+00:00")
                )
                hours = Decimal(str((gen_end - gen_start).total_seconds() / 3600))
                total_hours += hours
                total_litres += (hours * litres_per_hour).quantize(Decimal("0.01"))
                gen_start = None

        return total_litres, total_hours.quantize(Decimal("0.01"))

    def _get_emission_factor(self, region_code: str, year: int) -> Decimal:
        """Look up emission factor from DB or defaults."""
        try:
            from sqlalchemy import select
            q = (
                select(EmissionFactor)
                .where(
                    EmissionFactor.region_code == region_code,
                    EmissionFactor.effective_year <= year,
                )
                .order_by(EmissionFactor.effective_year.desc())
                .limit(1)
            )
            result = self.session.execute(q).scalars().first()
            if result:
                return result.factor_kg_per_kwh
        except Exception:
            pass
        return DEFAULT_EMISSION_FACTORS.get(region_code, Decimal("0.39"))

    def _infer_region(
        self, site_id: str, grid_factor: Optional[Decimal] = None,
    ) -> str:
        """Infer region code from site ID or grid factor."""
        sid = site_id.upper()
        if "BALD" in sid or "NY" in sid:
            return "US-NY"
        if "ZA" in sid or "CPT" in sid:
            return "ZA"
        if grid_factor:
            # Match by closest factor
            closest = min(
                DEFAULT_EMISSION_FACTORS.items(),
                key=lambda x: abs(x[1] - grid_factor),
            )
            return closest[0]
        return "US-AVG"

    # ─────────────────────────────────────────
    # NYSERDA CNB alignment
    # ─────────────────────────────────────────

    def _calc_nyserda(self, report: ESGReport, kwht: KWhtResult) -> NYSERDAReport:
        """
        NYSERDA Carbon Neutral Brewery programme alignment.
        Specific to Baldwinsville (AB InBev) site.
        """
        targets = NYSERDA_CNB_TARGETS
        n = NYSERDAReport(
            site_id=report.site_id,
            period=f"{report.period_start} to {report.period_end}",
        )

        # Heat recovery efficiency
        n.heat_recovery_efficiency = report.efficiency.heat_recovery_pct
        n.heat_recovery_target_met = n.heat_recovery_efficiency >= targets["heat_recovery_efficiency_min"]

        # PUE
        n.pue = report.efficiency.pue
        n.pue_target_met = n.pue <= targets["pue_target"] and n.pue > 0

        # Renewable
        n.renewable_energy_pct = report.renewable_energy_pct
        n.renewable_target_met = n.renewable_energy_pct >= targets["renewable_energy_pct_target"]

        # CO₂ reduction (offset as % of gross emissions)
        if report.emissions.total_kg > 0:
            n.co2_reduction_vs_baseline_pct = (
                report.offset.offset_kg / report.emissions.total_kg
            ).quantize(Decimal("0.001"), ROUND_HALF_UP)
        n.co2_reduction_target_met = n.co2_reduction_vs_baseline_pct >= targets["co2_reduction_target_pct"]

        # Overall
        met_count = sum([
            n.heat_recovery_target_met, n.pue_target_met,
            n.renewable_target_met, n.co2_reduction_target_met,
        ])
        if met_count == 4:
            n.overall_alignment = "full"
        elif met_count >= 2:
            n.overall_alignment = "partial"
        else:
            n.overall_alignment = "below_target"

        n.notes = []
        if not n.heat_recovery_target_met:
            n.notes.append(
                f"Heat recovery {n.heat_recovery_efficiency:.1%} below "
                f"{targets['heat_recovery_efficiency_min']:.0%} target"
            )
        if not n.pue_target_met:
            n.notes.append(f"PUE {n.pue} above {targets['pue_target']} target")
        if not n.renewable_target_met:
            n.notes.append(
                f"Renewable energy {n.renewable_energy_pct:.0%} below "
                f"{targets['renewable_energy_pct_target']:.0%} target — consider PPA procurement"
            )

        return n

    # ─────────────────────────────────────────
    # Methodology
    # ─────────────────────────────────────────

    def _build_methodology(self, report: ESGReport) -> Dict[str, str]:
        """Transparent calculation methodology for each metric."""
        return {
            "heat_offset_co2": (
                "CO2_offset_kg = kWht_exported × grid_emission_factor × (1 - host_existing_efficiency)\n"
                f"Grid emission factor: {report.grid_emission_factor} kg CO2/kWh ({report.region_code})\n"
                "Source: EPA eGRID / EEA / Eskom published grid averages\n"
                "Host efficiency: efficiency of displaced heating system (e.g. gas boiler = 0.85)"
            ),
            "scope_1": (
                "Scope 1 = diesel_litres_consumed × 2.68 kg CO2/litre\n"
                "Source: IPCC emission factors for diesel generators\n"
                "Only counted when backup generator operates during utility outage"
            ),
            "scope_2": (
                "Scope 2 = total_facility_kWh × grid_emission_factor\n"
                "Location-based method using regional grid average\n"
                "Market-based adjustments applied when PPA/REC data available"
            ),
            "pue": (
                "PUE = total_facility_kWh / IT_kWh\n"
                "Measured from revenue-grade meters (MET-01 for facility, PDU-TOTAL for IT)\n"
                "5-minute interval metering, monthly average"
            ),
            "wue": (
                "WUE = water_litres / IT_kWh\n"
                "Water consumption from adiabatic cooling spray flow meter (FT-SPRAY)\n"
                "Only consumed during adiabatic mode (dry-bulb > setpoint)"
            ),
            "net_carbon": (
                "Net carbon = (Scope 1 + Scope 2) - heat_offset_CO2\n"
                "Negative value indicates carbon-negative operation\n"
                "Scope 3 excluded from initial calculations"
            ),
        }

    async def _get_trend(
        self, site_id: str, year: int, month: int,
    ) -> List[MonthlyESGSnapshot]:
        """12-month rolling trend (placeholder structure)."""
        trend = []
        for i in range(11, -1, -1):
            m = month - i
            y = year
            while m <= 0:
                m += 12
                y -= 1
            trend.append(MonthlyESGSnapshot(year=y, month=m))
        return trend


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """Full ESG calculation test with stub data."""
    from unittest.mock import MagicMock
    import sys

    print("=" * 60)
    print("ESG / Carbon Calculator — Stub Test")
    print("=" * 60)

    # ── Mock session ──
    mock_session = MagicMock(spec=Session)

    mock_contract = MagicMock()
    mock_contract.id = 1
    mock_contract.site_id = "BALD-01"
    mock_contract.rack_assignments = []
    mock_contract.rate_schedules = []

    mock_agreement = MagicMock()
    mock_agreement.contract = mock_contract
    mock_agreement.heat_pricing_model = MagicMock()
    mock_agreement.heat_pricing_model.value = "credit"
    mock_agreement.host_energy_rate = Decimal("0.065")
    mock_agreement.displacement_efficiency = Decimal("0.85")
    mock_agreement.revenue_share_pct = Decimal("0.05")
    mock_agreement.budget_neutral_threshold = Decimal("0")
    mock_agreement.host_existing_efficiency = Decimal("0.85")
    mock_agreement.grid_emission_factor = Decimal("0.24")

    # Patch
    this_module = sys.modules[__name__]
    import kwht_calculator as kwht_mod

    patches = {}
    for mod in [this_module, kwht_mod]:
        for fn_name in ["get_host_agreement_for_site", "get_active_contracts_for_site"]:
            if hasattr(mod, fn_name):
                patches[(mod, fn_name)] = getattr(mod, fn_name)

    for mod in [this_module, kwht_mod]:
        if hasattr(mod, "get_host_agreement_for_site"):
            setattr(mod, "get_host_agreement_for_site", lambda s, sid: mock_agreement)
        if hasattr(mod, "get_active_contracts_for_site"):
            setattr(mod, "get_active_contracts_for_site", lambda s, sid: [mock_contract])

    try:
        stub_api = StubStreamBClient()
        calculator = ESGCalculator(stub_api, mock_session)
        report = await calculator.calculate(site_id="BALD-01", year=2026, month=1)

        # ── Display ──
        print(f"\nSite: {report.site_id} | Region: {report.region_code}")
        print(f"Period: {report.period_start} → {report.period_end}")
        print(f"Grid factor: {report.grid_emission_factor} kg CO2/kWh")
        print()

        print("─── Emissions ──────────────────────────────────────")
        e = report.emissions
        print(f"  Scope 1 (diesel):    {e.scope_1_kg:>10,.1f} kg CO2")
        print(f"    Generator:         {e.generator_runtime_hours} hrs, {e.diesel_litres} L diesel")
        print(f"  Scope 2 (grid):      {e.scope_2_kg:>10,.1f} kg CO2")
        print(f"    Facility kWh:      {e.total_facility_kwh:>10,.0f}")
        print(f"  Total emissions:     {e.total_kg:>10,.1f} kg CO2")
        print()

        print("─── Heat Offset ────────────────────────────────────")
        o = report.offset
        print(f"  kWht exported:       {o.kwht_exported:>10,.0f}")
        print(f"  CO2 offset:          {o.offset_kg:>10,.1f} kg CO2")
        print()

        print("─── Net Carbon Position ────────────────────────────")
        sign = "+" if report.net_carbon_kg >= 0 else ""
        status = "CARBON NEGATIVE ✓" if report.carbon_negative else "CARBON POSITIVE"
        print(f"  Net:                 {sign}{report.net_carbon_kg:>9,.1f} kg CO2  [{status}]")
        print()

        print("─── Efficiency ─────────────────────────────────────")
        eff = report.efficiency
        print(f"  PUE:                 {eff.pue}")
        print(f"  WUE:                 {eff.wue} L/kWh")
        print(f"  IT kWh:              {eff.it_kwh:>10,.0f}")
        print(f"  Cooling kWh:         {eff.cooling_kwh:>10,.0f}")
        print(f"  Water:               {eff.water_litres:>10,.0f} L")
        print(f"  Heat recovery:       {eff.heat_recovery_pct:.1%}")
        print()

        if report.nyserda:
            n = report.nyserda
            print("─── NYSERDA CNB Alignment ──────────────────────────")
            print(f"  Heat recovery:       {n.heat_recovery_efficiency:.1%} {'✓' if n.heat_recovery_target_met else '✗'}")
            print(f"  PUE:                 {n.pue} {'✓' if n.pue_target_met else '✗'}")
            print(f"  Renewable:           {n.renewable_energy_pct:.0%} {'✓' if n.renewable_target_met else '✗'}")
            print(f"  CO2 reduction:       {n.co2_reduction_vs_baseline_pct:.1%} {'✓' if n.co2_reduction_target_met else '✗'}")
            print(f"  Overall:             {n.overall_alignment.upper()}")
            if n.notes:
                for note in n.notes:
                    print(f"    → {note}")
            print()

        # ── Assertions ──
        assert report.emissions.total_kg > 0
        assert report.offset.offset_kg > 0
        assert report.efficiency.pue > Decimal("1.0")
        assert report.efficiency.pue < Decimal("2.0")
        assert len(report.methodology) >= 5
        assert report.nyserda is not None

        print("✓ All assertions passed")

    finally:
        for (mod, fn_name), orig in patches.items():
            setattr(mod, fn_name, orig)
        await stub_api.close()


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
