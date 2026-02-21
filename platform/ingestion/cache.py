"""
MCS Stream B — Sensor Cache

Resolves MQTT topic tags to integer sensor PKs using Redis as a write-through
cache backed by the sensors table.  Critical for ingestion throughput — avoids
a DB round-trip on every message (~5,000/sec per block).

Cache key format:  sensor:{site_id}:{block_id}:{subsystem}:{tag}
Cache value:       sensor integer PK (the hypertable partition-friendly int)
"""

import logging
from typing import Optional

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings

logger = logging.getLogger("mcs.cache")


class SensorCache:
    """Two-tier cache: in-process dict → Redis → Postgres."""

    def __init__(self) -> None:
        self._local: dict[str, int] = {}  # hot path — zero-latency
        self._redis: Optional[aioredis.Redis] = None
        self._misses = 0
        self._hits = 0

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await self._redis.ping()
        logger.info("Sensor cache connected to Redis")

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    def _cache_key(self, site_id: str, block_id: str, subsystem: str, tag: str) -> str:
        return f"sensor:{site_id}:{block_id}:{subsystem}:{tag}"

    async def resolve(
        self,
        site_id: str,
        block_id: str,
        subsystem: str,
        tag: str,
        db: AsyncSession,
    ) -> Optional[int]:
        """
        Resolve an MQTT topic path to a sensor PK.

        Returns None if the sensor is not registered — caller should
        route to dead letter queue.
        """
        key = self._cache_key(site_id, block_id, subsystem, tag)

        # ── Tier 1: in-process dict ──────────────────────────────────────
        if key in self._local:
            self._hits += 1
            return self._local[key]

        # ── Tier 2: Redis ────────────────────────────────────────────────
        if self._redis:
            cached = await self._redis.get(key)
            if cached is not None:
                sensor_id = int(cached)
                self._local[key] = sensor_id
                self._hits += 1
                return sensor_id

        # ── Tier 3: Postgres ─────────────────────────────────────────────
        self._misses += 1
        result = await db.execute(
            text("""
                SELECT s.id
                FROM   sensors s
                JOIN   equipment e ON e.id = s.equipment_id
                JOIN   blocks b   ON b.id = e.block_id
                JOIN   sites st   ON st.id = b.site_id
                WHERE  st.slug = :site_id
                  AND  b.slug  = :block_id
                  AND  e.subsystem = :subsystem
                  AND  s.tag   = :tag
                LIMIT 1
            """),
            {
                "site_id": site_id,
                "block_id": block_id,
                "subsystem": subsystem,
                "tag": tag,
            },
        )
        row = result.fetchone()
        if row is None:
            return None

        sensor_id = row[0]

        # Write back through both tiers
        self._local[key] = sensor_id
        if self._redis:
            await self._redis.set(key, str(sensor_id), ex=settings.SENSOR_CACHE_TTL)

        return sensor_id

    async def warm(self, db: AsyncSession) -> int:
        """
        Pre-load the entire sensor registry into the cache at startup.
        Returns the number of sensors loaded.
        """
        result = await db.execute(
            text("""
                SELECT st.slug, b.slug, e.subsystem, s.tag, s.id
                FROM   sensors s
                JOIN   equipment e ON e.id = s.equipment_id
                JOIN   blocks b   ON b.id = e.block_id
                JOIN   sites st   ON st.id = b.site_id
                WHERE  st.status = 'active'
            """)
        )
        rows = result.fetchall()
        pipe = self._redis.pipeline() if self._redis else None

        for site_slug, block_slug, subsystem, tag, sensor_id in rows:
            key = self._cache_key(site_slug, block_slug, subsystem, tag)
            self._local[key] = sensor_id
            if pipe:
                pipe.set(key, str(sensor_id), ex=settings.SENSOR_CACHE_TTL)

        if pipe:
            await pipe.execute()

        logger.info("Sensor cache warmed: %d sensors loaded", len(rows))
        return len(rows)

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0,
            "local_size": len(self._local),
        }
