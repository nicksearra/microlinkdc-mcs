"""
MCS Stream C — Task 4: SLA Engine
===================================
Service that calculates availability and SLA compliance per customer
for a given billing period.

Consumes Stream B's alarm API (P0/P1 incidents) and planned maintenance
windows from the billing schema.

Usage:
    engine = SLAEngine(api_client, session)
    report = await engine.calculate(customer_id=1, year=2026, month=1)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any, Tuple

import httpx

# Local imports — Task 1 models
from billing_models import (
    Session,
    Contract, ContractRackAssignment, PlannedMaintenance,
    ContractType, ContractStatus, AvailabilityClass,
    SLA_TARGETS, SLA_CREDIT_TIERS,
    get_active_contract, get_customer_rack_assignments,
    get_planned_maintenance_windows, get_rate, get_sla_credit_pct,
    RateType,
)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Alarm priorities that count as unplanned downtime
DOWNTIME_PRIORITIES = {"P0", "P1"}

# Root cause categories for incident classification
ROOT_CAUSE_CATEGORIES = [
    "power_failure",
    "cooling_failure",
    "network_outage",
    "hardware_failure",
    "software_failure",
    "human_error",
    "environmental",
    "unknown",
]


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class AlarmIncident:
    """A P0/P1 alarm from Stream B that may count as downtime."""
    alarm_id: str
    priority: str               # P0, P1
    block_id: str
    sensor_tag: str
    description: str
    raised_at: datetime
    cleared_at: Optional[datetime]
    duration_minutes: Decimal = Decimal("0")
    root_cause: str = "unknown"
    excluded: bool = False
    exclusion_reason: str = ""


@dataclass
class MaintenanceWindow:
    """Planned maintenance window (for SLA exclusion)."""
    id: int
    block_id: str
    start_at: datetime
    end_at: datetime
    notice_sent_at: Optional[datetime]
    description: str
    valid_exclusion: bool       # True if notice ≥ 48h before start


@dataclass
class SLAReport:
    """Complete SLA report for a customer/period."""
    customer_id: int
    contract_id: int
    period_start: date
    period_end: date
    availability_class: str     # A, B, C
    sla_target_pct: Decimal

    # Time accounting (all in minutes)
    total_minutes: Decimal = Decimal("0")
    total_downtime_minutes: Decimal = Decimal("0")
    excluded_downtime_minutes: Decimal = Decimal("0")
    unplanned_downtime_minutes: Decimal = Decimal("0")

    # Availability
    availability_pct: Decimal = Decimal("100.0000")
    sla_met: bool = True

    # Credits
    credit_tier_pct: Decimal = Decimal("0")
    credit_amount: Decimal = Decimal("0")
    monthly_colo_fee: Decimal = Decimal("0")

    # Incidents
    incidents: List[AlarmIncident] = field(default_factory=list)
    contributing_incidents: List[AlarmIncident] = field(default_factory=list)
    excluded_incidents: List[AlarmIncident] = field(default_factory=list)

    # Maintenance windows
    maintenance_windows: List[MaintenanceWindow] = field(default_factory=list)

    # Trending (prior 3 months)
    trend: List[Dict[str, Any]] = field(default_factory=list)

    # Warnings
    breach_warning: bool = False
    breach_warning_message: str = ""

    # Summary for invoice
    summary_text: str = ""


# ─────────────────────────────────────────────
# Stream B API client (alarm endpoints)
# ─────────────────────────────────────────────

class StreamBClient:
    """HTTP client for Stream B's alarm and event APIs."""

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

    async def get_alarms(
        self,
        block_id: str,
        start: str,
        end: str,
        state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        GET /alarms?block_id=X&start=T1&end=T2
        Returns alarm history for SLA calculation.
        """
        client = await self._get_client()
        params: Dict[str, str] = {
            "block_id": block_id,
            "start": start,
            "end": end,
        }
        if state:
            params["state"] = state
        resp = await client.get("/alarms", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_active_alarms(self, block_id: str) -> List[Dict[str, Any]]:
        """GET /alarms?state=ACTIVE&block_id=X"""
        client = await self._get_client()
        resp = await client.get(
            "/alarms",
            params={"state": "ACTIVE", "block_id": block_id},
        )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────
# Stub data
# ─────────────────────────────────────────────

class StubStreamBClient(StreamBClient):
    """Synthetic alarm data for testing."""

    def __init__(self):
        super().__init__(base_url="stub://")

    async def get_alarms(
        self, block_id: str, start: str, end: str, state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Generate a realistic alarm history for a month."""
        import random
        random.seed(hash(block_id + start) % 2**31)

        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

        alarms = []

        # 1 × P0 incident (45 min) — power dip
        p0_start = start_dt + timedelta(days=random.randint(5, 20), hours=random.randint(0, 23))
        if p0_start < end_dt:
            p0_dur = random.randint(30, 60)
            alarms.append({
                "alarm_id": f"ALM-{block_id}-P0-001",
                "priority": "P0",
                "block_id": block_id,
                "sensor_tag": "UPS-01-STATUS",
                "description": "UPS transfer to battery — utility power dip",
                "raised_at": p0_start.isoformat(),
                "cleared_at": (p0_start + timedelta(minutes=p0_dur)).isoformat(),
                "root_cause": "power_failure",
            })

        # 2 × P1 incidents (15-30 min each) — cooling warnings
        for i in range(2):
            p1_start = start_dt + timedelta(
                days=random.randint(1, 28), hours=random.randint(0, 23),
            )
            if p1_start < end_dt:
                p1_dur = random.randint(10, 35)
                alarms.append({
                    "alarm_id": f"ALM-{block_id}-P1-{i+1:03d}",
                    "priority": "P1",
                    "block_id": block_id,
                    "sensor_tag": f"TT-CDU{i+1}-IN",
                    "description": f"CDU{i+1} inlet temperature high — threshold exceeded",
                    "raised_at": p1_start.isoformat(),
                    "cleared_at": (p1_start + timedelta(minutes=p1_dur)).isoformat(),
                    "root_cause": "cooling_failure",
                })

        # 5 × P2 alarms (not counted for SLA)
        for i in range(5):
            p2_start = start_dt + timedelta(
                days=random.randint(1, 28), hours=random.randint(0, 23),
            )
            if p2_start < end_dt:
                alarms.append({
                    "alarm_id": f"ALM-{block_id}-P2-{i+1:03d}",
                    "priority": "P2",
                    "block_id": block_id,
                    "sensor_tag": f"MISC-{i+1}",
                    "description": f"Minor sensor warning #{i+1}",
                    "raised_at": p2_start.isoformat(),
                    "cleared_at": (p2_start + timedelta(minutes=random.randint(5, 120))).isoformat(),
                    "root_cause": "hardware_failure",
                })

        return sorted(alarms, key=lambda a: a["raised_at"])


# ─────────────────────────────────────────────
# Core SLA engine
# ─────────────────────────────────────────────

class SLAEngine:
    """
    Calculates SLA compliance for a customer over a billing period.

    Steps:
      1. Load contract — get availability class and rack assignments
      2. Fetch P0/P1 alarms from Stream B for each assigned block
      3. Fetch planned maintenance windows from billing DB
      4. Calculate gross downtime from alarm durations
      5. Subtract valid exclusions (planned maintenance, force majeure)
      6. Calculate availability percentage
      7. Determine SLA credit tier and amount
      8. Build SLA report with incident detail and trending
    """

    def __init__(self, api_client: StreamBClient, session: Session):
        self.api = api_client
        self.session = session

    async def calculate(
        self,
        customer_id: int,
        year: int,
        month: int,
    ) -> SLAReport:
        """Main entry point — calculate SLA for a customer/month."""

        # ── 1. Contract & assignments ──
        contract = get_active_contract(self.session, customer_id, ContractType.COLO_MSA)
        if contract is None:
            raise ValueError(f"No active colo MSA for customer {customer_id}")

        assignments = get_customer_rack_assignments(self.session, customer_id)
        if not assignments:
            raise ValueError(f"No rack assignments for customer {customer_id}")

        # Get availability class (take highest class across assignments)
        avail_classes = [a.availability_class for a in assignments]
        availability_class = max(avail_classes, key=lambda c: SLA_TARGETS[c])

        sla_target = SLA_TARGETS[availability_class]

        # ── Period bounds ──
        period_start = date(year, month, 1)
        if month == 12:
            period_end = date(year + 1, 1, 1)
        else:
            period_end = date(year, month + 1, 1)

        total_minutes = Decimal(str((period_end - period_start).days * 24 * 60))

        start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
        end_dt = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        # ── Init report ──
        report = SLAReport(
            customer_id=customer_id,
            contract_id=contract.id,
            period_start=period_start,
            period_end=period_end,
            availability_class=availability_class.value,
            sla_target_pct=sla_target,
            total_minutes=total_minutes,
        )

        # ── 2. Fetch alarms per block ──
        block_ids = list({a.block_id for a in assignments})
        all_alarms: List[AlarmIncident] = []

        for block_id in block_ids:
            raw_alarms = await self.api.get_alarms(block_id, start_iso, end_iso)

            for a in raw_alarms:
                # Only P0/P1 count for SLA
                if a.get("priority") not in DOWNTIME_PRIORITIES:
                    continue

                raised_at = datetime.fromisoformat(a["raised_at"].replace("Z", "+00:00"))
                cleared_at = None
                if a.get("cleared_at"):
                    cleared_at = datetime.fromisoformat(a["cleared_at"].replace("Z", "+00:00"))

                # Clamp to billing period
                effective_start = max(raised_at, start_dt)
                effective_end = min(cleared_at, end_dt) if cleared_at else end_dt

                duration = Decimal(str(
                    max(0, (effective_end - effective_start).total_seconds() / 60)
                )).quantize(Decimal("0.01"), ROUND_HALF_UP)

                incident = AlarmIncident(
                    alarm_id=a["alarm_id"],
                    priority=a["priority"],
                    block_id=a.get("block_id", block_id),
                    sensor_tag=a.get("sensor_tag", ""),
                    description=a.get("description", ""),
                    raised_at=raised_at,
                    cleared_at=cleared_at,
                    duration_minutes=duration,
                    root_cause=a.get("root_cause", "unknown"),
                )

                all_alarms.append(incident)

        report.incidents = all_alarms

        # ── 3. Fetch maintenance windows ──
        maintenance_windows: List[MaintenanceWindow] = []
        for block_id in block_ids:
            windows = get_planned_maintenance_windows(
                self.session, block_id, start_dt, end_dt, valid_only=False,
            )
            for w in windows:
                maintenance_windows.append(MaintenanceWindow(
                    id=w.id,
                    block_id=w.block_id,
                    start_at=w.start_at,
                    end_at=w.end_at,
                    notice_sent_at=w.notice_sent_at,
                    description=w.description,
                    valid_exclusion=w.is_valid_exclusion,
                ))

        report.maintenance_windows = maintenance_windows
        valid_windows = [w for w in maintenance_windows if w.valid_exclusion]

        # ── 4. Calculate downtime ──
        total_downtime = Decimal("0")
        excluded_downtime = Decimal("0")

        for incident in all_alarms:
            total_downtime += incident.duration_minutes

            # Check if incident overlaps with a valid maintenance window
            overlap_minutes = self._maintenance_overlap(incident, valid_windows)

            if overlap_minutes >= incident.duration_minutes:
                # Entirely within maintenance — exclude completely
                incident.excluded = True
                incident.exclusion_reason = "Planned maintenance"
                excluded_downtime += incident.duration_minutes
            elif overlap_minutes > 0:
                # Partially overlapping — exclude the overlap portion
                excluded_downtime += overlap_minutes

        report.total_downtime_minutes = total_downtime.quantize(Decimal("0.01"))
        report.excluded_downtime_minutes = excluded_downtime.quantize(Decimal("0.01"))
        report.unplanned_downtime_minutes = (total_downtime - excluded_downtime).quantize(Decimal("0.01"))

        # Split incidents
        report.contributing_incidents = [i for i in all_alarms if not i.excluded]
        report.excluded_incidents = [i for i in all_alarms if i.excluded]

        # ── 5. Calculate availability ──
        if total_minutes > 0:
            report.availability_pct = (
                (total_minutes - report.unplanned_downtime_minutes)
                / total_minutes
                * Decimal("100")
            ).quantize(Decimal("0.0001"), ROUND_HALF_UP)

        report.sla_met = report.availability_pct >= sla_target

        # ── 6. SLA credit ──
        report.credit_tier_pct = get_sla_credit_pct(availability_class, report.availability_pct)

        # Get colo fee for credit calculation
        rate_colo = get_rate(self.session, contract.id, RateType.COLO_PER_KW, period_start)
        if rate_colo:
            total_committed_kw = sum(a.committed_kw for a in assignments)
            report.monthly_colo_fee = (total_committed_kw * rate_colo.rate_value).quantize(
                Decimal("0.01"), ROUND_HALF_UP,
            )

        report.credit_amount = (
            report.monthly_colo_fee * report.credit_tier_pct
        ).quantize(Decimal("0.01"), ROUND_HALF_UP)

        # ── 7. Breach warning (live tracking) ──
        report.breach_warning, report.breach_warning_message = self._check_breach_trajectory(
            report, year, month,
        )

        # ── 8. Summary text ──
        report.summary_text = self._build_summary(report)

        # ── 9. Trending (prior 3 months) ──
        report.trend = await self._get_trend(customer_id, year, month)

        return report

    def _maintenance_overlap(
        self,
        incident: AlarmIncident,
        windows: List[MaintenanceWindow],
    ) -> Decimal:
        """
        Calculate how many minutes of an incident overlap with
        valid planned maintenance windows.
        """
        if not windows:
            return Decimal("0")

        incident_start = incident.raised_at
        incident_end = incident.cleared_at or datetime.now(timezone.utc)

        total_overlap = Decimal("0")

        for window in windows:
            # Check for overlap
            overlap_start = max(incident_start, window.start_at)
            overlap_end = min(incident_end, window.end_at)

            if overlap_start < overlap_end:
                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                total_overlap += Decimal(str(overlap_seconds / 60))

        return total_overlap.quantize(Decimal("0.01"), ROUND_HALF_UP)

    def _check_breach_trajectory(
        self,
        report: SLAReport,
        year: int,
        month: int,
    ) -> Tuple[bool, str]:
        """
        Check if current-month tracking is below target.
        For mid-month queries, projects whether the SLA will be breached.
        """
        today = date.today()
        period_end = report.period_end

        # Only warn if we're in the current billing period
        if not (report.period_start <= today < period_end):
            if not report.sla_met:
                return True, (
                    f"SLA BREACHED: {report.availability_pct}% vs "
                    f"{report.sla_target_pct}% target. "
                    f"Credit: {report.credit_tier_pct * 100:.0f}% of colo fee "
                    f"(${report.credit_amount:,.2f})"
                )
            return False, ""

        # Mid-month: calculate remaining downtime budget
        days_elapsed = (today - report.period_start).days
        days_total = (period_end - report.period_start).days

        if days_elapsed == 0:
            return False, ""

        # Max allowed downtime for the full month
        max_downtime = report.total_minutes * (Decimal("1") - report.sla_target_pct / Decimal("100"))

        remaining_budget = max_downtime - report.unplanned_downtime_minutes

        if remaining_budget <= Decimal("0"):
            return True, (
                f"SLA BREACH IN PROGRESS: {report.unplanned_downtime_minutes:.0f} min downtime "
                f"already exceeds budget of {max_downtime:.0f} min. "
                f"Current availability: {report.availability_pct}%"
            )

        # Warn if less than 25% of budget remaining
        budget_pct_used = (
            report.unplanned_downtime_minutes / max_downtime * Decimal("100")
        ) if max_downtime > 0 else Decimal("0")

        if budget_pct_used > Decimal("75"):
            return True, (
                f"SLA AT RISK: {budget_pct_used:.0f}% of downtime budget consumed "
                f"({report.unplanned_downtime_minutes:.0f} of {max_downtime:.0f} min). "
                f"Only {remaining_budget:.0f} min remaining for rest of month."
            )

        return False, ""

    def _build_summary(self, report: SLAReport) -> str:
        """Build human-readable summary for invoice."""
        lines = [
            f"Availability: {report.availability_pct}% "
            f"(target: {report.sla_target_pct}%, class {report.availability_class})",
        ]

        if report.contributing_incidents:
            lines.append(
                f"Incidents: {len(report.contributing_incidents)} "
                f"({report.unplanned_downtime_minutes:.0f} min unplanned downtime)"
            )

        if report.excluded_incidents:
            lines.append(
                f"Excluded: {len(report.excluded_incidents)} incidents during planned maintenance"
            )

        if report.sla_met:
            lines.append("SLA: MET")
        else:
            lines.append(
                f"SLA: BREACHED — credit {report.credit_tier_pct * 100:.0f}% "
                f"of colo fee (${report.credit_amount:,.2f})"
            )

        return " | ".join(lines)

    async def _get_trend(
        self,
        customer_id: int,
        year: int,
        month: int,
    ) -> List[Dict[str, Any]]:
        """
        Return SLA data for prior 3 months for trend comparison.
        In production, this reads from stored SLA reports. Here we
        return placeholder structure.
        """
        trend = []
        for i in range(1, 4):
            m = month - i
            y = year
            if m <= 0:
                m += 12
                y -= 1

            trend.append({
                "year": y,
                "month": m,
                "availability_pct": None,       # populated from historical reports
                "downtime_minutes": None,
                "incidents": None,
                "sla_met": None,
            })

        return trend

    # ─────────────────────────────────────────
    # Live tracking (for current month)
    # ─────────────────────────────────────────

    async def get_live_status(
        self,
        customer_id: int,
    ) -> Dict[str, Any]:
        """
        Real-time SLA tracking for the current month.
        Called by the /sla/current API endpoint.
        """
        today = date.today()
        report = await self.calculate(customer_id, today.year, today.month)

        # Time accounting
        now = datetime.now(timezone.utc)
        start_of_month = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
        elapsed_minutes = Decimal(str((now - start_of_month).total_seconds() / 60))

        max_allowed = report.total_minutes * (Decimal("1") - report.sla_target_pct / Decimal("100"))
        budget_remaining = max_allowed - report.unplanned_downtime_minutes
        budget_pct = (
            (budget_remaining / max_allowed * Decimal("100")).quantize(Decimal("0.1"))
            if max_allowed > 0 else Decimal("100")
        )

        # Active incidents right now
        active_incidents = [
            i for i in report.incidents
            if i.cleared_at is None
        ]

        return {
            "customer_id": customer_id,
            "month": f"{today.year}-{today.month:02d}",
            "availability_class": report.availability_class,
            "sla_target_pct": str(report.sla_target_pct),
            "current_availability_pct": str(report.availability_pct),
            "sla_met": report.sla_met,
            "elapsed_minutes": str(elapsed_minutes.quantize(Decimal("0.1"))),
            "total_minutes": str(report.total_minutes),
            "downtime_minutes": str(report.unplanned_downtime_minutes),
            "budget_remaining_minutes": str(budget_remaining.quantize(Decimal("0.1"))),
            "budget_remaining_pct": str(budget_pct),
            "active_incidents": len(active_incidents),
            "total_incidents_mtd": len(report.contributing_incidents),
            "breach_warning": report.breach_warning,
            "breach_warning_message": report.breach_warning_message,
        }


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """Integration test with stub alarm data."""
    from unittest.mock import MagicMock

    print("=" * 60)
    print("SLA Engine — Stub Test")
    print("=" * 60)

    # ── Mock session & DB objects ──
    mock_session = MagicMock(spec=Session)

    mock_contract = MagicMock(spec=Contract)
    mock_contract.id = 1
    mock_contract.customer_id = 1
    mock_contract.contract_type = ContractType.COLO_MSA
    mock_contract.start_date = date(2026, 1, 1)
    mock_contract.end_date = None
    mock_contract.rate_schedules = []

    mock_assignment = MagicMock(spec=ContractRackAssignment)
    mock_assignment.block_id = "BALD-BLK-01"
    mock_assignment.committed_kw = Decimal("420")
    mock_assignment.availability_class = AvailabilityClass.B  # 99.9%
    mock_assignment.rack_ids = ["R01", "R02", "R03", "R04", "R05", "R06"]

    mock_rate = MagicMock()
    mock_rate.rate_value = Decimal("170.00")

    # Planned maintenance: 1 window with valid 48h notice
    mock_maintenance = MagicMock(spec=PlannedMaintenance)
    mock_maintenance.id = 1
    mock_maintenance.block_id = "BALD-BLK-01"
    mock_maintenance.start_at = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)
    mock_maintenance.end_at = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
    mock_maintenance.notice_sent_at = datetime(2026, 1, 12, 10, 0, tzinfo=timezone.utc)
    mock_maintenance.description = "Scheduled CDU filter replacement"
    mock_maintenance.is_valid_exclusion = True  # notice > 48h before start

    # Patch module-level functions
    import sys
    this_module = sys.modules[__name__]

    _orig_get_active = get_active_contract
    _orig_get_rack = get_customer_rack_assignments
    _orig_get_maintenance = get_planned_maintenance_windows
    _orig_get_rate = get_rate
    _orig_get_sla_credit = get_sla_credit_pct

    this_module.get_active_contract = lambda s, cid, ct=None: mock_contract
    this_module.get_customer_rack_assignments = lambda s, cid, bid=None: [mock_assignment]
    this_module.get_planned_maintenance_windows = lambda s, bid, ps, pe, valid_only=True: (
        [mock_maintenance] if not valid_only else
        ([mock_maintenance] if mock_maintenance.is_valid_exclusion else [])
    )
    this_module.get_rate = lambda s, cid, rt, d=None: mock_rate

    try:
        stub_api = StubStreamBClient()
        engine = SLAEngine(stub_api, mock_session)
        report = await engine.calculate(customer_id=1, year=2026, month=1)

        # ── Display ──
        print(f"\nPeriod: {report.period_start} → {report.period_end}")
        print(f"Class: {report.availability_class} (target: {report.sla_target_pct}%)")
        print(f"Total minutes in period: {report.total_minutes}")
        print()
        print(f"─── Availability ───────────────────────────────────")
        print(f"  Availability:          {report.availability_pct}%")
        print(f"  Total downtime:        {report.total_downtime_minutes} min")
        print(f"  Excluded (maintenance): {report.excluded_downtime_minutes} min")
        print(f"  Unplanned downtime:    {report.unplanned_downtime_minutes} min")
        print(f"  SLA met:               {report.sla_met}")
        print()

        print(f"─── Credit ─────────────────────────────────────────")
        print(f"  Monthly colo fee:      ${report.monthly_colo_fee:,.2f}")
        print(f"  Credit tier:           {report.credit_tier_pct * 100:.0f}%")
        print(f"  Credit amount:         ${report.credit_amount:,.2f}")
        print()

        print(f"─── Incidents ({len(report.incidents)} total) ──────────────────────")
        for inc in report.incidents:
            status = "EXCLUDED" if inc.excluded else "COUNTED"
            print(f"  [{inc.priority}] {inc.alarm_id}")
            print(f"       {inc.description}")
            print(f"       Duration: {inc.duration_minutes} min | {status}")
            if inc.excluded:
                print(f"       Reason: {inc.exclusion_reason}")
            print()

        print(f"─── Maintenance Windows ────────────────────────────")
        for mw in report.maintenance_windows:
            valid = "✓ valid" if mw.valid_exclusion else "✗ invalid (late notice)"
            print(f"  {mw.description}")
            print(f"    {mw.start_at} → {mw.end_at} [{valid}]")
        print()

        if report.breach_warning:
            print(f"⚠ {report.breach_warning_message}")
            print()

        print(f"Summary: {report.summary_text}")
        print()

        # ── Assertions ──
        assert report.total_minutes == Decimal("44640"), "Jan should be 44640 min"
        assert report.availability_pct <= Decimal("100"), "Can't exceed 100%"
        assert report.availability_pct > Decimal("0"), "Should be > 0%"
        assert len(report.incidents) > 0, "Should have incidents from stub"
        assert report.monthly_colo_fee == Decimal("71400.00"), "420kW × $170 = $71,400"

        # With ~45+25+25 min downtime and 44640 min total → ~99.79%
        # Class B target is 99.9% → likely breached
        print(f"  Calculated availability: {report.availability_pct}%")
        if not report.sla_met:
            assert report.credit_amount > 0, "Breached SLA should have credit"
            print(f"  SLA breached → credit ${report.credit_amount:,.2f}")

        print("\n✓ All assertions passed")

    finally:
        this_module.get_active_contract = _orig_get_active
        this_module.get_customer_rack_assignments = _orig_get_rack
        this_module.get_planned_maintenance_windows = _orig_get_maintenance
        this_module.get_rate = _orig_get_rate
        this_module.get_sla_credit_pct = _orig_get_sla_credit
        await stub_api.close()


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
