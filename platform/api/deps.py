"""
MCS API — Shared Dependencies

FastAPI dependency injection for DB sessions, pagination, and auth.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Query, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncSession:
    """Yield a database session from the pool."""
    async with request.app.state.db_session() as session:
        yield session


async def get_redis(request: Request):
    """Get the shared Redis connection."""
    return request.app.state.redis


class Pagination:
    """Standard pagination parameters."""
    def __init__(
        self,
        offset: int = Query(0, ge=0, description="Number of records to skip"),
        limit: int = Query(100, ge=1, le=1000, description="Max records to return"),
    ):
        self.offset = offset
        self.limit = limit


class TimeRange:
    """Standard time range parameters for telemetry queries."""
    def __init__(
        self,
        start: datetime = Query(..., description="Start time (ISO 8601)"),
        end: datetime = Query(None, description="End time (ISO 8601, default: now)"),
    ):
        self.start = start
        self.end = end or datetime.now(timezone.utc)

        if self.end <= self.start:
            raise HTTPException(400, "end must be after start")

        # Safety: cap at 1 year
        span = (self.end - self.start).total_seconds()
        if span > 366 * 86400:
            raise HTTPException(400, "Time range cannot exceed 1 year")


class OptionalTimeRange:
    """Optional time range — defaults to last 24 hours."""
    def __init__(
        self,
        start: Optional[datetime] = Query(None, description="Start time (ISO 8601)"),
        end: Optional[datetime] = Query(None, description="End time (ISO 8601)"),
    ):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        self.end = end or now
        self.start = start or (now - timedelta(hours=24))


# ── Auth stub ────────────────────────────────────────────────────────────
# Replace with real auth (JWT/API key) in production.

async def get_current_user(request: Request) -> dict:
    """
    Auth dependency stub.
    In production: validate JWT from Authorization header,
    resolve tenant context, enforce RLS.
    """
    # For now, return a default operator identity
    api_key = request.headers.get("X-API-Key", "")
    return {
        "user_id": "operator-dev",
        "tenant_id": "microlink",
        "roles": ["admin"],
        "api_key": api_key,
    }


async def require_operator(user: dict = Depends(get_current_user)) -> dict:
    """Require at least operator-level access."""
    # Stub — in production, check roles
    return user
