"""GET /sites, GET /sites/{slug}"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, Pagination
from ..schemas import SiteResponse, SiteDetailResponse

router = APIRouter()


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(
    pagination: Pagination = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """List all sites with block count and capacity summary."""
    result = await db.execute(text("""
        SELECT
            s.id, s.slug, s.name, s.region, s.status,
            s.latitude, s.longitude,
            count(b.id) AS block_count,
            coalesce(sum(b.capacity_mw), 0) AS total_capacity_mw
        FROM sites s
        LEFT JOIN blocks b ON b.site_id = s.id
        GROUP BY s.id
        ORDER BY s.name
        OFFSET :offset LIMIT :limit
    """), {"offset": pagination.offset, "limit": pagination.limit})

    return [SiteResponse(
        id=r.id, slug=r.slug, name=r.name, region=r.region,
        status=r.status, latitude=r.latitude, longitude=r.longitude,
        block_count=r.block_count, total_capacity_mw=float(r.total_capacity_mw),
    ) for r in result.fetchall()]


@router.get("/sites/{slug}", response_model=SiteDetailResponse)
async def get_site(slug: str, db: AsyncSession = Depends(get_db)):
    """Get site details by slug."""
    result = await db.execute(text("""
        SELECT
            s.id, s.slug, s.name, s.region, s.status,
            s.latitude, s.longitude, s.config_json, s.created_at,
            count(b.id) AS block_count,
            coalesce(sum(b.capacity_mw), 0) AS total_capacity_mw
        FROM sites s
        LEFT JOIN blocks b ON b.site_id = s.id
        WHERE s.slug = :slug
        GROUP BY s.id
    """), {"slug": slug})
    row = result.fetchone()
    if not row:
        raise HTTPException(404, f"Site '{slug}' not found")

    return SiteDetailResponse(
        id=row.id, slug=row.slug, name=row.name, region=row.region,
        status=row.status, latitude=row.latitude, longitude=row.longitude,
        config=row.config_json, created_at=row.created_at,
        block_count=row.block_count, total_capacity_mw=float(row.total_capacity_mw),
    )
