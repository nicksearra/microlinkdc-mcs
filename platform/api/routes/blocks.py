"""GET /blocks, GET /blocks/{slug}"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, Pagination
from ..schemas import BlockResponse, BlockDetailResponse

router = APIRouter()


@router.get("/blocks", response_model=list[BlockResponse])
async def list_blocks(
    site_slug: Optional[str] = Query(None, description="Filter by site"),
    pagination: Pagination = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """List all blocks, optionally filtered by site."""
    query = """
        SELECT
            b.id, b.slug, b.capacity_mw, b.status, b.commissioned_at,
            s.slug AS site_slug, s.name AS site_name,
            count(DISTINCT sen.id) AS sensor_count
        FROM blocks b
        JOIN sites s ON s.id = b.site_id
        LEFT JOIN equipment e ON e.block_id = b.id
        LEFT JOIN sensors sen ON sen.equipment_id = e.id
    """
    params = {"offset": pagination.offset, "limit": pagination.limit}

    if site_slug:
        query += " WHERE s.slug = :site_slug"
        params["site_slug"] = site_slug

    query += " GROUP BY b.id, s.slug, s.name ORDER BY s.name, b.slug OFFSET :offset LIMIT :limit"

    result = await db.execute(text(query), params)
    return [BlockResponse(
        id=r.id, slug=r.slug, site_slug=r.site_slug, site_name=r.site_name,
        capacity_mw=float(r.capacity_mw), status=r.status,
        commissioned_at=r.commissioned_at, sensor_count=r.sensor_count,
    ) for r in result.fetchall()]


@router.get("/blocks/{slug}", response_model=BlockDetailResponse)
async def get_block(slug: str, db: AsyncSession = Depends(get_db)):
    """Get block details by slug."""
    result = await db.execute(text("""
        SELECT
            b.id, b.slug, b.capacity_mw, b.status, b.commissioned_at,
            b.config_json,
            s.slug AS site_slug, s.name AS site_name,
            count(DISTINCT e.id) AS equipment_count,
            count(DISTINCT sen.id) AS sensor_count
        FROM blocks b
        JOIN sites s ON s.id = b.site_id
        LEFT JOIN equipment e ON e.block_id = b.id
        LEFT JOIN sensors sen ON sen.equipment_id = e.id
        WHERE b.slug = :slug
        GROUP BY b.id, s.slug, s.name
    """), {"slug": slug})
    row = result.fetchone()
    if not row:
        raise HTTPException(404, f"Block '{slug}' not found")

    return BlockDetailResponse(
        id=row.id, slug=row.slug, site_slug=row.site_slug, site_name=row.site_name,
        capacity_mw=float(row.capacity_mw), status=row.status,
        commissioned_at=row.commissioned_at, config=row.config_json,
        equipment_count=row.equipment_count, sensor_count=row.sensor_count,
    )


@router.get("/equipment/{block_slug}")
async def list_equipment(block_slug: str, db: AsyncSession = Depends(get_db)):
    """List all equipment in a block with sensor counts."""
    result = await db.execute(text("""
        SELECT e.id, e.tag, e.type, e.subsystem, e.metadata_json,
               b.slug AS block_slug,
               count(s.id) AS sensor_count
        FROM equipment e
        JOIN blocks b ON b.id = e.block_id
        LEFT JOIN sensors s ON s.equipment_id = e.id
        WHERE b.slug = :slug
        GROUP BY e.id, b.slug
        ORDER BY e.subsystem, e.tag
    """), {"slug": block_slug})

    rows = result.fetchall()
    if not rows:
        raise HTTPException(404, f"No equipment found for block '{block_slug}'")

    return [dict(
        id=r.id, tag=r.tag, type=r.type, subsystem=r.subsystem,
        block_slug=r.block_slug, metadata=r.metadata_json,
        sensor_count=r.sensor_count,
    ) for r in rows]


@router.get("/sensors/{block_slug}")
async def list_sensors(
    block_slug: str,
    subsystem: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all sensors in a block, optionally filtered by subsystem."""
    query = """
        SELECT s.id, s.tag, s.description, s.unit,
               e.subsystem, e.tag AS equipment_tag, b.slug AS block_slug,
               s.range_min, s.range_max, s.poll_rate_ms,
               s.alarm_thresholds_json
        FROM sensors s
        JOIN equipment e ON e.id = s.equipment_id
        JOIN blocks b ON b.id = e.block_id
        WHERE b.slug = :slug
    """
    params = {"slug": block_slug}

    if subsystem:
        query += " AND e.subsystem = :subsystem"
        params["subsystem"] = subsystem

    query += " ORDER BY e.subsystem, s.tag"

    result = await db.execute(text(query), params)
    return [dict(
        id=r.id, tag=r.tag, description=r.description, unit=r.unit,
        subsystem=r.subsystem, equipment_tag=r.equipment_tag,
        block_slug=r.block_slug, range_min=r.range_min,
        range_max=r.range_max, poll_rate_ms=r.poll_rate_ms,
        alarm_thresholds=r.alarm_thresholds_json,
    ) for r in result.fetchall()]
