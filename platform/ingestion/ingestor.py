"""
MCS Stream B — MQTT Ingestor (main service)

Subscribes to microlink/# on the MQTT broker, parses each message against
the Stream A contract, resolves sensor PKs via the cache, and batch-writes
telemetry into TimescaleDB.

Architecture:
  MQTT broker
      │
      ▼
  Topic parser ──── invalid? ──→ Dead Letter Queue
      │
      ▼
  Sensor cache ──── unknown? ──→ Dead Letter Queue
      │
      ▼
  Batch writer ──── bulk INSERT ──→ TimescaleDB (telemetry hypertable)
      │
      ▼
  Alarm detector ── alarm? ──→ Redis pub/sub ──→ Alarm Engine (Task 4)

One instance per site.  At 10 blocks × 5,000 msg/sec = 50,000 msg/sec,
the bottleneck is the batch writer, not MQTT parsing.
"""

import asyncio
import json
import logging
import math
import signal
import re
from datetime import datetime, timezone
from typing import Optional

import aiomqtt
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from .config import settings
from .cache import SensorCache
from .batch_writer import BatchWriter, TelemetryRow, QUALITY_MAP
from .dead_letter import DeadLetterQueue
from .alarm_detector import AlarmDetector, AlarmSignal
from .metrics import update_metrics, start_metrics_server

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mcs.ingestor")

# ── Stream A MQTT Contract ───────────────────────────────────────────────
# Topic:   microlink/{site_id}/{block_id}/{subsystem}/{tag}
# Payload: {"ts":"ISO8601","v":float,"u":"unit","q":"GOOD|UNCERTAIN|BAD","alarm":null|"P0"|"P1"|"P2"|"P3"}

TOPIC_PATTERN = re.compile(
    r"^microlink/([a-z0-9_-]+)/([a-z0-9_-]+)/([a-z0-9_-]+)/([A-Za-z0-9_-]+)$"
)

VALID_SUBSYSTEMS = frozenset({
    "electrical",
    "thermal-l1",    # IT secondary (CDU → racks)
    "thermal-l2",    # MicroLink primary glycol
    "thermal-l3",    # Host process water
    "thermal-reject",# Dry cooler rejection
    "thermal-safety",# Leak detection, pressure relief
    "environmental", # Ambient temp, humidity, dust
    "network",       # Switch metrics, latency
    "security",      # Door sensors, CCTV status
})


def parse_topic(topic: str) -> Optional[tuple[str, str, str, str]]:
    """
    Parse an MQTT topic into (site_id, block_id, subsystem, tag).
    Returns None if the topic doesn't match the contract.
    """
    m = TOPIC_PATTERN.match(topic)
    if m is None:
        return None
    site_id, block_id, subsystem, tag = m.groups()
    if subsystem not in VALID_SUBSYSTEMS:
        return None
    return site_id, block_id, subsystem, tag


