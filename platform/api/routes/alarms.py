"""
GET  /alarms — list active/filtered alarms
POST /alarms/{sensor_id}/acknowledge — operator ack
POST /alarms/{sensor_id}/shelve — operator shelve with reason
GET  /alarms/history — historical alarm records
GET  /alarms/stats — ISA-18.2 compliance metrics
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, Pagination, OptionalTimeRange, require_operator
from ..schemas import (
    AlarmResponse, AlarmListResponse,
    AlarmAckRequest, AlarmShelveRequest,
)

router = APIRouter()


@router.get("/alarms", response_model=AlarmListResponse)
async def list_alarms(
    state: Optional[str] = Query(None, description="Filter: ACTIVE,ACKED,RTN_UNACK,SHELVED,SUPPRESSED"),
    priority: Optional[str] = Query(None, description="Filter: P0,P1,P2,P3"),
    block_slug: Optional[str] = Query(None),
    site_slug: Optional[str] = Query(None),
    pagination: Pagination = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    List alarms with optional filters.
    Default: returns all non-CLEARED alarms sorted by priority then time.
    """
    conditions = ["a.state != 'CLEARED'"]
    params: dict = {"offset": pagination.offset, "limit": pagination.limit}

    if state:
        states = [s.strip().upper() for s in state.split(",")]
        conditions = [f"a.state = ANY(:states)"]
        params["states"] = states

    if priority:
        priorities = [p.strip().upper() for p in priority.split(",")]
        conditions.append("a.priority = ANY(:priorities)")
        params["priorities"] = priorities

    if block_slug:
        conditions.append("b.slug = :block_slug")
        params["block_slug"] = block_slug

    if site_slug:
        conditions.append("st.slug = :site_slug")
        params["site_slug"] = site_slug

    where = " AND ".join(conditions)

    result = await db.execute(text(f"""
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
        WHERE {where}
        ORDER BY
            CASE a.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
            a.raised_at DESC
        OFFSET :offset LIMIT :limit
    """), params)
    rows = result.fetchall()

    # Count totals
    count_result = await db.execute(text(f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE a.state IN ('ACTIVE', 'RTN_UNACK')) AS standing
        FROM alarms a
        JOIN sensors s    ON s.id = a.sensor_id
        JOIN equipment e  ON e.id = s.equipment_id
        JOIN blocks b     ON b.id = e.block_id
        JOIN sites st     ON st.id = b.site_id
        WHERE {where}
    """), {k: v for k, v in params.items() if k not in ("offset", "limit")})
    counts = count_result.fetchone()

    alarms = [AlarmResponse(
        id=r.id, sensor_id=r.sensor_id, priority=r.priority,
        state=r.state, tag=r.tag, subsystem=r.subsystem,
        site_id=r.site_slug, block_id=r.block_slug,
        raised_at=r.raised_at, acked_at=r.acked_at, acked_by=r.acked_by,
        cleared_at=r.cleared_at, shelved_at=r.shelved_at,
        shelved_by=r.shelved_by, shelved_until=r.shelved_until,
        shelve_reason=r.shelve_reason,
    ) for r in rows]

    return AlarmListResponse(
        total=counts.total,
        standing=counts.standing,
        alarms=alarms,
    )


@router.post("/alarms/{sensor_id}/acknowledge", response_model=AlarmResponse)
async def acknowledge_alarm(
    sensor_id: int,
    body: AlarmAckRequest,
    user: dict = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """
    Acknowledge an active alarm. Transitions ACTIVE → ACKED or RTN_UNACK → CLEARED.
    """
    now = datetime.now(timezone.utc)

    # Find the alarm
    result = await db.execute(text("""
        SELECT a.id, a.state
        FROM alarms a
        WHERE a.sensor_id = :sensor_id AND a.state IN ('ACTIVE', 'RTN_UNACK')
        ORDER BY a.raised_at DESC LIMIT 1
    """), {"sensor_id": sensor_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(404, f"No active alarm for sensor {sensor_id}")

    # Determine new state
    new_state = "ACKED" if row.state == "ACTIVE" else "CLEARED"

    await db.execute(text("""
        UPDATE alarms SET
            state = :new_state,
            acked_at = :now,
            acked_by = :operator
        WHERE id = :id
    """), {"id": row.id, "new_state": new_state, "now": now, "operator": body.operator})

    # Log event
    await db.execute(text("""
        INSERT INTO events (block_id, event_type, payload, created_at)
        SELECT e.block_id, 'alarm_acked',
               jsonb_build_object('alarm_id', :alarm_id, 'sensor_id', :sensor_id, 'operator', :operator),
               :now
        FROM sensors s JOIN equipment e ON e.id = s.equipment_id
        WHERE s.id = :sensor_id
    """), {"alarm_id": row.id, "sensor_id": sensor_id, "operator": body.operator, "now": now})

    await db.commit()

    # Return updated alarm
    updated = await db.execute(text("""
        SELECT a.*, s.tag, e.subsystem, b.slug AS block_slug, st.slug AS site_slug
        FROM alarms a
        JOIN sensors s ON s.id = a.sensor_id
        JOIN equipment e ON e.id = s.equipment_id
        JOIN blocks b ON b.id = e.block_id
        JOIN sites st ON st.id = b.site_id
        WHERE a.id = :id
    """), {"id": row.id})
    r = updated.fetchone()

    return AlarmResponse(
        id=r.id, sensor_id=r.sensor_id, priority=r.priority,
        state=r.state, tag=r.tag, subsystem=r.subsystem,
        site_id=r.site_slug, block_id=r.block_slug,
        raised_at=r.raised_at, acked_at=r.acked_at, acked_by=r.acked_by,
        cleared_at=r.cleared_at,
    )


@router.post("/alarms/{sensor_id}/shelve", response_model=AlarmResponse)
async def shelve_alarm(
    sensor_id: int,
    body: AlarmShelveRequest,
    user: dict = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """
    Shelve an alarm — temporarily suppress it with a mandatory reason.
    Maximum duration: 24 hours.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    shelved_until = now + timedelta(hours=min(body.duration_hours, 24.0))

    result = await db.execute(text("""
        SELECT a.id, a.state
        FROM alarms a
        WHERE a.sensor_id = :sensor_id AND a.state IN ('ACTIVE', 'ACKED', 'RTN_UNACK')
        ORDER BY a.raised_at DESC LIMIT 1
    """), {"sensor_id": sensor_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(404, f"No active alarm for sensor {sensor_id}")

    await db.execute(text("""
        UPDATE alarms SET
            state = 'SHELVED',
            shelved_at = :now,
            shelved_by = :operator,
            shelved_until = :until,
            shelve_reason = :reason
        WHERE id = :id
    """), {
        "id": row.id, "now": now, "operator": body.operator,
        "until": shelved_until, "reason": body.reason,
    })

    # Audit event
    await db.execute(text("""
        INSERT INTO events (block_id, event_type, payload, created_at)
        SELECT e.block_id, 'alarm_shelved',
               jsonb_build_object(
                   'alarm_id', :alarm_id, 'sensor_id', :sensor_id,
                   'operator', :operator, 'reason', :reason,
                   'duration_hours', :hours
               ),
               :now
        FROM sensors s JOIN equipment e ON e.id = s.equipment_id
        WHERE s.id = :sensor_id
    """), {
        "alarm_id": row.id, "sensor_id": sensor_id,
        "operator": body.operator, "reason": body.reason,
        "hours": body.duration_hours, "now": now,
    })

    await db.commit()

    updated = await db.execute(text("""
        SELECT a.*, s.tag, e.subsystem, b.slug AS block_slug, st.slug AS site_slug
        FROM alarms a
        JOIN sensors s ON s.id = a.sensor_id JOIN equipment e ON e.id = s.equipment_id
        JOIN blocks b ON b.id = e.block_id JOIN sites st ON st.id = b.site_id
        WHERE a.id = :id
    """), {"id": row.id})
    r = updated.fetchone()

    return AlarmResponse(
        id=r.id, sensor_id=r.sensor_id, priority=r.priority,
        state=r.state, tag=r.tag, subsystem=r.subsystem,
        site_id=r.site_slug, block_id=r.block_slug,
        raised_at=r.raised_at, shelved_at=r.shelved_at,
        shelved_by=r.shelved_by, shelved_until=r.shelved_until,
        shelve_reason=r.shelve_reason,
    )


@router.get("/alarms/stats")
async def alarm_stats(
    block_slug: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """ISA-18.2 compliance statistics — alarm rates, standing counts, response times."""
    block_filter = ""
    params = {}
    if block_slug:
        block_filter = "AND b.slug = :block_slug"
        params["block_slug"] = block_slug

    result = await db.execute(text(f"""
        SELECT
            count(*) FILTER (WHERE a.state IN ('ACTIVE', 'RTN_UNACK'))    AS standing,
            count(*) FILTER (WHERE a.state = 'ACKED')                      AS acked,
            count(*) FILTER (WHERE a.state = 'SHELVED')                    AS shelved,
            count(*) FILTER (WHERE a.state = 'SUPPRESSED')                 AS suppressed,
            count(*) FILTER (WHERE a.raised_at > now() - interval '1 hour') AS raised_last_hour,
            avg(EXTRACT(EPOCH FROM (a.acked_at - a.raised_at)))
                FILTER (WHERE a.acked_at IS NOT NULL
                    AND a.raised_at > now() - interval '24 hours')         AS avg_response_seconds_24h
        FROM alarms a
        JOIN sensors s ON s.id = a.sensor_id
        JOIN equipment e ON e.id = s.equipment_id
        JOIN blocks b ON b.id = e.block_id
        WHERE a.state != 'CLEARED' {block_filter}
    """), params)
    row = result.fetchone()

    return {
        "standing": row.standing or 0,
        "acked": row.acked or 0,
        "shelved": row.shelved or 0,
        "suppressed": row.suppressed or 0,
        "raised_last_hour": row.raised_last_hour or 0,
        "avg_response_seconds_24h": round(row.avg_response_seconds_24h or 0, 1),
        "isa_18_2_target_per_hour": 6,
        "compliant": (row.raised_last_hour or 0) <= 6,
    }
