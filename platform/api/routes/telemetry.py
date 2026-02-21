"""
GET /telemetry — query historical telemetry with automatic tier selection
GET /telemetry/latest — last known value per sensor for a block
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, TimeRange
from ..schemas import (
    TelemetryResponse, TelemetryPoint,
    BlockLatestResponse, LatestValueResponse,
)

router = APIRouter()


@router.get("/telemetry", response_model=TelemetryResponse)
async def query_telemetry(
    sensor_id: int = Query(..., description="Sensor ID"),
    time_range: TimeRange = Depends(),
    agg: Optional[str] = Query(
        None,
        description="Aggregate tier override: raw, 1min, 5min, 1hour, 1day. Auto-selected if omitted.",
        regex="^(raw|1min|5min|1hour|1day)$",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Query telemetry for a single sensor over a time range.

    Automatically selects the optimal aggregate tier based on the time span,
    or accepts an explicit override via the `agg` parameter.

    Returns: avg, min, max, last, count, quality for each time bucket.
    """
    # Verify sensor exists and get metadata
    sensor_result = await db.execute(text("""
        SELECT s.id, s.tag, s.unit FROM sensors s WHERE s.id = :id
    """), {"id": sensor_id})
    sensor = sensor_result.fetchone()
    if not sensor:
        raise HTTPException(404, f"Sensor {sensor_id} not found")

    # Call the query_telemetry() function (Task 3)
    result = await db.execute(text("""
        SELECT bucket, val_avg, val_min, val_max, val_last,
               sample_count, quality_ratio
        FROM query_telemetry(:sensor_id, :start, :end, :agg)
    """), {
        "sensor_id": sensor_id,
        "start": time_range.start,
        "end": time_range.end,
        "agg": agg,
    })
    rows = result.fetchall()

    # Determine which tier was used
    tier_result = await db.execute(text("""
        SELECT select_aggregate_tier(:start, :end)
    """), {"start": time_range.start, "end": time_range.end})
    tier = agg or tier_result.scalar_one()

    points = [TelemetryPoint(
        bucket=r.bucket,
        val_avg=r.val_avg,
        val_min=r.val_min,
        val_max=r.val_max,
        val_last=r.val_last,
        sample_count=r.sample_count,
        quality_ratio=r.quality_ratio,
    ) for r in rows]

    return TelemetryResponse(
        sensor_id=sensor.id,
        sensor_tag=sensor.tag,
        unit=sensor.unit,
        tier=tier,
        start=time_range.start,
        end=time_range.end,
        point_count=len(points),
        data=points,
    )


@router.get("/telemetry/latest", response_model=BlockLatestResponse)
async def get_latest_values(
    block_slug: str = Query(..., description="Block slug"),
    subsystem: Optional[str] = Query(None, description="Filter by subsystem"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the last known value for every sensor in a block.

    Uses a DISTINCT ON query for efficiency — single pass over the
    telemetry hypertable's most recent chunk.
    """
    query = """
        SELECT DISTINCT ON (t.sensor_id)
            t.sensor_id,
            s.tag,
            e.subsystem,
            s.unit,
            t.value,
            t.quality,
            t.time AS timestamp
        FROM telemetry t
        JOIN sensors s    ON s.id = t.sensor_id
        JOIN equipment e  ON e.id = s.equipment_id
        JOIN blocks b     ON b.id = e.block_id
        WHERE b.slug = :block_slug
          AND t.time > now() - interval '1 hour'
    """
    params = {"block_slug": block_slug}

    if subsystem:
        query += " AND e.subsystem = :subsystem"
        params["subsystem"] = subsystem

    query += " ORDER BY t.sensor_id, t.time DESC"

    result = await db.execute(text(query), params)
    rows = result.fetchall()

    readings = [LatestValueResponse(
        sensor_id=r.sensor_id,
        tag=r.tag,
        subsystem=r.subsystem,
        unit=r.unit,
        value=r.value,
        quality=r.quality,
        timestamp=r.timestamp,
    ) for r in rows]

    return BlockLatestResponse(
        block_slug=block_slug,
        sensor_count=len(readings),
        readings=readings,
    )


@router.get("/telemetry/multi")
async def query_multi_sensor(
    sensor_ids: str = Query(..., description="Comma-separated sensor IDs"),
    time_range: TimeRange = Depends(),
    agg: Optional[str] = Query(None, regex="^(raw|1min|5min|1hour|1day)$"),
    db: AsyncSession = Depends(get_db),
):
    """
    Query telemetry for multiple sensors in one call.
    Used by Stream D dashboards that render multiple trends on one chart.
    """
    try:
        ids = [int(x.strip()) for x in sensor_ids.split(",")]
    except ValueError:
        raise HTTPException(400, "sensor_ids must be comma-separated integers")

    if len(ids) > 50:
        raise HTTPException(400, "Maximum 50 sensors per multi-query")

    # Determine tier
    tier_result = await db.execute(text("""
        SELECT select_aggregate_tier(:start, :end)
    """), {"start": time_range.start, "end": time_range.end})
    tier = agg or tier_result.scalar_one()

    results = {}
    for sid in ids:
        result = await db.execute(text("""
            SELECT bucket, val_avg, val_min, val_max, val_last,
                   sample_count, quality_ratio
            FROM query_telemetry(:sensor_id, :start, :end, :agg)
        """), {
            "sensor_id": sid,
            "start": time_range.start,
            "end": time_range.end,
            "agg": agg,
        })
        results[sid] = [dict(
            bucket=r.bucket.isoformat(),
            val_avg=r.val_avg, val_min=r.val_min,
            val_max=r.val_max, val_last=r.val_last,
            sample_count=r.sample_count,
        ) for r in result.fetchall()]

    return {
        "tier": tier,
        "start": time_range.start.isoformat(),
        "end": time_range.end.isoformat(),
        "sensors": results,
    }
