"""
MCS Stream B — Alarm Engine (main orchestrator)

Subscribes to the Redis alarm channel (published by the ingestion service),
evaluates thresholds, manages ISA-18.2 lifecycle, cascade suppression,
shelve timers, and publishes alarm events to WebSocket consumers (Stream D).

Architecture:
    Ingestion Service (Task 2)
        │
        ▼ Redis pub/sub (mcs:alarms:inbound)
        │
    ┌───┴────────────────────────────────────────────────────┐
    │                    ALARM ENGINE                         │
    │                                                         │
    │  ┌───────────┐   ┌──────────────┐   ┌──────────────┐  │
    │  │ Threshold  │──▶│ State Machine │──▶│  Cascade     │  │
    │  │ Evaluator  │   │ (ISA-18.2)   │   │  Suppressor  │  │
    │  └───────────┘   └──────┬───────┘   └──────────────┘  │
    │                         │                               │
    │  ┌───────────┐   ┌──────▼───────┐   ┌──────────────┐  │
    │  │ Shelve     │   │ Persistence  │──▶│  Event Log   │  │
    │  │ Manager    │   │ (DB write)   │   │  (immutable) │  │
    │  └───────────┘   └──────────────┘   └──────────────┘  │
    │                                                         │
    │  ┌──────────────────────────────────────────────────┐  │
    │  │ Redis pub/sub (mcs:alarms:outbound)              │  │
    │  │ → Stream D WebSocket → NOC dashboard alarm feed  │  │
    │  └──────────────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────────────┘

Run as: python -m alarm_engine
"""

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from .config import (
    AlarmState, AlarmPriority, AlarmEngineConfig,
    DEFAULT_CASCADE_RULES,
)
from .state_machine import AlarmInstance, TransitionResult
from .threshold import ThresholdRegistry, SensorThresholds
from .cascade import CascadeEngine
from .persistence import AlarmStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mcs.alarm.engine")

# Import settings from the ingestion config (shared infra)
import sys
sys.path.insert(0, "..")
try:
    from ingestion.config import settings
except ImportError:
    # Fallback for standalone runs
    from dataclasses import dataclass as _dc

    @_dc
    class _Settings:
        REDIS_URL: str = "redis://redis:6379/0"
        database_url: str = "postgresql+asyncpg://mcs_admin:localdev@timescaledb:5432/mcs"
        ALARM_REDIS_CHANNEL: str = "mcs:alarms:inbound"
    settings = _Settings()


OUTBOUND_CHANNEL = "mcs:alarms:outbound"  # Stream D consumes this


