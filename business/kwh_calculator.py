"""
MCS Stream C — Task 2: kWh Billing Calculator
===============================================
Service that calculates electrical billing for each customer for a given
billing period (month).

Consumes Stream B's REST API for 5-min power readings.
Produces line-item dicts ready for invoice generation (Task 5).

Usage:
    calculator = KWhCalculator(api_client, session)
    result = await calculator.calculate(customer_id=1, year=2026, month=1)
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any

import httpx

# Local imports — Task 1 models
from billing_models import (
    Session,
    Contract, ContractRackAssignment, RateSchedule, BillingMeter,
    ContractStatus, ContractType, RateType, LineItemType,
    get_active_contract, get_rate, get_customer_rack_assignments,
)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

INTERVAL_MINUTES = 5
INTERVAL_HOURS = Decimal(str(INTERVAL_MINUTES)) / Decimal("60")  # 5/60
DEMAND_WINDOW_MINUTES = 15  # rolling average window for peak demand
DEMAND_INTERVALS = DEMAND_WINDOW_MINUTES // INTERVAL_MINUTES  # 3 intervals
MISSING_DATA_THRESHOLD = Decimal("0.05")  # 5% missing → flag for review
BAD_QUALITY_THRESHOLD_MINUTES = 60  # >1 hour of BAD quality → flag

HOURS_PER_MONTH_APPROX = Decimal("730")  # used for display / estimation only


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class MeterReading:
    """Single 5-minute interval reading from Stream B."""
    timestamp: datetime
    avg_kw: Decimal
    quality: str = "GOOD"  # GOOD / SUSPECT / BAD


@dataclass
class DataQualityReport:
    """Quality assessment of meter data for a billing period."""
    total_expected_intervals: int = 0
    total_received_intervals: int = 0
    missing_intervals: int = 0
    missing_pct: Decimal = Decimal("0")
    bad_quality_intervals: int = 0
    bad_quality_total_minutes: int = 0
    suspect_intervals: int = 0
    needs_manual_review: bool = False
    flags: List[str] = field(default_factory=list)


@dataclass
class BillingLineItem:
    """A calculated line item ready for invoice generation."""
    line_type: str  # matches LineItemType enum value
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    amount: Decimal
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KWhBillingResult:
    """Complete billing calculation output for a customer/period."""
    customer_id: int
    contract_id: int
    period_start: date
    period_end: date
    currency: str

    # Raw calculations
    total_kwh: Decimal = Decimal("0")
    peak_demand_kw: Decimal = Decimal("0")
    cooling_kwh: Decimal = Decimal("0")
    facility_pue: Decimal = Decimal("1.0")
    committed_kw: Decimal = Decimal("0")

    # Costs
    colo_fee: Decimal = Decimal("0")
    power_cost: Decimal = Decimal("0")
    demand_cost: Decimal = Decimal("0")
    cooling_cost: Decimal = Decimal("0")

    # Pro-rata
    is_partial_month: bool = False
    prorate_factor: Decimal = Decimal("1.0")

    # Line items for invoice
    line_items: List[BillingLineItem] = field(default_factory=list)

    # Data quality
    quality: DataQualityReport = field(default_factory=DataQualityReport)

    # Per-meter breakdown (for auditing)
    meter_breakdowns: Dict[str, Dict[str, Any]] = field(default_factory=dict)


# ─────────────────────────────────────────────
# Stream B API client (stub-ready)
# ─────────────────────────────────────────────

class StreamBClient:
    """
    HTTP client for Stream B's REST API.
    Swap base_url to hit real API or test fixtures.
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

    async def get_billing_telemetry(
        self,
        block_id: str,
        month: str,  # "2026-01"
    ) -> List[Dict[str, Any]]:
        """
        GET /telemetry/billing?block_id=X&month=2026-01
        Returns 5-min kW rollups for billing meters.
        """
        client = await self._get_client()
        resp = await client.get(
            "/telemetry/billing",
            params={"block_id": block_id, "month": month},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_telemetry(
        self,
        sensor_id: str,
        start: str,
        end: str,
        agg: str = "5min",
    ) -> List[Dict[str, Any]]:
        """
        GET /telemetry?sensor_id=X&start=T1&end=T2&agg=5min
        Returns historical readings for a specific sensor.
        """
        client = await self._get_client()
        resp = await client.get(
            "/telemetry",
            params={"sensor_id": sensor_id, "start": start, "end": end, "agg": agg},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_latest_telemetry(self, block_id: str) -> Dict[str, Any]:
        """GET /telemetry/latest?block_id=X"""
        client = await self._get_client()
        resp = await client.get("/telemetry/latest", params={"block_id": block_id})
        resp.raise_for_status()
        return resp.json()

    async def get_block(self, block_id: str) -> Dict[str, Any]:
        """GET /blocks/{id} — block config including PUE."""
        client = await self._get_client()
        resp = await client.get(f"/blocks/{block_id}")
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────
# Stub data (for dev until Stream B is live)
# ─────────────────────────────────────────────

class StubStreamBClient(StreamBClient):
    """
    Returns synthetic 5-min readings for testing.
    Replace with real StreamBClient once Stream B API is available.
    """

    def __init__(self):
        super().__init__(base_url="stub://")

    async def get_telemetry(
        self, sensor_id: str, start: str, end: str, agg: str = "5min",
    ) -> List[Dict[str, Any]]:
        """Generate synthetic 5-min readings across the period."""
        import random
        random.seed(hash(sensor_id + start))

        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

        readings = []
        t = start_dt
        base_kw = 65.0  # ~65kW average per rack group

        while t < end_dt:
            # Simulate slight variation with occasional dips
            hour = t.hour
            daily_factor = 1.0 + 0.05 * math.sin(2 * math.pi * hour / 24)
            noise = random.gauss(0, 1.5)
            kw = max(0, base_kw * daily_factor + noise)
            quality = "GOOD"
            if random.random() < 0.001:
                quality = "BAD"
            elif random.random() < 0.005:
                quality = "SUSPECT"

            readings.append({
                "timestamp": t.isoformat(),
                "avg_kw": round(kw, 3),
                "quality": quality,
            })
            t += timedelta(minutes=INTERVAL_MINUTES)

        return readings

    async def get_block(self, block_id: str) -> Dict[str, Any]:
        return {
            "id": block_id,
            "site_id": "BALD-01",
            "pue": 1.15,  # liquid-cooled target PUE
            "total_capacity_kw": 1000,
        }


# ─────────────────────────────────────────────
# Core calculator
# ─────────────────────────────────────────────

class KWhCalculator:
    """
    Calculates electrical billing for a customer for a given month.

    Steps:
      1. Load contract, rack assignments, rates
      2. Fetch 5-min power readings per meter from Stream B
      3. Calculate total kWh, peak demand, cooling share
      4. Apply rates and build line items
      5. Handle pro-rata, data quality, missing data
    """

    def __init__(self, api_client: StreamBClient, session: Session):
        self.api = api_client
        self.session = session

    async def calculate(
        self,
        customer_id: int,
        year: int,
        month: int,
    ) -> KWhBillingResult:
        """Main entry point — calculate billing for a customer/month."""

        # ── 1. Contract & assignments ──
        contract = get_active_contract(self.session, customer_id, ContractType.COLO_MSA)
        if contract is None:
            raise ValueError(f"No active colo MSA found for customer {customer_id}")

        assignments = get_customer_rack_assignments(self.session, customer_id)
        if not assignments:
            raise ValueError(f"No rack assignments for customer {customer_id}")

        # ── Period bounds ──
        period_start = date(year, month, 1)
        if month == 12:
            period_end = date(year + 1, 1, 1)
        else:
            period_end = date(year, month + 1, 1)

        # Pro-rata: if contract starts or ends mid-month
        effective_start = max(period_start, contract.start_date)
        effective_end = period_end
        if contract.end_date and contract.end_date < period_end:
            effective_end = contract.end_date

        total_days_in_month = (period_end - period_start).days
        billable_days = (effective_end - effective_start).days
        is_partial = billable_days < total_days_in_month
        prorate_factor = Decimal(str(billable_days)) / Decimal(str(total_days_in_month))

        # ── Committed kW ──
        total_committed_kw = sum(a.committed_kw for a in assignments)

        # ── Rates ──
        rate_colo = get_rate(self.session, contract.id, RateType.COLO_PER_KW, period_start)
        rate_power = get_rate(self.session, contract.id, RateType.POWER_PER_KWH, period_start)
        rate_demand = get_rate(self.session, contract.id, RateType.DEMAND_CHARGE, period_start)
        rate_cooling = get_rate(self.session, contract.id, RateType.COOLING_PUE, period_start)

        if rate_colo is None:
            raise ValueError(f"No colo rate for contract {contract.id}")
        if rate_power is None:
            raise ValueError(f"No power rate for contract {contract.id}")

        # ── Result container ──
        result = KWhBillingResult(
            customer_id=customer_id,
            contract_id=contract.id,
            period_start=period_start,
            period_end=period_end,
            currency=contract.contracts[0].currency if hasattr(contract, 'contracts') else "USD",
            committed_kw=total_committed_kw,
            is_partial_month=is_partial,
            prorate_factor=prorate_factor,
        )
        # Fix currency access
        result.currency = rate_colo.currency

        # ── 2. Fetch readings per meter and block ──
        all_readings: List[MeterReading] = []

        for assignment in assignments:
            block_id = assignment.block_id
            # Get billing meters for this contract
            meters = [
                m for m in contract.billing_meters
                if m.meter_type.value == "electrical"
            ]

            # If no specific meters configured, use block-level billing endpoint
            sensor_ids = [m.sensor_tag for m in meters] if meters else [f"PDU-{block_id}"]

            start_iso = datetime(year, month, 1, tzinfo=timezone.utc).isoformat()
            end_iso = datetime(
                effective_end.year, effective_end.month, effective_end.day,
                tzinfo=timezone.utc,
            ).isoformat()

            for sensor_id in sensor_ids:
                raw = await self.api.get_telemetry(
                    sensor_id=sensor_id,
                    start=start_iso,
                    end=end_iso,
                )

                readings = [
                    MeterReading(
                        timestamp=datetime.fromisoformat(
                            r["timestamp"].replace("Z", "+00:00")
                        ),
                        avg_kw=Decimal(str(r["avg_kw"])),
                        quality=r.get("quality", "GOOD"),
                    )
                    for r in raw
                ]

                # Per-meter breakdown
                meter_kwh, meter_peak = self._calc_energy_and_peak(readings)
                result.meter_breakdowns[sensor_id] = {
                    "readings_count": len(readings),
                    "total_kwh": meter_kwh,
                    "peak_kw": meter_peak,
                }

                all_readings.extend(readings)

            # Get block PUE
            block_info = await self.api.get_block(block_id)
            result.facility_pue = Decimal(str(block_info.get("pue", 1.15)))

        # ── 3. Data quality assessment ──
        result.quality = self._assess_data_quality(
            all_readings, effective_start, effective_end,
        )

        # ── 4. Energy calculations ──
        result.total_kwh, result.peak_demand_kw = self._calc_energy_and_peak(all_readings)
        result.cooling_kwh = result.total_kwh * (result.facility_pue - Decimal("1.0"))

        # ── 5. Cost calculations ──
        # Colocation fee: committed kW × $/kW/month (rate_value is $/kW/hr → × hours)
        # The rate is stored as $/kW/month for simplicity
        colo_monthly = total_committed_kw * rate_colo.rate_value
        result.colo_fee = (colo_monthly * prorate_factor).quantize(
            Decimal("0.01"), ROUND_HALF_UP,
        )

        # Metered power: total kWh × $/kWh
        result.power_cost = (result.total_kwh * rate_power.rate_value).quantize(
            Decimal("0.01"), ROUND_HALF_UP,
        )

        # Demand charge: peak kW × $/kW (if rate exists)
        if rate_demand and rate_demand.rate_value > 0:
            result.demand_cost = (result.peak_demand_kw * rate_demand.rate_value).quantize(
                Decimal("0.01"), ROUND_HALF_UP,
            )

        # Cooling overhead: cooling kWh × $/kWh (uses power rate if no specific cooling rate)
        cooling_rate = rate_cooling.rate_value if rate_cooling else rate_power.rate_value
        result.cooling_cost = (result.cooling_kwh * cooling_rate).quantize(
            Decimal("0.01"), ROUND_HALF_UP,
        )

        # ── 6. Build line items ──
        result.line_items = self._build_line_items(result, rate_colo, rate_power, rate_demand, rate_cooling)

        return result

    def _calc_energy_and_peak(
        self, readings: List[MeterReading],
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate total kWh and peak 15-min demand kW from 5-min readings.

        kWh = SUM(avg_kw × 5/60) for each interval
        Peak demand = MAX(rolling 15-min average kW)
        """
        if not readings:
            return Decimal("0"), Decimal("0")

        # Sort by timestamp
        sorted_readings = sorted(readings, key=lambda r: r.timestamp)

        # Total kWh
        total_kwh = sum(
            r.avg_kw * INTERVAL_HOURS
            for r in sorted_readings
            if r.quality != "BAD"
        )

        # Peak 15-min demand (rolling window of 3 × 5-min intervals)
        peak_demand = Decimal("0")
        kw_values = [r.avg_kw for r in sorted_readings if r.quality != "BAD"]

        if len(kw_values) >= DEMAND_INTERVALS:
            for i in range(len(kw_values) - DEMAND_INTERVALS + 1):
                window = kw_values[i : i + DEMAND_INTERVALS]
                avg_15min = sum(window) / Decimal(str(DEMAND_INTERVALS))
                if avg_15min > peak_demand:
                    peak_demand = avg_15min

        total_kwh = total_kwh.quantize(Decimal("0.0001"), ROUND_HALF_UP)
        peak_demand = peak_demand.quantize(Decimal("0.01"), ROUND_HALF_UP)

        return total_kwh, peak_demand

    def _assess_data_quality(
        self,
        readings: List[MeterReading],
        period_start: date,
        period_end: date,
    ) -> DataQualityReport:
        """Check for missing data and quality issues."""
        report = DataQualityReport()

        total_minutes = (period_end - period_start).days * 24 * 60
        report.total_expected_intervals = total_minutes // INTERVAL_MINUTES
        report.total_received_intervals = len(readings)
        report.missing_intervals = max(
            0, report.total_expected_intervals - report.total_received_intervals,
        )

        if report.total_expected_intervals > 0:
            report.missing_pct = (
                Decimal(str(report.missing_intervals))
                / Decimal(str(report.total_expected_intervals))
            ).quantize(Decimal("0.0001"), ROUND_HALF_UP)

        # Count bad/suspect readings
        report.bad_quality_intervals = sum(1 for r in readings if r.quality == "BAD")
        report.bad_quality_total_minutes = report.bad_quality_intervals * INTERVAL_MINUTES
        report.suspect_intervals = sum(1 for r in readings if r.quality == "SUSPECT")

        # Flag conditions
        if report.missing_pct > MISSING_DATA_THRESHOLD:
            report.needs_manual_review = True
            report.flags.append(
                f"Missing data: {report.missing_pct * 100:.1f}% "
                f"({report.missing_intervals} of {report.total_expected_intervals} intervals)"
            )

        if report.bad_quality_total_minutes > BAD_QUALITY_THRESHOLD_MINUTES:
            report.needs_manual_review = True
            report.flags.append(
                f"Bad quality readings: {report.bad_quality_total_minutes} minutes "
                f"({report.bad_quality_intervals} intervals)"
            )

        if report.suspect_intervals > 0:
            report.flags.append(
                f"Suspect readings: {report.suspect_intervals} intervals"
            )

        return report

    def _build_line_items(
        self,
        result: KWhBillingResult,
        rate_colo: RateSchedule,
        rate_power: RateSchedule,
        rate_demand: Optional[RateSchedule],
        rate_cooling: Optional[RateSchedule],
    ) -> List[BillingLineItem]:
        """Assemble billing line items from calculated values."""
        items = []

        # 1. Colocation fee
        prorate_note = ""
        if result.is_partial_month:
            prorate_note = f" (pro-rata {result.prorate_factor:.4f})"

        items.append(BillingLineItem(
            line_type=LineItemType.COLO_FEE.value,
            description=f"Colocation — {result.committed_kw} kW committed{prorate_note}",
            quantity=result.committed_kw,
            unit="kW",
            unit_price=rate_colo.rate_value * result.prorate_factor,
            amount=result.colo_fee,
            metadata={
                "committed_kw": str(result.committed_kw),
                "rate_per_kw_month": str(rate_colo.rate_value),
                "prorate_factor": str(result.prorate_factor),
            },
        ))

        # 2. Metered power
        items.append(BillingLineItem(
            line_type=LineItemType.METERED_POWER.value,
            description=f"Metered IT power — {result.total_kwh:,.1f} kWh",
            quantity=result.total_kwh,
            unit="kWh",
            unit_price=rate_power.rate_value,
            amount=result.power_cost,
            metadata={
                "total_kwh": str(result.total_kwh),
                "rate_per_kwh": str(rate_power.rate_value),
                "meter_breakdowns": {
                    k: {"kwh": str(v["total_kwh"]), "readings": v["readings_count"]}
                    for k, v in result.meter_breakdowns.items()
                },
            },
        ))

        # 3. Demand charge (if applicable)
        if rate_demand and result.demand_cost > 0:
            items.append(BillingLineItem(
                line_type=LineItemType.DEMAND_CHARGE.value,
                description=f"Peak demand charge — {result.peak_demand_kw:.1f} kW",
                quantity=result.peak_demand_kw,
                unit="kW",
                unit_price=rate_demand.rate_value,
                amount=result.demand_cost,
                metadata={"peak_15min_kw": str(result.peak_demand_kw)},
            ))

        # 4. Cooling overhead
        items.append(BillingLineItem(
            line_type=LineItemType.COOLING_OVERHEAD.value,
            description=(
                f"Cooling overhead — PUE {result.facility_pue:.3f}, "
                f"{result.cooling_kwh:,.1f} kWh"
            ),
            quantity=result.cooling_kwh,
            unit="kWh",
            unit_price=(rate_cooling.rate_value if rate_cooling else rate_power.rate_value),
            amount=result.cooling_cost,
            metadata={
                "pue": str(result.facility_pue),
                "cooling_kwh": str(result.cooling_kwh),
            },
        ))

        return items


# ─────────────────────────────────────────────
# Convenience: run billing for all customers at a site
# ─────────────────────────────────────────────

async def calculate_site_billing(
    api_client: StreamBClient,
    session: Session,
    site_id: str,
    year: int,
    month: int,
) -> List[KWhBillingResult]:
    """
    Calculate billing for every active colo customer at a site.
    Used by Task 5 (invoice generator) for batch mode.
    """
    from billing_models import get_active_contracts_for_site, ContractType

    contracts = get_active_contracts_for_site(session, site_id)
    colo_contracts = [c for c in contracts if c.contract_type == ContractType.COLO_MSA]

    calculator = KWhCalculator(api_client, session)
    results = []

    for contract in colo_contracts:
        try:
            result = await calculator.calculate(contract.customer_id, year, month)
            results.append(result)
        except Exception as e:
            # Log error but continue with other customers
            print(f"[BILLING ERROR] customer={contract.customer_id} site={site_id}: {e}")

    return results


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """
    Integration test using stub data.
    Validates calculation pipeline without a real DB or API.
    """
    from unittest.mock import MagicMock
    from decimal import Decimal

    print("=" * 60)
    print("kWh Billing Calculator — Stub Test")
    print("=" * 60)

    # ── Mock session & DB objects ──
    mock_session = MagicMock(spec=Session)

    # Fake contract
    mock_contract = MagicMock(spec=Contract)
    mock_contract.id = 1
    mock_contract.customer_id = 1
    mock_contract.start_date = date(2026, 1, 1)
    mock_contract.end_date = None
    mock_contract.billing_meters = []

    # Fake rack assignment
    mock_assignment = MagicMock(spec=ContractRackAssignment)
    mock_assignment.block_id = "BALD-BLK-01"
    mock_assignment.committed_kw = Decimal("420")  # 6 racks × 70kW
    mock_assignment.rack_ids = ["R01", "R02", "R03", "R04", "R05", "R06"]

    # Fake rates
    mock_rate_colo = MagicMock(spec=RateSchedule)
    mock_rate_colo.rate_value = Decimal("170.00")  # $170/kW/month
    mock_rate_colo.currency = "USD"

    mock_rate_power = MagicMock(spec=RateSchedule)
    mock_rate_power.rate_value = Decimal("0.085")  # $0.085/kWh

    mock_rate_demand = MagicMock(spec=RateSchedule)
    mock_rate_demand.rate_value = Decimal("12.50")  # $12.50/kW demand

    mock_rate_cooling = MagicMock(spec=RateSchedule)
    mock_rate_cooling.rate_value = Decimal("0.085")

    # Patch the helper functions — must patch THIS module's namespace
    import sys
    this_module = sys.modules[__name__]

    _orig_get_active = get_active_contract
    _orig_get_rack = get_customer_rack_assignments
    _orig_get_rate = get_rate

    this_module.get_active_contract = lambda s, cid, ct=None: mock_contract
    this_module.get_customer_rack_assignments = lambda s, cid, bid=None: [mock_assignment]

    def _mock_get_rate(s, cid, rt, d=None):
        return {
            RateType.COLO_PER_KW: mock_rate_colo,
            RateType.POWER_PER_KWH: mock_rate_power,
            RateType.DEMAND_CHARGE: mock_rate_demand,
            RateType.COOLING_PUE: mock_rate_cooling,
        }.get(rt)

    this_module.get_rate = _mock_get_rate

    try:
        # ── Run calculation with stub API ──
        stub_api = StubStreamBClient()
        calculator = KWhCalculator(stub_api, mock_session)
        result = await calculator.calculate(customer_id=1, year=2026, month=1)

        # ── Display results ──
        print(f"\nPeriod: {result.period_start} → {result.period_end}")
        print(f"Partial month: {result.is_partial_month} (factor: {result.prorate_factor})")
        print(f"Committed kW: {result.committed_kw}")
        print(f"Facility PUE: {result.facility_pue}")
        print()
        print(f"Total kWh:      {result.total_kwh:>12,.1f}")
        print(f"Peak demand kW: {result.peak_demand_kw:>12,.1f}")
        print(f"Cooling kWh:    {result.cooling_kwh:>12,.1f}")
        print()
        print("─── Line Items ─────────────────────────────────────")
        subtotal = Decimal("0")
        for item in result.line_items:
            print(f"  {item.description}")
            print(f"    {item.quantity:>10,.2f} {item.unit} × ${item.unit_price:,.6f} = ${item.amount:>10,.2f}")
            subtotal += item.amount
        print(f"{'':>50}{'─' * 14}")
        print(f"  {'Subtotal':>48} ${subtotal:>10,.2f}")
        print()

        # ── Data quality ──
        q = result.quality
        print("─── Data Quality ───────────────────────────────────")
        print(f"  Expected intervals:  {q.total_expected_intervals}")
        print(f"  Received intervals:  {q.total_received_intervals}")
        print(f"  Missing:             {q.missing_intervals} ({q.missing_pct * 100:.2f}%)")
        print(f"  Bad quality:         {q.bad_quality_intervals} ({q.bad_quality_total_minutes} min)")
        print(f"  Manual review:       {q.needs_manual_review}")
        if q.flags:
            for flag in q.flags:
                print(f"    ⚠ {flag}")

        # ── Assertions ──
        print()
        assert result.total_kwh > 0, "kWh should be > 0"
        assert result.peak_demand_kw > 0, "Peak demand should be > 0"
        assert result.colo_fee > 0, "Colo fee should be > 0"
        assert result.power_cost > 0, "Power cost should be > 0"
        assert len(result.line_items) >= 3, "Should have at least 3 line items"
        assert result.cooling_kwh > 0, "Cooling kWh should be > 0 (PUE > 1.0)"

        # Sanity check: ~65kW avg × 744 hours ≈ ~48,360 kWh for Jan
        expected_range = (40_000, 60_000)
        assert expected_range[0] < float(result.total_kwh) < expected_range[1], \
            f"kWh {result.total_kwh} outside expected range {expected_range}"

        print("✓ All assertions passed")

    finally:
        # Restore originals
        this_module.get_active_contract = _orig_get_active
        this_module.get_customer_rack_assignments = _orig_get_rack
        this_module.get_rate = _orig_get_rate
        await stub_api.close()


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
