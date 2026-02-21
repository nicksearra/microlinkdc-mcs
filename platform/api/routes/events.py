"""GET /events — immutable event log with filters"""

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, Pagination, OptionalTimeRange
from ..schemas import EventResponse, EventListResponse

router = APIRouter()


@router.get("/events", response_model=EventListResponse)
async def list_events(
    block_slug: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None, description="Filter: alarm_raised, mode_change, etc."),
    time_range: OptionalTimeRange = Depends(),
    pagination: Pagination = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Query the immutable event log. Used by Stream C for audit trails
    and Stream D for event timelines.
    """
    conditions = ["ev.created_at >= :start", "ev.created_at <= :end"]
    params = {
        "start": time_range.start, "end": time_range.end,
        "offset": pagination.offset, "limit": pagination.limit,
    }

    if block_slug:
        conditions.append("b.slug = :block_slug")
        params["block_slug"] = block_slug

    if event_type:
        types = [t.strip() for t in event_type.split(",")]
        conditions.append("ev.event_type = ANY(:types)")
        params["types"] = types

    where = " AND ".join(conditions)

    # Count
    count_result = await db.execute(text(f"""
        SELECT count(*)
        FROM events ev
        LEFT JOIN blocks b ON b.id = ev.block_id
        WHERE {where}
    """), {k: v for k, v in params.items() if k not in ("offset", "limit")})
    total = count_result.scalar_one()

    # Fetch
    result = await db.execute(text(f"""
        SELECT ev.id, ev.block_id, ev.event_type, ev.payload, ev.created_at
        FROM events ev
        LEFT JOIN blocks b ON b.id = ev.block_id
        WHERE {where}
        ORDER BY ev.created_at DESC
        OFFSET :offset LIMIT :limit
    """), params)

    events = [EventResponse(
        id=r.id, block_id=r.block_id, event_type=r.event_type,
        payload=r.payload, created_at=r.created_at,
    ) for r in result.fetchall()]

    return EventListResponse(total=total, events=events)
