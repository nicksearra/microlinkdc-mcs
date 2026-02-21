"""
MCS Stream B — Alarm State Machine

Manages individual alarm lifecycle per ISA-18.2.
Each alarm instance tracks its own state, timestamps, and transition history.

State diagram:
                        ┌──────────────────────┐
                        │                      │
    ┌───────┐    raise  │  ┌────────┐   ack    │  ┌─────────┐   clear
    │CLEARED│──────────▶│  │ ACTIVE │─────────▶│  │  ACKED  │──────────▶ CLEARED
    └───────┘           │  └────┬───┘          │  └────┬────┘
                        │       │              │       │
                        │  clear│(no ack)      │  clear│(already acked)
                        │       ▼              │       │
                        │  ┌──────────┐  ack   │       │
                        │  │RTN_UNACK │────────┼───────┘
                        │  └──────────┘        │
                        │                      │
                        │     shelve (from any) │
                        │       │              │
                        │       ▼              │
                        │  ┌──────────┐        │
                        │  │ SHELVED  │ expire  │
                        │  └──────────┘────────┘
                        └──────────────────────┘
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from .config import AlarmState, AlarmPriority, AlarmEngineConfig, RESPONSE_TARGETS

logger = logging.getLogger("mcs.alarm.state")


class TransitionResult(str, Enum):
    OK = "OK"
    NO_CHANGE = "NO_CHANGE"
    INVALID = "INVALID"


@dataclass
class AlarmInstance:
    """
    A single alarm instance tracking its full lifecycle.

    One instance per (sensor_id, priority) combination.
    Immutable events are written to the events table for audit.
    """
    # Identity
    id: Optional[int] = None            # DB primary key (set after INSERT)
    sensor_id: int = 0
    priority: AlarmPriority = AlarmPriority.P2

    # Current state
    state: AlarmState = AlarmState.CLEARED
    value_at_raise: Optional[float] = None
    value_at_clear: Optional[float] = None

    # Timestamps
    raised_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    acked_by: Optional[str] = None
    cleared_at: Optional[datetime] = None
    shelved_at: Optional[datetime] = None
    shelved_by: Optional[str] = None
    shelved_until: Optional[datetime] = None
    shelve_reason: Optional[str] = None
    suppressed_by_alarm_id: Optional[int] = None

    # Context
    site_id: str = ""
    block_id: str = ""
    subsystem: str = ""
    tag: str = ""

    # Threshold that triggered this alarm
    threshold_value: Optional[float] = None
    threshold_direction: Optional[str] = None  # "HIGH" or "LOW"

    # Tracking
    transition_count: int = 0
    last_value: Optional[float] = None
    last_seen: Optional[datetime] = None

    def raise_alarm(
        self,
        value: float,
        timestamp: datetime,
        threshold: Optional[float] = None,
        direction: Optional[str] = None,
    ) -> TransitionResult:
        """Transition to ACTIVE state."""
        if self.state in (AlarmState.ACTIVE, AlarmState.ACKED):
            # Already in alarm — update value but don't re-raise
            self.last_value = value
            self.last_seen = timestamp
            return TransitionResult.NO_CHANGE

        if self.state == AlarmState.SHELVED:
            # Shelved alarms don't transition on new readings
            self.last_value = value
            self.last_seen = timestamp
            return TransitionResult.NO_CHANGE

        if self.state == AlarmState.SUPPRESSED:
            # Suppressed — record value but stay suppressed
            self.last_value = value
            self.last_seen = timestamp
            return TransitionResult.NO_CHANGE

        # Valid raise: from CLEARED or RTN_UNACK
        self.state = AlarmState.ACTIVE
        self.value_at_raise = value
        self.raised_at = timestamp
        self.cleared_at = None
        self.acked_at = None
        self.acked_by = None
        self.threshold_value = threshold
        self.threshold_direction = direction
        self.last_value = value
        self.last_seen = timestamp
        self.transition_count += 1

        logger.info(
            "RAISE %s [%s] sensor=%d tag=%s value=%.2f threshold=%s",
            self.priority.name, self.state.value,
            self.sensor_id, self.tag, value,
            f"{direction} {threshold}" if threshold else "signal",
        )
        return TransitionResult.OK

    def acknowledge(self, operator: str, timestamp: datetime) -> TransitionResult:
        """Operator acknowledges the alarm."""
        if self.state == AlarmState.ACTIVE:
            self.state = AlarmState.ACKED
            self.acked_at = timestamp
            self.acked_by = operator
            self.transition_count += 1
            logger.info("ACK alarm sensor=%d by %s", self.sensor_id, operator)
            return TransitionResult.OK

        if self.state == AlarmState.RTN_UNACK:
            # Returned but unacked → acknowledge clears it fully
            self.state = AlarmState.CLEARED
            self.acked_at = timestamp
            self.acked_by = operator
            self.transition_count += 1
            logger.info("ACK+CLEAR alarm sensor=%d by %s", self.sensor_id, operator)
            return TransitionResult.OK

        logger.debug("ACK ignored — alarm sensor=%d in state %s", self.sensor_id, self.state.value)
        return TransitionResult.NO_CHANGE

    def clear_condition(
        self,
        value: float,
        timestamp: datetime,
        config: AlarmEngineConfig,
    ) -> TransitionResult:
        """Value has returned to normal (with deadband)."""
        self.last_value = value
        self.last_seen = timestamp

        if self.state == AlarmState.ACKED:
            # Already acknowledged → clear fully
            self.state = AlarmState.CLEARED
            self.value_at_clear = value
            self.cleared_at = timestamp
            self.transition_count += 1
            logger.info("CLEAR alarm sensor=%d (was acked)", self.sensor_id)
            return TransitionResult.OK

        if self.state == AlarmState.ACTIVE:
            # Not yet acknowledged → go to RTN_UNACK
            self.state = AlarmState.RTN_UNACK
            self.value_at_clear = value
            self.cleared_at = timestamp
            self.transition_count += 1
            logger.info("RTN_UNACK alarm sensor=%d (clear but not acked)", self.sensor_id)
            return TransitionResult.OK

        return TransitionResult.NO_CHANGE

    def shelve(
        self,
        operator: str,
        reason: str,
        duration_hours: float,
        timestamp: datetime,
        config: AlarmEngineConfig,
    ) -> TransitionResult:
        """Temporarily suppress alarm."""
        if self.state == AlarmState.CLEARED:
            return TransitionResult.INVALID

        if config.shelve_requires_reason and not reason.strip():
            logger.warning("Shelve rejected — reason required (sensor=%d)", self.sensor_id)
            return TransitionResult.INVALID

        capped_hours = min(duration_hours, config.max_shelve_duration_hours)
        self.state = AlarmState.SHELVED
        self.shelved_at = timestamp
        self.shelved_by = operator
        self.shelved_until = timestamp + timedelta(hours=capped_hours)
        self.shelve_reason = reason
        self.transition_count += 1

        logger.info(
            "SHELVE alarm sensor=%d by %s for %.1fh reason='%s'",
            self.sensor_id, operator, capped_hours, reason,
        )
        return TransitionResult.OK

    def unshelve(self, timestamp: datetime) -> TransitionResult:
        """Unshelve — either manual or timer expired. Re-evaluates condition."""
        if self.state != AlarmState.SHELVED:
            return TransitionResult.NO_CHANGE

        # If last known value is still in alarm, go back to ACTIVE
        # Otherwise go to CLEARED. The caller (engine) handles re-evaluation.
        self.state = AlarmState.CLEARED
        self.shelved_at = None
        self.shelved_by = None
        self.shelved_until = None
        self.shelve_reason = None
        self.transition_count += 1

        logger.info("UNSHELVE alarm sensor=%d", self.sensor_id)
        return TransitionResult.OK

    def suppress(self, cause_alarm_id: int, timestamp: datetime) -> TransitionResult:
        """Suppress due to cascade rule."""
        if self.state in (AlarmState.CLEARED, AlarmState.SUPPRESSED):
            return TransitionResult.NO_CHANGE

        prev_state = self.state
        self.state = AlarmState.SUPPRESSED
        self.suppressed_by_alarm_id = cause_alarm_id
        self.transition_count += 1

        logger.info(
            "SUPPRESS alarm sensor=%d (was %s) by cause alarm %d",
            self.sensor_id, prev_state.value, cause_alarm_id,
        )
        return TransitionResult.OK

    def unsuppress(self, timestamp: datetime) -> TransitionResult:
        """Remove cascade suppression — re-evaluate if still in alarm."""
        if self.state != AlarmState.SUPPRESSED:
            return TransitionResult.NO_CHANGE

        self.state = AlarmState.CLEARED
        self.suppressed_by_alarm_id = None
        self.transition_count += 1

        logger.info("UNSUPPRESS alarm sensor=%d", self.sensor_id)
        return TransitionResult.OK

    @property
    def is_standing(self) -> bool:
        """Is this alarm currently requiring attention?"""
        return self.state in (AlarmState.ACTIVE, AlarmState.RTN_UNACK)

    @property
    def response_time_seconds(self) -> Optional[float]:
        """Time from raise to ack in seconds. None if not yet acked."""
        if self.raised_at and self.acked_at:
            return (self.acked_at - self.raised_at).total_seconds()
        return None

    @property
    def response_target_met(self) -> Optional[bool]:
        """Did the operator respond within the priority target?"""
        rt = self.response_time_seconds
        if rt is None:
            return None
        target = RESPONSE_TARGETS.get(self.priority)
        if target is None or target == 0:
            return rt <= 30  # P0: within 30 seconds
        return rt <= target

    def to_dict(self) -> dict:
        """Serialise for API responses and event logging."""
        return {
            "id": self.id,
            "sensor_id": self.sensor_id,
            "priority": self.priority.name,
            "state": self.state.value,
            "site_id": self.site_id,
            "block_id": self.block_id,
            "subsystem": self.subsystem,
            "tag": self.tag,
            "value_at_raise": self.value_at_raise,
            "value_at_clear": self.value_at_clear,
            "threshold_value": self.threshold_value,
            "threshold_direction": self.threshold_direction,
            "raised_at": self.raised_at.isoformat() if self.raised_at else None,
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "acked_by": self.acked_by,
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
            "shelved_at": self.shelved_at.isoformat() if self.shelved_at else None,
            "shelved_by": self.shelved_by,
            "shelved_until": self.shelved_until.isoformat() if self.shelved_until else None,
            "shelve_reason": self.shelve_reason,
            "suppressed_by_alarm_id": self.suppressed_by_alarm_id,
            "transition_count": self.transition_count,
            "last_value": self.last_value,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "response_time_seconds": self.response_time_seconds,
        }