def parse_payload(raw: bytes) -> Optional[dict]:
    """
    Parse and validate the JSON payload against Stream A's contract.
    Returns the parsed dict or None if invalid.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Required fields
    if not isinstance(data, dict):
        return None
    if "ts" not in data or "v" not in data:
        return None

    # Validate value — reject NaN, Inf
    v = data["v"]
    if not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
        return None

    # Validate quality
    q = data.get("q", "GOOD")
    if q not in QUALITY_MAP:
        return None
    data["_quality"] = QUALITY_MAP[q]

    # Validate timestamp
    try:
        datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    return data


class MQTTIngestor:
    """Main ingestion service — orchestrates all components."""

    def __init__(self) -> None:
        self.cache = SensorCache()
        self.writer = BatchWriter()
        self.alarm_detector = AlarmDetector()
        self._dlq: Optional[DeadLetterQueue] = None
        self._db_sessionmaker = None
        self._running = False
        self._received = 0

    async def start(self) -> None:
        """Initialize all subsystems and start processing."""
        logger.info("=" * 60)
        logger.info("MCS Stream B — MQTT Ingestor starting")
        logger.info("=" * 60)

        # Database engine (shared for cache warmup + DLQ)
        engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
        )
        self._db_sessionmaker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self._dlq = DeadLetterQueue(engine)

        # Connect subsystems
        await self.cache.connect()
        await self.writer.start()
        await self.alarm_detector.connect()

        # Warm the sensor cache
        async with self._db_sessionmaker() as session:
            count = await self.cache.warm(session)
            logger.info("Ready — %d sensors in cache", count)

        # Start metrics server
        await start_metrics_server()

        # Start MQTT consumer
        self._running = True
        await self._consume()

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False
        await self.writer.stop()
        await self.cache.close()
        await self.alarm_detector.close()
        logger.info("Shutdown complete")

    async def _consume(self) -> None:
        """
        Main MQTT consumer loop with auto-reconnect.
        Uses aiomqtt for clean async integration.
        """
        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=settings.MQTT_BROKER,
                    port=settings.MQTT_PORT,
                    username=settings.MQTT_USERNAME,
                    password=settings.MQTT_PASSWORD,
                    identifier=settings.MQTT_CLIENT_ID,
                    keepalive=settings.MQTT_KEEPALIVE,
                ) as client:
                    await client.subscribe(settings.MQTT_TOPIC_ROOT, qos=settings.MQTT_QOS)
                    logger.info(
                        "Connected to MQTT broker %s:%d — subscribed to '%s'",
                        settings.MQTT_BROKER,
                        settings.MQTT_PORT,
                        settings.MQTT_TOPIC_ROOT,
                    )

                    # Start periodic metrics reporter
                    metrics_task = asyncio.create_task(self._report_metrics())

                    async for message in client.messages:
                        if not self._running:
                            break
                        await self._handle_message(message)

                    metrics_task.cancel()

            except aiomqtt.MqttError as e:
                logger.error("MQTT connection lost: %s — reconnecting in 5s", e)
                await asyncio.sleep(5)
            except Exception:
                logger.exception("Unexpected error in MQTT consumer — reconnecting in 10s")
                await asyncio.sleep(10)

    async def _handle_message(self, message: aiomqtt.Message) -> None:
        """Process a single MQTT message through the pipeline."""
        self._received += 1
        topic_str = str(message.topic)
        raw_payload = message.payload

        # ── Step 1: Parse topic ──────────────────────────────────────────
        parsed_topic = parse_topic(topic_str)
        if parsed_topic is None:
            await self._dlq.send(
                topic=topic_str,
                payload=raw_payload.decode("utf-8", errors="replace") if isinstance(raw_payload, bytes) else str(raw_payload),
                error_category="TOPIC_ERROR",
                error_message=f"Topic does not match microlink/{{site}}/{{block}}/{{subsystem}}/{{tag}}",
            )
            return

        site_id, block_id, subsystem, tag = parsed_topic

        # ── Step 2: Parse payload ────────────────────────────────────────
        payload_bytes = raw_payload if isinstance(raw_payload, bytes) else str(raw_payload).encode()
        data = parse_payload(payload_bytes)
        if data is None:
            await self._dlq.send(
                topic=topic_str,
                payload=payload_bytes.decode("utf-8", errors="replace"),
                error_category="PARSE_ERROR",
                error_message="Payload failed JSON parse or validation",
            )
            return

        # ── Step 3: Resolve sensor PK ────────────────────────────────────
        async with self._db_sessionmaker() as session:
            sensor_id = await self.cache.resolve(
                site_id, block_id, subsystem, tag, session
            )

        if sensor_id is None:
            await self._dlq.send(
                topic=topic_str,
                payload=payload_bytes.decode("utf-8", errors="replace"),
                error_category="SENSOR_UNKNOWN",
                error_message=f"No sensor registered for {site_id}/{block_id}/{subsystem}/{tag}",
            )
            return

        # ── Step 4: Enqueue telemetry row ────────────────────────────────
        row = TelemetryRow(
            time=data["ts"],
            sensor_id=sensor_id,
            value=float(data["v"]),
            quality=data["_quality"],
        )
        accepted = await self.writer.enqueue(row)
        if not accepted:
            logger.warning("Backpressure — row dropped for sensor %d", sensor_id)

        # ── Step 5: Check for alarm signal ───────────────────────────────
        alarm_priority = data.get("alarm")
        if alarm_priority is not None:
            signal = AlarmSignal(
                sensor_id=sensor_id,
                priority=alarm_priority,
                value=float(data["v"]),
                timestamp=data["ts"],
                site_id=site_id,
                block_id=block_id,
                subsystem=subsystem,
                tag=tag,
            )
            await self.alarm_detector.check_and_publish(signal)

    async def _report_metrics(self) -> None:
        """Push metrics to the Prometheus exporter every 5 seconds."""
        while self._running:
            cache_stats = self.cache.stats
            alarm_stats = self.alarm_detector.stats
            writer_stats = self.writer.stats

            update_metrics({
                "mqtt_received": self._received,
                "rows_written": writer_stats.rows_written,
                "rows_dropped": writer_stats.rows_dropped,
                "flushes": writer_stats.flushes,
                "flush_errors": writer_stats.flush_errors,
                "last_flush_ms": writer_stats.last_flush_ms,
                "last_flush_rows": writer_stats.last_flush_rows,
                "cache_hits": cache_stats["hits"],
                "cache_misses": cache_stats["misses"],
                "cache_hit_rate": cache_stats["hit_rate"],
                "cache_local_size": cache_stats["local_size"],
                "dlq_count": self._dlq.count if self._dlq else 0,
                "alarms_published": alarm_stats["published"],
                "buffer_size": len(self.writer._buffer),
            })
            await asyncio.sleep(5)


async def main() -> None:
    """Entry point — runs the ingestor with graceful shutdown."""
    ingestor = MQTTIngestor()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(ingestor.stop()))

    try:
        await ingestor.start()
    except asyncio.CancelledError:
        await ingestor.stop()


if __name__ == "__main__":
    asyncio.run(main())
