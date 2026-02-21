"""
MCS Stream B — Alarm Detector (Ingest-Time)

Lightweight alarm signal extraction that runs in the ingestion hot path.
When a message arrives with alarm != null, we publish the signal to a Redis
channel for the alarm engine (Task 4) to pick up and manage lifecycle.

This module does NOT manage alarm state (ACTIVE/ACK/CLEARED/SHELVED) — that's
the alarm engine's job.  We just detect and forward.

Redis channel: mcs:alarms:inbound
Message format (JSON):
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

import json
import logging
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger("mcs.alarm_detect")


@dataclass(slots=True)
class AlarmSignal:
    sensor_id: int
    priority: str        # P0, P1, P2, P3
    value: float
    timestamp: str
    site_id: str
    block_id: str
    subsystem: str
    tag: str


class AlarmDetector:
    """Publishes alarm signals to Redis for the alarm engine."""

    VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._published = 0
        self._invalid = 0

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        logger.info("Alarm detector connected to Redis pub/sub")

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def check_and_publish(self, signal: AlarmSignal) -> bool:
        """
        Publish an alarm signal if priority is valid.
        Returns True if published, False if skipped.
        """
        if signal.priority not in self.VALID_PRIORITIES:
            self._invalid += 1
            logger.warning(
                "Invalid alarm priority '%s' for sensor %d",
                signal.priority,
                signal.sensor_id,
            )
            return False

        msg = json.dumps({
            "sensor_id": signal.sensor_id,
            "priority": signal.priority,
            "value": signal.value,
            "timestamp": signal.timestamp,
            "site_id": signal.site_id,
            "block_id": signal.block_id,
            "subsystem": signal.subsystem,
            "tag": signal.tag,
        })

        try:
            await self._redis.publish(settings.ALARM_REDIS_CHANNEL, msg)
            self._published += 1
            if signal.priority in ("P0", "P1"):
                logger.info(
                    "ALARM %s: sensor=%d tag=%s value=%.2f site=%s block=%s",
                    signal.priority,
                    signal.sensor_id,
                    signal.tag,
                    signal.value,
                    signal.site_id,
                    signal.block_id,
                )
            return True
        except Exception:
            logger.exception("Failed to publish alarm signal for sensor %d", signal.sensor_id)
            return False

    @property
    def stats(self) -> dict:
        return {
            "published": self._published,
            "invalid": self._invalid,
        }