class AlarmEngine:
    """
    Main alarm engine — processes signals, manages state, and publishes events.
    """

    def __init__(self, config: Optional[AlarmEngineConfig] = None) -> None:
        self.config = config or AlarmEngineConfig()
        self.thresholds = ThresholdRegistry()
        self.cascade = CascadeEngine(DEFAULT_CASCADE_RULES)
        self._store: Optional[AlarmStore] = None
        self._redis_sub: Optional[aioredis.Redis] = None
        self._redis_pub: Optional[aioredis.Redis] = None

        # In-memory alarm state (authoritative during runtime)
        self._active_alarms: dict[int, AlarmInstance] = {}

        # Stats
        self._signals_processed = 0
        self._alarms_raised = 0
        self._alarms_cleared = 0
        self._running = False

    async def start(self) -> None:
        """Initialize subsystems and start processing."""
        logger.info("=" * 60)
        logger.info("MCS Stream B — Alarm Engine starting")
        logger.info("=" * 60)

        # Database
        engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
        )
        session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self._store = AlarmStore(session_factory)

        # Redis
        self._redis_sub = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        self._redis_pub = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

        # Load state from database
        self._active_alarms = await self._store.load_active_alarms()
        threshold_rows = await self._store.load_thresholds()
        self.thresholds.load_from_rows(threshold_rows)

        logger.info(
            "Engine ready: %d active alarms, %d threshold configs, %d cascade rules",
            len(self._active_alarms),
            self.thresholds.count,
            len(DEFAULT_CASCADE_RULES),
        )

        # Start background tasks
        self._running = True
        await asyncio.gather(
            self._subscribe_loop(),
            self._shelve_monitor_loop(),
            self._stale_alarm_loop(),
            self._metrics_loop(),
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Alarm engine shutting down...")
        self._running = False
        if self._redis_sub:
            await self._redis_sub.aclose()
        if self._redis_pub:
            await self._redis_pub.aclose()
        logger.info("Alarm engine stopped")

    # ── Signal Processing ────────────────────────────────────────────────

    async def _subscribe_loop(self) -> None:
        """Subscribe to the inbound alarm channel and process signals."""
        pubsub = self._redis_sub.pubsub()
        await pubsub.subscribe(settings.ALARM_REDIS_CHANNEL)
        logger.info("Subscribed to %s", settings.ALARM_REDIS_CHANNEL)

        try:
            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue

                try:
                    signal = json.loads(message["data"])
                    await self._process_signal(signal)
                except Exception:
                    logger.exception("Error processing alarm signal")
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()

    async def _process_signal(self, signal: dict) -> None:
        """
        Process a single alarm signal from the ingestion service.

        Signal format:
        {
            "sensor_id": 1234,
            "priority": "P0",
            "value": 85.3,
            "timestamp": "2026-02-21T10:30:00Z",
            "site_id": "baldwinsville",
            "block_id": "block-01",
            "subsystem": "thermal-l1",
            "tag": "TT-101"
        }
        """
        self._signals_processed += 1
        sensor_id = signal["sensor_id"]
        value = signal["value"]
        timestamp = datetime.fromisoformat(signal["timestamp"].replace("Z", "+00:00"))
        priority_str = signal["priority"]

        # ── Step 1: Check thresholds ─────────────────────────────────
        sensor_thresholds = self.thresholds.get(sensor_id)
        if sensor_thresholds:
            evaluations = sensor_thresholds.evaluate(value, self.config)
            for threshold_def, in_alarm in evaluations:
                if in_alarm:
                    await self._handle_alarm_condition(
                        sensor_id=sensor_id,
                        priority=threshold_def.priority,
                        value=value,
                        timestamp=timestamp,
                        signal=signal,
                        threshold=threshold_def.value,
                        direction=threshold_def.direction,
                    )
                else:
                    await self._handle_clear_condition(
                        sensor_id=sensor_id,
                        threshold_def=threshold_def,
                        value=value,
                        timestamp=timestamp,
                    )
        else:
            # No threshold config — use the priority from the signal directly
            # (Stream A or the edge controller determined the alarm)
            try:
                priority = AlarmPriority.from_string(priority_str)
            except (KeyError, ValueError):
                return

            await self._handle_alarm_condition(
                sensor_id=sensor_id,
                priority=priority,
                value=value,
                timestamp=timestamp,
                signal=signal,
            )

    async def _handle_alarm_condition(
        self,
        sensor_id: int,
        priority: AlarmPriority,
        value: float,
        timestamp: datetime,
        signal: dict,
        threshold: Optional[float] = None,
        direction: Optional[str] = None,
    ) -> None:
        """Handle a value that is in alarm condition."""

        # Get or create alarm instance
        alarm = self._active_alarms.get(sensor_id)

        if alarm and alarm.state in (AlarmState.ACTIVE, AlarmState.ACKED):
            # Already alarming — just update last value
            alarm.last_value = value
            alarm.last_seen = timestamp
            return

        if alarm is None or alarm.state == AlarmState.CLEARED:
            # New alarm
            alarm = AlarmInstance(
                sensor_id=sensor_id,
                priority=priority,
                site_id=signal.get("site_id", ""),
                block_id=signal.get("block_id", ""),
                subsystem=signal.get("subsystem", ""),
                tag=signal.get("tag", ""),
            )

        # Check cascade: would this be suppressed?
        suppressor = self.cascade.would_be_suppressed(alarm, self._active_alarms)
        if suppressor is not None:
            alarm.raise_alarm(value, timestamp, threshold, direction)
            alarm.suppress(suppressor, timestamp)
            self._active_alarms[sensor_id] = alarm
            alarm_id = await self._store.insert_alarm(alarm)
            await self._store.log_event(alarm, "alarm_suppressed", {
                "suppressed_by_sensor_id": suppressor,
                "value": value,
            })
            return

        # Raise the alarm
        result = alarm.raise_alarm(value, timestamp, threshold, direction)
        if result != TransitionResult.OK:
            return

        self._active_alarms[sensor_id] = alarm
        self._alarms_raised += 1

        # Persist
        await self._store.insert_alarm(alarm)
        await self._store.log_event(alarm, "alarm_raised", {
            "value": value,
            "threshold": threshold,
            "direction": direction,
        })

        # Check if this alarm is a cascade cause
        suppressed = self.cascade.on_alarm_raised(alarm, self._active_alarms)
        for s_alarm in suppressed:
            await self._store.update_alarm(s_alarm)
            await self._store.log_event(s_alarm, "alarm_suppressed", {
                "suppressed_by_sensor_id": sensor_id,
            })

        # Publish to outbound channel for Stream D
        await self._publish_alarm_event("alarm_raised", alarm)

    async def _handle_clear_condition(
        self,
        sensor_id: int,
        threshold_def,
        value: float,
        timestamp: datetime,
    ) -> None:
        """Handle a value that has returned to normal."""
        alarm = self._active_alarms.get(sensor_id)
        if alarm is None:
            return
        if alarm.state in (AlarmState.CLEARED, AlarmState.SHELVED, AlarmState.SUPPRESSED):
            return

        # Check deadband before clearing
        from .threshold import SensorThresholds
        if not SensorThresholds.check_clear_with_deadband(value, threshold_def, self.config):
            # Value returned but not past deadband — don't clear yet
            alarm.last_value = value
            alarm.last_seen = timestamp
            return

        result = alarm.clear_condition(value, timestamp, self.config)
        if result != TransitionResult.OK:
            return

        self._alarms_cleared += 1

        # Persist
        await self._store.update_alarm(alarm)

        event_type = "alarm_cleared" if alarm.state == AlarmState.CLEARED else "alarm_rtn_unack"
        await self._store.log_event(alarm, event_type, {"value": value})

        # If fully cleared, check cascade unsuppression
        if alarm.state == AlarmState.CLEARED:
            unsuppressed = self.cascade.on_alarm_cleared(alarm, self._active_alarms)
            for u_alarm in unsuppressed:
                await self._store.update_alarm(u_alarm)
                await self._store.log_event(u_alarm, "alarm_unsuppressed")
                # TODO: re-evaluate unsuppressed alarms against current values

        await self._publish_alarm_event(event_type, alarm)

    # ── Operator Actions (called by REST API — Task 5) ───────────────────

    async def acknowledge_alarm(self, sensor_id: int, operator: str) -> Optional[dict]:
        """Operator acknowledges an alarm. Called by REST API."""
        alarm = self._active_alarms.get(sensor_id)
        if alarm is None:
            return None

        timestamp = datetime.now(timezone.utc)
        result = alarm.acknowledge(operator, timestamp)
        if result != TransitionResult.OK:
            return {"status": "no_change", "state": alarm.state.value}

        await self._store.update_alarm(alarm)
        await self._store.log_event(alarm, "alarm_acked", {"operator": operator})
        await self._publish_alarm_event("alarm_acked", alarm)

        return alarm.to_dict()

    async def shelve_alarm(
        self,
        sensor_id: int,
        operator: str,
        reason: str,
        duration_hours: float = 0,
    ) -> Optional[dict]:
        """Operator shelves an alarm. Called by REST API."""
        alarm = self._active_alarms.get(sensor_id)
        if alarm is None:
            return None

        if duration_hours <= 0:
            duration_hours = self.config.default_shelve_duration_hours

        timestamp = datetime.now(timezone.utc)
        result = alarm.shelve(operator, reason, duration_hours, timestamp, self.config)
        if result != TransitionResult.OK:
            return {"status": "invalid", "message": "Shelve requires a reason" if self.config.shelve_requires_reason else "Cannot shelve"}

        await self._store.update_alarm(alarm)
        await self._store.log_event(alarm, "alarm_shelved", {
            "operator": operator,
            "reason": reason,
            "duration_hours": duration_hours,
        })
        await self._publish_alarm_event("alarm_shelved", alarm)

        return alarm.to_dict()

    # ── Background Tasks ─────────────────────────────────────────────────

    async def _shelve_monitor_loop(self) -> None:
        """Periodically check for expired shelved alarms."""
        while self._running:
            await asyncio.sleep(self.config.shelve_reeval_interval_seconds)
            try:
                expired = await self._store.get_shelved_expired()
                for alarm in expired:
                    # Find in active alarms
                    active = self._active_alarms.get(alarm.sensor_id)
                    if active and active.state == AlarmState.SHELVED:
                        timestamp = datetime.now(timezone.utc)
                        active.unshelve(timestamp)
                        await self._store.update_alarm(active)
                        await self._store.log_event(active, "alarm_unshelved", {
                            "reason": "timer_expired",
                        })
                        await self._publish_alarm_event("alarm_unshelved", active)
                        logger.info("Shelve expired: sensor=%d", alarm.sensor_id)

                if expired:
                    logger.info("Shelve monitor: %d alarms unshelved", len(expired))
            except Exception:
                logger.exception("Error in shelve monitor")

    async def _stale_alarm_loop(self) -> None:
        """Detect and clear stale alarms (no new readings for timeout period)."""
        while self._running:
            await asyncio.sleep(60)  # Check every minute
            try:
                now = datetime.now(timezone.utc)
                timeout_minutes = self.config.stale_alarm_timeout_minutes
                stale = []

                for sensor_id, alarm in self._active_alarms.items():
                    if alarm.state not in (AlarmState.ACTIVE, AlarmState.ACKED):
                        continue
                    if alarm.last_seen and (now - alarm.last_seen).total_seconds() > timeout_minutes * 60:
                        stale.append(alarm)

                for alarm in stale:
                    alarm.state = AlarmState.CLEARED
                    alarm.cleared_at = now
                    alarm.transition_count += 1
                    await self._store.update_alarm(alarm)
                    await self._store.log_event(alarm, "alarm_cleared", {
                        "reason": "stale_timeout",
                        "timeout_minutes": timeout_minutes,
                    })
                    logger.warning(
                        "Stale alarm cleared: sensor=%d tag=%s (no data for %d min)",
                        alarm.sensor_id, alarm.tag, timeout_minutes,
                    )
            except Exception:
                logger.exception("Error in stale alarm monitor")

    async def _metrics_loop(self) -> None:
        """Log engine metrics periodically."""
        while self._running:
            await asyncio.sleep(30)
            standing = sum(1 for a in self._active_alarms.values() if a.is_standing)
            suppressed = sum(1 for a in self._active_alarms.values() if a.state == AlarmState.SUPPRESSED)
            shelved = sum(1 for a in self._active_alarms.values() if a.state == AlarmState.SHELVED)

            try:
                hourly_rate = await self._store.get_alarm_rate_per_hour()
            except Exception:
                hourly_rate = -1

            logger.info(
                "ENGINE STATS: signals=%d raised=%d cleared=%d standing=%d suppressed=%d shelved=%d rate=%.1f/hr",
                self._signals_processed, self._alarms_raised, self._alarms_cleared,
                standing, suppressed, shelved, hourly_rate,
            )

            # ISA-18.2 alert
            if hourly_rate > self.config.target_alarms_per_operator_hour * 2:
                logger.warning(
                    "ISA-18.2 WARNING: alarm rate %.1f/hr exceeds 2× target (%d/hr)",
                    hourly_rate, self.config.target_alarms_per_operator_hour,
                )

    # ── Publishing ───────────────────────────────────────────────────────

    async def _publish_alarm_event(self, event_type: str, alarm: AlarmInstance) -> None:
        """Publish alarm event to Redis for Stream D WebSocket consumers."""
        try:
            msg = json.dumps({
                "event": event_type,
                "alarm": alarm.to_dict(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await self._redis_pub.publish(OUTBOUND_CHANNEL, msg)
        except Exception:
            logger.exception("Failed to publish alarm event")

    # ── Query Methods (for REST API) ─────────────────────────────────────

    def get_active_alarms(
        self,
        block_id: Optional[str] = None,
        priority: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[dict]:
        """Return filtered list of active alarms for API responses."""
        results = []
        for alarm in self._active_alarms.values():
            if alarm.state == AlarmState.CLEARED:
                continue
            if block_id and alarm.block_id != block_id:
                continue
            if priority and alarm.priority.name != priority:
                continue
            if state and alarm.state.value != state:
                continue
            results.append(alarm.to_dict())

        # Sort: P0 first, then by raised_at
        results.sort(key=lambda a: (
            AlarmPriority[a["priority"]].value,
            a.get("raised_at") or "",
        ))
        return results

    @property
    def stats(self) -> dict:
        standing = sum(1 for a in self._active_alarms.values() if a.is_standing)
        return {
            "signals_processed": self._signals_processed,
            "alarms_raised": self._alarms_raised,
            "alarms_cleared": self._alarms_cleared,
            "active_count": len(self._active_alarms),
            "standing_count": standing,
            "cascade": self.cascade.stats,
        }


# ── Entry point ──────────────────────────────────────────────────────────

async def main() -> None:
    engine = AlarmEngine()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))

    try:
        await engine.start()
    except asyncio.CancelledError:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
