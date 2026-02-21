"""
MCS Stream C — Task 7: PagerDuty Integration
==============================================
Service that consumes alarms from Stream B and routes them to
the correct on-call teams via PagerDuty Events API v2.

Routing:
  P0 → NOC + on-call engineer (24/7 immediate)
  P1 → NOC + site engineer (business hours first, escalate after 15min)
  P2 → Maintenance queue (next business day)
  P3 → Weekly digest only (batched, no individual incidents)

Features:
  - Trigger/resolve lifecycle tied to alarm state
  - Dedup key prevents duplicate incidents
  - Planned maintenance suppression
  - P3 batching with weekly digest
  - Retry with exponential backoff on API failure
  - Full audit logging

Usage:
    router = PagerDutyRouter(config, api_client, session)
    await router.start_polling()          # continuous polling mode
    await router.process_alarm(alarm)     # single alarm processing
    await router.send_weekly_digest()     # P3 batch digest
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional, Dict, Any

import httpx

from billing_models import (
    Session,
    get_planned_maintenance_windows,
)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logger = logging.getLogger("microlink.pagerduty")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

@dataclass
class EscalationPolicy:
    """PagerDuty escalation policy mapping for a priority level."""
    routing_key: str                    # PD Events API v2 integration key
    escalation_policy_id: str           # PD escalation policy ID
    urgency: str = "high"               # high / low
    escalation_timeout_min: int = 0     # 0 = immediate, >0 = escalate after N min


@dataclass
class PagerDutyConfig:
    """Configuration for PagerDuty integration."""
    # Per-priority routing keys (each maps to a PD service/integration)
    routing: Dict[str, EscalationPolicy] = field(default_factory=dict)

    # API settings
    events_api_url: str = "https://events.pagerduty.com/v2/enqueue"
    api_token: str = ""                 # REST API token (for managing incidents)
    rest_api_url: str = "https://api.pagerduty.com"

    # Polling
    poll_interval_seconds: int = 15
    stream_b_base_url: str = "http://localhost:8001/api/v1"
    stream_b_token: str = ""

    # Retry
    max_retries: int = 3
    retry_base_delay: float = 1.0       # seconds, exponential backoff

    # Batching
    p3_batch_enabled: bool = True
    p3_digest_day: int = 0              # 0=Monday

    # Suppression
    suppress_during_maintenance: bool = True

    @classmethod
    def default(cls) -> PagerDutyConfig:
        """Default config with placeholder routing keys."""
        return cls(
            routing={
                "P0": EscalationPolicy(
                    routing_key="R0_PLACEHOLDER_KEY_NOC_CRITICAL",
                    escalation_policy_id="P_NOC_24x7",
                    urgency="high",
                    escalation_timeout_min=0,
                ),
                "P1": EscalationPolicy(
                    routing_key="R1_PLACEHOLDER_KEY_NOC_HIGH",
                    escalation_policy_id="P_SITE_ENG",
                    urgency="high",
                    escalation_timeout_min=15,
                ),
                "P2": EscalationPolicy(
                    routing_key="R2_PLACEHOLDER_KEY_MAINTENANCE",
                    escalation_policy_id="P_MAINT_QUEUE",
                    urgency="low",
                    escalation_timeout_min=0,
                ),
                "P3": EscalationPolicy(
                    routing_key="R3_PLACEHOLDER_KEY_DIGEST",
                    escalation_policy_id="P_WEEKLY_DIGEST",
                    urgency="low",
                    escalation_timeout_min=0,
                ),
            },
        )


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

class AlarmState(str, Enum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


@dataclass
class Alarm:
    """Alarm from Stream B."""
    alarm_id: str
    priority: str               # P0, P1, P2, P3
    state: str                  # ACTIVE, CLEARED, ACKNOWLEDGED
    block_id: str
    site_id: str
    sensor_tag: str
    description: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: str = ""
    raised_at: Optional[datetime] = None
    cleared_at: Optional[datetime] = None
    block_name: str = ""
    site_name: str = ""


@dataclass
class PagerDutyEvent:
    """PagerDuty Events API v2 payload."""
    routing_key: str
    event_action: str           # trigger, acknowledge, resolve
    dedup_key: str
    payload: Dict[str, Any] = field(default_factory=dict)
    links: List[Dict[str, str]] = field(default_factory=list)
    images: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "routing_key": self.routing_key,
            "event_action": self.event_action,
            "dedup_key": self.dedup_key,
        }
        if self.event_action == "trigger":
            body["payload"] = self.payload
            if self.links:
                body["links"] = self.links
            if self.images:
                body["images"] = self.images
        return body


@dataclass
class AuditEntry:
    """Audit log entry for PD interactions."""
    timestamp: datetime
    alarm_id: str
    action: str                 # trigger, resolve, suppress, batch, retry, error
    pd_dedup_key: str
    pd_response: Optional[Dict[str, Any]] = None
    success: bool = True
    error_message: str = ""
    retry_count: int = 0


# ─────────────────────────────────────────────
# Stream B alarm client
# ─────────────────────────────────────────────

class StreamBAlarmClient:
    """Polls Stream B for active alarms."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                timeout=15.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_active_alarms(self, block_id: Optional[str] = None) -> List[Dict]:
        client = await self._get_client()
        params: Dict[str, str] = {"state": "ACTIVE"}
        if block_id:
            params["block_id"] = block_id
        resp = await client.get("/alarms", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_alarm_history(
        self, block_id: str, start: str, end: str,
    ) -> List[Dict]:
        client = await self._get_client()
        resp = await client.get(
            "/alarms",
            params={"block_id": block_id, "start": start, "end": end},
        )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────
# PagerDuty API client
# ─────────────────────────────────────────────

class PagerDutyClient:
    """PagerDuty Events API v2 client with retry logic."""

    def __init__(self, config: PagerDutyConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send_event(self, event: PagerDutyEvent) -> Dict[str, Any]:
        """
        Send event to PagerDuty Events API v2 with retry.
        Returns PD response dict or raises after max retries.
        """
        client = await self._get_client()
        last_error = None

        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.post(
                    self.config.events_api_url,
                    json=event.to_dict(),
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code in (200, 201, 202):
                    return resp.json()

                # Rate limited — respect Retry-After
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "30"))
                    logger.warning(
                        f"PD rate limited, waiting {retry_after}s "
                        f"(attempt {attempt + 1}/{self.config.max_retries + 1})"
                    )
                    await asyncio.sleep(retry_after)
                    continue

                # Server error — retry
                if resp.status_code >= 500:
                    last_error = f"PD server error {resp.status_code}: {resp.text}"
                    logger.warning(f"{last_error} (attempt {attempt + 1})")
                else:
                    # Client error — don't retry
                    return {"status": "error", "code": resp.status_code, "message": resp.text}

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = f"PD connection error: {e}"
                logger.warning(f"{last_error} (attempt {attempt + 1})")

            # Exponential backoff
            if attempt < self.config.max_retries:
                delay = self.config.retry_base_delay * (2 ** attempt)
                await asyncio.sleep(delay)

        raise PagerDutyError(f"Failed after {self.config.max_retries + 1} attempts: {last_error}")


class PagerDutyError(Exception):
    pass


# ─────────────────────────────────────────────
# Core router
# ─────────────────────────────────────────────

class PagerDutyRouter:
    """
    Routes MicroLink alarms to PagerDuty based on priority,
    with maintenance suppression, P3 batching, and audit logging.
    """

    def __init__(
        self,
        config: PagerDutyConfig,
        alarm_client: StreamBAlarmClient,
        session: Session,
        pd_client: Optional[PagerDutyClient] = None,
    ):
        self.config = config
        self.alarm_client = alarm_client
        self.session = session
        self.pd_client = pd_client or PagerDutyClient(config)

        # State tracking
        self._tracked_alarms: Dict[str, Alarm] = {}   # alarm_id → last known state
        self._p3_batch: List[Alarm] = []                # queued P3 alarms
        self._audit_log: List[AuditEntry] = []
        self._running = False

    # ─────────────────────────────────────────
    # Polling loop
    # ─────────────────────────────────────────

    async def start_polling(self, block_ids: Optional[List[str]] = None):
        """
        Continuously poll Stream B for active alarms and route to PD.
        Detects new alarms (trigger) and cleared alarms (resolve).
        """
        self._running = True
        logger.info("PagerDuty router started polling")

        while self._running:
            try:
                current_alarms = await self._fetch_all_active(block_ids)
                await self._reconcile(current_alarms)
            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(self.config.poll_interval_seconds)

    def stop_polling(self):
        self._running = False
        logger.info("PagerDuty router stopping")

    async def _fetch_all_active(
        self, block_ids: Optional[List[str]] = None,
    ) -> Dict[str, Alarm]:
        """Fetch all active alarms, optionally filtered by blocks."""
        raw_alarms = await self.alarm_client.get_active_alarms()
        alarms = {}
        for a in raw_alarms:
            alarm = self._parse_alarm(a)
            if block_ids and alarm.block_id not in block_ids:
                continue
            alarms[alarm.alarm_id] = alarm
        return alarms

    async def _reconcile(self, current: Dict[str, Alarm]):
        """
        Compare current active alarms against tracked state.
        New alarms → trigger. Missing alarms → resolve.
        """
        current_ids = set(current.keys())
        tracked_ids = set(self._tracked_alarms.keys())

        # New alarms
        for alarm_id in current_ids - tracked_ids:
            alarm = current[alarm_id]
            await self.process_alarm(alarm)

        # Cleared alarms (were tracked, no longer active)
        for alarm_id in tracked_ids - current_ids:
            alarm = self._tracked_alarms[alarm_id]
            alarm.state = AlarmState.CLEARED.value
            alarm.cleared_at = datetime.now(timezone.utc)
            await self._resolve_alarm(alarm)
            del self._tracked_alarms[alarm_id]

        # Update tracked state
        for alarm_id, alarm in current.items():
            self._tracked_alarms[alarm_id] = alarm

    # ─────────────────────────────────────────
    # Single alarm processing
    # ─────────────────────────────────────────

    async def process_alarm(self, alarm: Alarm):
        """
        Process a single alarm — route based on priority.
        Called by polling loop or directly for webhook/event-driven mode.
        """
        dedup_key = f"microlink-{alarm.alarm_id}"

        # ── Maintenance suppression ──
        if self.config.suppress_during_maintenance:
            if self._is_in_maintenance(alarm):
                self._log_audit(alarm, "suppress", dedup_key, success=True,
                                error_message="Suppressed: within planned maintenance window")
                logger.info(f"Suppressed {alarm.alarm_id} — planned maintenance")
                return

        # ── Priority routing ──
        priority = alarm.priority.upper()

        if priority == "P3" and self.config.p3_batch_enabled:
            # Batch P3 — don't create individual incidents
            self._p3_batch.append(alarm)
            self._log_audit(alarm, "batch", dedup_key, success=True,
                            error_message=f"Batched for weekly digest ({len(self._p3_batch)} queued)")
            logger.debug(f"Batched P3 alarm {alarm.alarm_id}")
            return

        policy = self.config.routing.get(priority)
        if not policy:
            logger.warning(f"No routing policy for priority {priority}")
            return

        # ── Build PD event ──
        event = self._build_trigger_event(alarm, policy, dedup_key)

        # ── Send to PagerDuty ──
        try:
            response = await self.pd_client.send_event(event)
            self._tracked_alarms[alarm.alarm_id] = alarm
            self._log_audit(alarm, "trigger", dedup_key, pd_response=response)
            logger.info(
                f"Triggered PD incident: {alarm.alarm_id} [{priority}] "
                f"→ {policy.escalation_policy_id}"
            )
        except PagerDutyError as e:
            self._log_audit(alarm, "error", dedup_key, success=False,
                            error_message=str(e))
            logger.error(f"Failed to trigger PD for {alarm.alarm_id}: {e}")
            # Queue for retry on next poll cycle
            self._tracked_alarms.pop(alarm.alarm_id, None)

    async def _resolve_alarm(self, alarm: Alarm):
        """Send resolve event to PagerDuty when alarm is cleared."""
        dedup_key = f"microlink-{alarm.alarm_id}"
        priority = alarm.priority.upper()

        if priority == "P3":
            # P3 are batched, nothing to resolve individually
            return

        policy = self.config.routing.get(priority)
        if not policy:
            return

        event = PagerDutyEvent(
            routing_key=policy.routing_key,
            event_action="resolve",
            dedup_key=dedup_key,
        )

        try:
            response = await self.pd_client.send_event(event)
            self._log_audit(alarm, "resolve", dedup_key, pd_response=response)
            logger.info(f"Resolved PD incident: {alarm.alarm_id}")
        except PagerDutyError as e:
            self._log_audit(alarm, "error", dedup_key, success=False,
                            error_message=f"Resolve failed: {e}")
            logger.error(f"Failed to resolve PD for {alarm.alarm_id}: {e}")

    # ─────────────────────────────────────────
    # P3 weekly digest
    # ─────────────────────────────────────────

    async def send_weekly_digest(self) -> Dict[str, Any]:
        """
        Send batched P3 alarms as a single PagerDuty incident.
        Called on schedule (e.g. Monday morning) or manually.
        """
        if not self._p3_batch:
            logger.info("No P3 alarms to digest")
            return {"status": "empty", "count": 0}

        policy = self.config.routing.get("P3")
        if not policy:
            return {"status": "no_routing", "count": len(self._p3_batch)}

        # Build digest summary
        batch = self._p3_batch.copy()
        summary_lines = []
        by_site: Dict[str, List[Alarm]] = {}

        for alarm in batch:
            site = alarm.site_name or alarm.site_id
            by_site.setdefault(site, []).append(alarm)

        for site, alarms in by_site.items():
            summary_lines.append(f"[{site}] {len(alarms)} P3 alarms:")
            for a in alarms[:10]:  # cap at 10 per site in summary
                summary_lines.append(f"  - {a.sensor_tag}: {a.description}")
            if len(alarms) > 10:
                summary_lines.append(f"  ... and {len(alarms) - 10} more")

        dedup_key = f"microlink-p3-digest-{datetime.now(timezone.utc).strftime('%Y-W%W')}"

        event = PagerDutyEvent(
            routing_key=policy.routing_key,
            event_action="trigger",
            dedup_key=dedup_key,
            payload={
                "summary": f"MicroLink P3 Weekly Digest: {len(batch)} alarms across {len(by_site)} sites",
                "source": "microlink-mcs",
                "severity": "info",
                "component": "mcs-business",
                "group": "weekly-digest",
                "class": "P3",
                "custom_details": {
                    "alarm_count": len(batch),
                    "sites": list(by_site.keys()),
                    "digest": "\n".join(summary_lines),
                    "period_start": batch[0].raised_at.isoformat() if batch[0].raised_at else "",
                    "period_end": datetime.now(timezone.utc).isoformat(),
                },
            },
        )

        try:
            response = await self.pd_client.send_event(event)
            # Clear the batch
            self._p3_batch.clear()
            logger.info(f"Sent P3 weekly digest: {len(batch)} alarms")
            return {"status": "sent", "count": len(batch), "pd_response": response}
        except PagerDutyError as e:
            logger.error(f"Failed to send P3 digest: {e}")
            return {"status": "error", "count": len(batch), "error": str(e)}

    # ─────────────────────────────────────────
    # Event builders
    # ─────────────────────────────────────────

    def _build_trigger_event(
        self,
        alarm: Alarm,
        policy: EscalationPolicy,
        dedup_key: str,
    ) -> PagerDutyEvent:
        """Build PD Events API v2 trigger payload."""

        severity_map = {"P0": "critical", "P1": "error", "P2": "warning", "P3": "info"}
        severity = severity_map.get(alarm.priority.upper(), "info")

        custom_details: Dict[str, Any] = {
            "sensor_tag": alarm.sensor_tag,
            "block_id": alarm.block_id,
            "block_name": alarm.block_name or alarm.block_id,
            "site_id": alarm.site_id,
            "site_name": alarm.site_name or alarm.site_id,
            "priority": alarm.priority,
        }

        if alarm.value is not None:
            custom_details["current_value"] = f"{alarm.value} {alarm.unit}"
        if alarm.threshold is not None:
            custom_details["threshold"] = f"{alarm.threshold} {alarm.unit}"
        if alarm.raised_at:
            custom_details["raised_at"] = alarm.raised_at.isoformat()

        return PagerDutyEvent(
            routing_key=policy.routing_key,
            event_action="trigger",
            dedup_key=dedup_key,
            payload={
                "summary": (
                    f"[{alarm.priority}] {alarm.site_name or alarm.site_id} / "
                    f"{alarm.block_name or alarm.block_id}: {alarm.description}"
                ),
                "source": f"microlink-{alarm.site_id}-{alarm.block_id}",
                "severity": severity,
                "component": alarm.sensor_tag,
                "group": alarm.block_id,
                "class": alarm.priority,
                "custom_details": custom_details,
            },
            links=[
                {
                    "href": f"https://mcs.microlink.io/alarms/{alarm.alarm_id}",
                    "text": "View in MCS Dashboard",
                },
            ],
        )

    # ─────────────────────────────────────────
    # Maintenance suppression
    # ─────────────────────────────────────────

    def _is_in_maintenance(self, alarm: Alarm) -> bool:
        """Check if alarm falls within a valid planned maintenance window."""
        now = datetime.now(timezone.utc)
        try:
            windows = get_planned_maintenance_windows(
                self.session,
                alarm.block_id,
                now - timedelta(hours=1),
                now + timedelta(hours=1),
                valid_only=True,
            )
            for w in windows:
                if w.start_at <= now <= w.end_at:
                    return True
        except Exception as e:
            logger.warning(f"Maintenance check failed for {alarm.block_id}: {e}")
        return False

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _parse_alarm(self, raw: Dict) -> Alarm:
        """Parse raw alarm dict from Stream B API."""
        raised_at = None
        if raw.get("raised_at"):
            raised_at = datetime.fromisoformat(raw["raised_at"].replace("Z", "+00:00"))

        cleared_at = None
        if raw.get("cleared_at"):
            cleared_at = datetime.fromisoformat(raw["cleared_at"].replace("Z", "+00:00"))

        return Alarm(
            alarm_id=raw["alarm_id"],
            priority=raw.get("priority", "P2"),
            state=raw.get("state", "ACTIVE"),
            block_id=raw.get("block_id", ""),
            site_id=raw.get("site_id", ""),
            sensor_tag=raw.get("sensor_tag", ""),
            description=raw.get("description", ""),
            value=raw.get("value"),
            threshold=raw.get("threshold"),
            unit=raw.get("unit", ""),
            raised_at=raised_at,
            cleared_at=cleared_at,
            block_name=raw.get("block_name", ""),
            site_name=raw.get("site_name", ""),
        )

    def _log_audit(
        self,
        alarm: Alarm,
        action: str,
        dedup_key: str,
        pd_response: Optional[Dict] = None,
        success: bool = True,
        error_message: str = "",
        retry_count: int = 0,
    ):
        """Record audit entry for every PD interaction."""
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc),
            alarm_id=alarm.alarm_id,
            action=action,
            pd_dedup_key=dedup_key,
            pd_response=pd_response,
            success=success,
            error_message=error_message,
            retry_count=retry_count,
        )
        self._audit_log.append(entry)

        # In production: persist to DB or send to log aggregator
        level = logging.INFO if success else logging.ERROR
        logger.log(level, f"AUDIT: {action} alarm={alarm.alarm_id} dedup={dedup_key} ok={success}")

    def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent audit entries for inspection."""
        return [
            {
                "timestamp": e.timestamp.isoformat(),
                "alarm_id": e.alarm_id,
                "action": e.action,
                "dedup_key": e.pd_dedup_key,
                "success": e.success,
                "error": e.error_message,
                "retry_count": e.retry_count,
            }
            for e in self._audit_log[-limit:]
        ]


# ─────────────────────────────────────────────
# Stub PD client for testing
# ─────────────────────────────────────────────

class StubPagerDutyClient(PagerDutyClient):
    """Records events instead of sending to PD."""

    def __init__(self):
        super().__init__(PagerDutyConfig.default())
        self.sent_events: List[Dict[str, Any]] = []

    async def send_event(self, event: PagerDutyEvent) -> Dict[str, Any]:
        payload = event.to_dict()
        self.sent_events.append(payload)
        return {
            "status": "success",
            "message": "Event processed (stub)",
            "dedup_key": event.dedup_key,
        }


class StubStreamBAlarmClient(StreamBAlarmClient):
    """Returns synthetic active alarms."""

    def __init__(self):
        super().__init__(base_url="stub://")
        self._alarms: List[Dict] = []

    def set_alarms(self, alarms: List[Dict]):
        self._alarms = alarms

    async def get_active_alarms(self, block_id=None) -> List[Dict]:
        if block_id:
            return [a for a in self._alarms if a.get("block_id") == block_id]
        return self._alarms


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """Test the full alarm routing pipeline."""
    from unittest.mock import MagicMock
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("=" * 60)
    print("PagerDuty Integration — Stub Test")
    print("=" * 60)

    mock_session = MagicMock(spec=Session)

    # Patch maintenance lookup to return empty
    this_module = sys.modules[__name__]
    _orig = get_planned_maintenance_windows
    this_module.get_planned_maintenance_windows = lambda s, bid, ps, pe, valid_only=True: []

    try:
        config = PagerDutyConfig.default()
        stub_alarm_client = StubStreamBAlarmClient()
        stub_pd_client = StubPagerDutyClient()

        router = PagerDutyRouter(
            config=config,
            alarm_client=stub_alarm_client,
            session=mock_session,
            pd_client=stub_pd_client,
        )

        now = datetime.now(timezone.utc)

        # ── Test 1: Process individual alarms ──
        print("\n─── Test 1: Individual alarm routing ───────────────")

        test_alarms = [
            Alarm(alarm_id="ALM-001", priority="P0", state="ACTIVE",
                  block_id="BALD-BLK-01", site_id="BALD-01",
                  sensor_tag="UPS-01-STATUS", description="UPS on battery — utility failure",
                  value=0, threshold=1, unit="status", raised_at=now,
                  site_name="Baldwinsville Brewery", block_name="Block 01"),
            Alarm(alarm_id="ALM-002", priority="P1", state="ACTIVE",
                  block_id="BALD-BLK-01", site_id="BALD-01",
                  sensor_tag="TT-CDU1-IN", description="CDU1 inlet temp high",
                  value=42.5, threshold=40.0, unit="°C", raised_at=now,
                  site_name="Baldwinsville Brewery", block_name="Block 01"),
            Alarm(alarm_id="ALM-003", priority="P2", state="ACTIVE",
                  block_id="BALD-BLK-01", site_id="BALD-01",
                  sensor_tag="FAN-03", description="Fan 03 speed degraded",
                  value=2800, threshold=3000, unit="RPM", raised_at=now,
                  site_name="Baldwinsville Brewery", block_name="Block 01"),
            Alarm(alarm_id="ALM-004", priority="P3", state="ACTIVE",
                  block_id="BALD-BLK-01", site_id="BALD-01",
                  sensor_tag="TT-AMB-01", description="Ambient temp sensor drift",
                  value=22.1, threshold=None, unit="°C", raised_at=now,
                  site_name="Baldwinsville Brewery", block_name="Block 01"),
            Alarm(alarm_id="ALM-005", priority="P3", state="ACTIVE",
                  block_id="BALD-BLK-01", site_id="BALD-01",
                  sensor_tag="HUM-01", description="Humidity sensor intermittent",
                  raised_at=now, site_name="Baldwinsville Brewery", block_name="Block 01"),
        ]

        for alarm in test_alarms:
            await router.process_alarm(alarm)

        print(f"  PD events sent: {len(stub_pd_client.sent_events)}")
        print(f"  P3 batch queue: {len(router._p3_batch)}")

        for evt in stub_pd_client.sent_events:
            payload = evt.get("payload", {})
            print(f"    [{payload.get('class', '?')}] {payload.get('severity', '?').upper()}: "
                  f"{payload.get('summary', '?')[:80]}")

        assert len(stub_pd_client.sent_events) == 3, "P0 + P1 + P2 = 3 events (P3 batched)"
        assert len(router._p3_batch) == 2, "Two P3 alarms batched"

        # Verify dedup keys
        dedup_keys = [e["dedup_key"] for e in stub_pd_client.sent_events]
        assert "microlink-ALM-001" in dedup_keys
        assert "microlink-ALM-002" in dedup_keys
        assert "microlink-ALM-003" in dedup_keys

        # Verify severity mapping
        severities = {
            e["dedup_key"]: e["payload"]["severity"]
            for e in stub_pd_client.sent_events
        }
        assert severities["microlink-ALM-001"] == "critical"
        assert severities["microlink-ALM-002"] == "error"
        assert severities["microlink-ALM-003"] == "warning"

        print("  ✓ Routing correct")

        # ── Test 2: Resolve ──
        print("\n─── Test 2: Alarm resolution ───────────────────────")
        stub_pd_client.sent_events.clear()

        alarm_to_resolve = test_alarms[0]  # P0
        alarm_to_resolve.state = "CLEARED"
        alarm_to_resolve.cleared_at = now + timedelta(minutes=30)
        await router._resolve_alarm(alarm_to_resolve)

        assert len(stub_pd_client.sent_events) == 1
        assert stub_pd_client.sent_events[0]["event_action"] == "resolve"
        assert stub_pd_client.sent_events[0]["dedup_key"] == "microlink-ALM-001"
        print("  ✓ Resolve event sent")

        # ── Test 3: P3 weekly digest ──
        print("\n─── Test 3: P3 weekly digest ────────────────────────")
        stub_pd_client.sent_events.clear()

        result = await router.send_weekly_digest()
        assert result["status"] == "sent"
        assert result["count"] == 2
        assert len(stub_pd_client.sent_events) == 1

        digest_evt = stub_pd_client.sent_events[0]
        assert "digest" in digest_evt["dedup_key"]
        assert digest_evt["payload"]["severity"] == "info"
        print(f"  Digest sent: {result['count']} alarms")
        print(f"  Summary: {digest_evt['payload']['summary']}")
        assert len(router._p3_batch) == 0, "Batch should be cleared"
        print("  ✓ Digest sent and batch cleared")

        # ── Test 4: Polling reconciliation ──
        print("\n─── Test 4: Polling reconciliation ─────────────────")
        stub_pd_client.sent_events.clear()
        router._tracked_alarms.clear()

        # First poll: 2 active alarms
        stub_alarm_client.set_alarms([
            {"alarm_id": "ALM-100", "priority": "P1", "state": "ACTIVE",
             "block_id": "BALD-BLK-01", "site_id": "BALD-01",
             "sensor_tag": "TT-CDU2-IN", "description": "CDU2 inlet high",
             "raised_at": now.isoformat()},
            {"alarm_id": "ALM-101", "priority": "P2", "state": "ACTIVE",
             "block_id": "BALD-BLK-01", "site_id": "BALD-01",
             "sensor_tag": "VIB-01", "description": "Pump vibration warning",
             "raised_at": now.isoformat()},
        ])

        current = await router._fetch_all_active()
        await router._reconcile(current)
        assert len(stub_pd_client.sent_events) == 2, "Two new alarms → two triggers"
        print(f"  Poll 1: {len(stub_pd_client.sent_events)} triggers")

        # Second poll: ALM-100 cleared, ALM-101 still active
        stub_pd_client.sent_events.clear()
        stub_alarm_client.set_alarms([
            {"alarm_id": "ALM-101", "priority": "P2", "state": "ACTIVE",
             "block_id": "BALD-BLK-01", "site_id": "BALD-01",
             "sensor_tag": "VIB-01", "description": "Pump vibration warning",
             "raised_at": now.isoformat()},
        ])

        current = await router._fetch_all_active()
        await router._reconcile(current)
        assert len(stub_pd_client.sent_events) == 1, "One cleared → one resolve"
        assert stub_pd_client.sent_events[0]["event_action"] == "resolve"
        print(f"  Poll 2: 1 resolve (ALM-100 cleared)")
        print("  ✓ Reconciliation correct")

        # ── Audit log ──
        print("\n─── Audit Log (last 10) ────────────────────────────")
        for entry in router.get_audit_log(limit=10):
            print(f"  {entry['timestamp'][:19]} | {entry['action']:8s} | "
                  f"{entry['alarm_id']:12s} | ok={entry['success']}")

        print(f"\n  Total audit entries: {len(router._audit_log)}")
        print("\n✓ All assertions passed")

    finally:
        this_module.get_planned_maintenance_windows = _orig


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
