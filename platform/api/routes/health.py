"""GET /health, GET /stats"""

import time
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db

router = APIRouter()

_start_time = time.monotonic()


@router.get("/health")
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    """Health check — verifies DB and Redis connectivity."""
    db_ok = False
    redis_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    try:
        await request.app.state.redis.ping()
        redis_ok = True
    except Exception:
        pass

    status = "ok" if (db_ok and redis_ok) else "degraded"
    return {
        "status": status,
        "version": "0.1.0",
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }


@router.get("/stats")
async def system_stats(db: AsyncSession = Depends(get_db)):
    """System statistics — data volumes, alarm counts, aggregate health."""

    # Telemetry volume
    vol = await db.execute(text("""
        SELECT
            count(*) AS total_rows,
            pg_size_pretty(pg_total_relation_size('telemetry')) AS table_size,
            min(time) AS oldest,
            max(time) AS newest
        FROM telemetry
        WHERE time > now() - interval '1 hour'
    """))
    vol_row = vol.fetchone()

    # Aggregate health
    try:
        agg = await db.execute(text("SELECT * FROM cagg_refresh_status"))
        agg_rows = [dict(r._mapping) for r in agg.fetchall()]
    except Exception:
        agg_rows = []

    # Alarm summary
    alarm = await db.execute(text("""
        SELECT
            count(*) FILTER (WHERE state = 'ACTIVE')    AS active,
            count(*) FILTER (WHERE state = 'ACKED')     AS acked,
            count(*) FILTER (WHERE state = 'SHELVED')   AS shelved,
            count(*) FILTER (WHERE state = 'SUPPRESSED') AS suppressed,
            count(*) FILTER (WHERE state = 'RTN_UNACK') AS rtn_unack
        FROM alarms WHERE state != 'CLEARED'
    """))
    alarm_row = alarm.fetchone()

    # DLQ count
    dlq = await db.execute(text("""
        SELECT count(*) FROM dead_letter_queue
        WHERE received_at > now() - interval '24 hours'
    """))
    dlq_count = dlq.scalar_one()

    return {
        "telemetry": {
            "rows_last_hour": vol_row.total_rows or 0,
            "table_size": vol_row.table_size,
            "oldest_in_window": vol_row.oldest.isoformat() if vol_row.oldest else None,
            "newest_in_window": vol_row.newest.isoformat() if vol_row.newest else None,
        },
        "aggregates": agg_rows,
        "alarms": {
            "active": alarm_row.active or 0,
            "acked": alarm_row.acked or 0,
            "shelved": alarm_row.shelved or 0,
            "suppressed": alarm_row.suppressed or 0,
            "rtn_unack": alarm_row.rtn_unack or 0,
        },
        "dead_letter_24h": dlq_count,
    }
