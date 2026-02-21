"""
MCS Stream B — Alarm Persistence

Handles all database operations for the alarm engine:
  - Loading active alarms on startup
  - Writing alarm state transitions
  - Immutable event logging (audit trail)
  - Loading threshold definitions
  - Querying alarm history

Uses the schema from Task 1 (alarms table, events table).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AlarmState, AlarmPriority
from .state_machine import AlarmInstance

logger = logging.getLogger("mcs.alarm.db")


class AlarmStore:
    """Async database operations for alarm lifecycle."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    # ── Load ─────────────────────────────────────────────────────────────

    async def load_active_alarms(self) -> dict[int, AlarmInstance]:
        """
        Load all non-cleared alarms from the database on startup.
        Returns dict keyed by sensor_id.
        """
        async with self._session_factory() as session:
            result = await session.execute(text("""
                SELECT
                    a.id, a.sensor_id, a.priority, a.state,
                    a.raised_at, a.acked_at, a.acked_by,
                    a.cleared_at, a.shelved_at, a.shelved_by,
                    a.shelved_until, a.shelve_reason,
                    s.tag, e.subsystem,
                    b.slug AS block_slug, st.slug AS site_slug
                FROM alarms a
                JOIN sensors s    ON s.id = a.sensor_id
                JOIN equipment e  ON e.id = s.equipment_id
                JOIN blocks b     ON b.id = e.block_id
                JOIN sites st     ON st.id = b.site_id
                WHERE a.state != 'CLEARED'
                ORDER BY a.raised_at
            """))
            rows = result.fetchall()

        alarms = {}
        for row in rows:
            alarm = AlarmInstance(
                id=row.id,
                sensor_id=row.sensor_id,
                priority=AlarmPriority[row.priority],
                state=AlarmState(row.state),
                raised_at=row.raised_at,
                acked_at=row.acked_at,
                acked_by=row.acked_by,
                cleared_at=row.cleared_at,
                shelved_at=row.shelved_at,
                shelved_by=row.shelved_by,
                shelved_until=row.shelved_until,
                shelve_reason=row.shelve_reason,
                tag=row.tag,
                subsystem=row.subsystem,
                block_id=row.block_slug,
                site_id=row.site_slug,
            )
            alarms[row.sensor_id] = alarm

        logger.info("Loaded %d active alarms from database", len(alarms))
        return alarms

    async def load_thresholds(self) -> list[tuple]:
        """
        Load all sensor threshold definitions.
        Returns: [(sensor_id, tag, alarm_thresholds_json), ...]
        """
        async with self._session_factory() as session:
            result = await session.execute(text("""
                SELECT s.id, s.tag, s.alarm_thresholds_json
                FROM sensors s
                JOIN equipment e ON e.id = s.equipment_id
                JOIN blocks b   ON b.id = e.block_id
                JOIN sites st   ON st.id = b.site_id
                WHERE st.status = 'active'
                  AND s.alarm_thresholds_json IS NOT NULL
            """))
            return result.fetchall()

    # ── Write ────────────────────────────────────────────────────────────

    async def insert_alarm(self, alarm: AlarmInstance) -> int:
        """Insert a new alarm record. Returns the new alarm ID."""
        async with self._session_factory() as session:
            result = await session.execute(
                text("""
                    INSERT INTO alarms
                        (sensor_id, priority, state, raised_at)
                    VALUES
                        (:sensor_id, :priority, :state, :raised_at)
                    RETURNING id
                """),
                {
                    "sensor_id": alarm.sensor_id,
                    "priority": alarm.priority.name,
                    "state": alarm.state.value,
                    "raised_at": alarm.raised_at,
                },
            )
            alarm_id = result.scalar_one()
            await session.commit()

        alarm.id = alarm_id
        return alarm_id

    async def update_alarm(self, alarm: AlarmInstance) -> None:
        """Update an existing alarm record with current state."""
        async with self._session_factory() as session:
            await session.execute(
                text("""
                    UPDATE alarms SET
                        state = :state,
                        acked_at = :acked_at,
                        acked_by = :acked_by,
                        cleared_at = :cleared_at,
                        shelved_at = :shelved_at,
                        shelved_by = :shelved_by,
                        shelved_until = :shelved_until,
                        shelve_reason = :shelve_reason
                    WHERE id = :id
                """),
                {
                    "id": alarm.id,
                    "state": alarm.state.value,
                    "acked_at": alarm.acked_at,
                    "acked_by": alarm.acked_by,
                    "cleared_at": alarm.cleared_at,
                    "shelved_at": alarm.shelved_at,
                    "shelved_by": alarm.shelved_by,
                    "shelved_until": alarm.shelved_until,
                    "shelve_reason": alarm.shelve_reason,
                },
            )
            await session.commit()

    # ── Immutable Event Logging ──────────────────────────────────────────

    async def log_event(
        self,
        alarm: AlarmInstance,
        event_type: str,
        payload: Optional[dict] = None,
    ) -> None:
        """
        Write an immutable event to the events table.
        This is the audit trail — events are never updated or deleted.

        Event types:
          alarm_raised, alarm_acked, alarm_cleared, alarm_rtn_unack,
          alarm_shelved, alarm_unshelved, alarm_suppressed, alarm_unsuppressed
        """
        event_payload = {
            "alarm_id": alarm.id,
            "sensor_id": alarm.sensor_id,
            "tag": alarm.tag,
            "subsystem": alarm.subsystem,
            "priority": alarm.priority.name,
            "state": alarm.state.value,
            "value": alarm.last_value,
            **(payload or {}),
        }

        try:
            async with self._session_factory() as session:
                # Find the block_id for the event
                block_result = await session.execute(
                    text("""
                        SELECT b.id
                        FROM sensors s
                        JOIN equipment e ON e.id = s.equipment_id
                        JOIN blocks b   ON b.id = e.block_id
                        WHERE s.id = :sensor_id
                    """),
                    {"sensor_id": alarm.sensor_id},
                )
                block_row = block_result.fetchone()
                block_id = block_row[0] if block_row else None

                await session.execute(
                    text("""
                        INSERT INTO events
                            (block_id, event_type, payload, created_at)
                        VALUES
                            (:block_id, :event_type, :payload, :created_at)
                    """),
                    {
                        "block_id": block_id,
                        "event_type": event_type,
                        "payload": json.dumps(event_payload),
                        "created_at": datetime.now(timezone.utc),
                    },
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to log alarm event (sensor=%d, type=%s)", alarm.sensor_id, event_type)

    # ── Queries ──────────────────────────────────────────────────────────

    async def get_standing_alarm_count(self, block_slug: Optional[str] = None) -> int:
        """Count standing (ACTIVE + RTN_UNACK) alarms, optionally filtered by block."""
        query = """
            SELECT count(*)
            FROM alarms a
            WHERE a.state IN ('ACTIVE', 'RTN_UNACK')
        """
        params = {}
        if block_slug:
            query += """
                AND a.sensor_id IN (
                    SELECT s.id FROM sensors s
                    JOIN equipment e ON e.id = s.equipment_id
                    JOIN blocks b ON b.id = e.block_id
                    WHERE b.slug = :block_slug
                )
            """
            params["block_slug"] = block_slug

        async with self._session_factory() as session:
            result = await session.execute(text(query), params)
            return result.scalar_one()

    async def get_alarm_rate_per_hour(self, hours: int = 1) -> float:
        """Calculate alarm raise rate over the last N hours."""
        async with self._session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT count(*) / :hours::float
                    FROM alarms
                    WHERE raised_at > now() - make_interval(hours => :hours)
                """),
                {"hours": hours},
            )
            return result.scalar_one() or 0.0

    async def get_shelved_expired(self) -> list[AlarmInstance]:
        """Find shelved alarms whose timer has expired."""
        async with self._session_factory() as session:
            result = await session.execute(text("""
                SELECT
                    a.id, a.sensor_id, a.priority, a.state,
                    a.shelved_until, s.tag, e.subsystem,
                    b.slug AS block_slug, st.slug AS site_slug
                FROM alarms a
                JOIN sensors s    ON s.id = a.sensor_id
                JOIN equipment e  ON e.id = s.equipment_id
                JOIN blocks b     ON b.id = e.block_id
                JOIN sites st     ON st.id = b.site_id
                WHERE a.state = 'SHELVED'
                  AND a.shelved_until < now()
            """))
            rows = result.fetchall()

        alarms = []
        for row in rows:
            alarms.append(AlarmInstance(
                id=row.id,
                sensor_id=row.sensor_id,
                priority=AlarmPriority[row.priority],
                state=AlarmState.SHELVED,
                shelved_until=row.shelved_until,
                tag=row.tag,
                subsystem=row.subsystem,
                block_id=row.block_slug,
                site_id=row.site_slug,
            ))
        return alarms
