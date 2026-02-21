"""
MCS Stream B — Dead Letter Queue

Any message that fails parsing, validation, or sensor resolution gets
written to the dead_letter_queue table for later investigation.

Categories:
  - PARSE_ERROR:    malformed JSON or missing required fields
  - TOPIC_ERROR:    topic doesn't match expected microlink/{site}/{block}/{subsystem}/{tag}
  - SENSOR_UNKNOWN: valid message but sensor tag not in registry
  - VALUE_ERROR:    value outside physically plausible range (NaN, Inf, etc.)
  - INTERNAL_ERROR: unexpected exception during processing
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import settings

logger = logging.getLogger("mcs.dlq")


class DeadLetterQueue:
    """Async dead letter writer — batches DLQ inserts to avoid blocking the hot path."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._count = 0

    async def send(
        self,
        topic: str,
        payload: Optional[str],
        error_category: str,
        error_message: str,
    ) -> None:
        """Write a single dead letter row.  Fire-and-forget — errors logged, not raised."""
        if not settings.DLQ_ENABLED:
            return

        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text("""
                        INSERT INTO dead_letter_queue
                            (received_at, mqtt_topic, raw_payload, error_category, error_message)
                        VALUES
                            (:received_at, :topic, :payload, :category, :message)
                    """),
                    {
                        "received_at": datetime.now(timezone.utc),
                        "topic": topic,
                        "payload": payload[:4000] if payload else None,  # truncate massive payloads
                        "category": error_category,
                        "message": error_message[:1000],
                    },
                )
            self._count += 1
        except Exception:
            logger.exception("Failed to write dead letter (topic=%s)", topic)

    @property
    def count(self) -> int:
        return self._count
