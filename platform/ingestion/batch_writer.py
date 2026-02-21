"""
MCS Stream B — Batch Writer

Accumulates telemetry rows in memory and flushes them to TimescaleDB in bulk
using COPY-style inserts (via executemany + unnest).

Performance target: sustain 50,000 inserts/sec (10 blocks × 5,000/sec).

Flush triggers:
  1. Batch reaches BATCH_SIZE rows
  2. Timer hits BATCH_FLUSH_INTERVAL seconds
  3. Graceful shutdown signal

Backpressure: if pending rows exceed BATCH_MAX_PENDING, new rows are dropped
and counted as overflow.  This protects memory under sustained overload.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import settings

logger = logging.getLogger("mcs.writer")


@dataclass(slots=True)
class TelemetryRow:
    """Single telemetry point ready for DB insertion."""
    time: str           # ISO 8601 timestamp from sensor
    sensor_id: int      # integer PK from sensor cache
    value: float        # measured value
    quality: int        # 0=GOOD, 1=UNCERTAIN, 2=BAD


QUALITY_MAP = {
    "GOOD": 0,
    "UNCERTAIN": 1,
    "BAD": 2,
}


@dataclass
class WriterStats:
    rows_written: int = 0
    rows_dropped: int = 0
    flushes: int = 0
    flush_errors: int = 0
    last_flush_ms: float = 0
    last_flush_rows: int = 0


class BatchWriter:
    """
    Async batch writer that accumulates telemetry rows and periodically
    flushes them to TimescaleDB.
    """

    def __init__(self) -> None:
        self._buffer: list[TelemetryRow] = []
        self._lock = asyncio.Lock()
        self._engine: Optional[AsyncEngine] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self.stats = WriterStats()

    async def start(self) -> None:
        """Create the connection pool and start the periodic flush loop."""
        self._engine = create_async_engine(
            settings.database_url,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info(
            "Batch writer started — batch_size=%d, flush_interval=%.1fs",
            settings.BATCH_SIZE,
            settings.BATCH_FLUSH_INTERVAL,
        )

    async def stop(self) -> None:
        """Flush remaining rows and shut down cleanly."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush()

        if self._engine:
            await self._engine.dispose()
        logger.info(
            "Batch writer stopped — total written: %d, dropped: %d",
            self.stats.rows_written,
            self.stats.rows_dropped,
        )

    async def enqueue(self, row: TelemetryRow) -> bool:
        """
        Add a row to the write buffer.
        Returns False if the row was dropped due to backpressure.
        """
        async with self._lock:
            if len(self._buffer) >= settings.BATCH_MAX_PENDING:
                self.stats.rows_dropped += 1
                return False
            self._buffer.append(row)

        # Trigger immediate flush if batch is full
        if len(self._buffer) >= settings.BATCH_SIZE:
            asyncio.create_task(self._flush())

        return True

    async def _flush_loop(self) -> None:
        """Periodic flush on a timer."""
        while self._running:
            await asyncio.sleep(settings.BATCH_FLUSH_INTERVAL)
            if self._buffer:
                await self._flush()

    async def _flush(self) -> None:
        """
        Take everything from the buffer and bulk-insert it.

        Uses a single multi-row INSERT with unnest for maximum throughput.
        TimescaleDB's chunk indexing handles the rest.
        """
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        t0 = time.monotonic()

        try:
            async with self._engine.begin() as conn:
                # Build parameter arrays for unnest-based bulk insert
                times = [r.time for r in batch]
                sensor_ids = [r.sensor_id for r in batch]
                values = [r.value for r in batch]
                qualities = [r.quality for r in batch]

                await conn.execute(
                    text("""
                        INSERT INTO telemetry (time, sensor_id, value, quality)
                        SELECT unnest(:times ::timestamptz[]),
                               unnest(:sensor_ids ::integer[]),
                               unnest(:values ::double precision[]),
                               unnest(:qualities ::smallint[])
                    """),
                    {
                        "times": times,
                        "sensor_ids": sensor_ids,
                        "values": values,
                        "qualities": qualities,
                    },
                )

            elapsed_ms = (time.monotonic() - t0) * 1000
            self.stats.rows_written += len(batch)
            self.stats.flushes += 1
            self.stats.last_flush_ms = round(elapsed_ms, 2)
            self.stats.last_flush_rows = len(batch)

            if elapsed_ms > 500:
                logger.warning(
                    "Slow flush: %d rows in %.0fms", len(batch), elapsed_ms
                )
            else:
                logger.debug(
                    "Flushed %d rows in %.1fms", len(batch), elapsed_ms
                )

        except Exception:
            # Put rows back at the front of the buffer for retry
            logger.exception("Flush failed — %d rows returned to buffer", len(batch))
            self.stats.flush_errors += 1
            async with self._lock:
                self._buffer = batch + self._buffer
                # Trim to max if overflow
                if len(self._buffer) > settings.BATCH_MAX_PENDING:
                    overflow = len(self._buffer) - settings.BATCH_MAX_PENDING
                    self._buffer = self._buffer[:settings.BATCH_MAX_PENDING]
                    self.stats.rows_dropped += overflow
