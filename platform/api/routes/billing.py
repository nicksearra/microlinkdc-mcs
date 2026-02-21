"""
Billing data endpoints — consumed by Stream C for invoice generation.

GET /billing/kwh    — 5-minute electrical energy data
GET /billing/kwht   — 5-minute thermal energy data
GET /billing/energy-daily — daily energy summary per block
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, TimeRange
from ..schemas import (
    BillingKwhResponse, BillingKwhRow,
    BillingKwhtResponse, BillingKwhtRow,
    EnergyDailySummaryRow,
)

router = APIRouter()


@router.get("/billing/kwh", response_model=BillingKwhResponse)
async def billing_kwh(
    block_slug: str = Query(...),
    time_range: TimeRange = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    5-minute electrical energy (kWh) data for billing.
    Stream C consumes this to calculate per-customer invoices.
    """
    result = await db.execute(text("""
        SELECT bucket, sensor_id, sensor_tag, equipment_tag,
               block_slug, site_slug, kwh_value, unit,
               sample_count, quality_good_ratio
        FROM billing_kwh_5min
        WHERE block_slug = :block_slug
          AND bucket >= :start
          AND bucket < :end
        ORDER BY bucket, sensor_tag
    """), {"block_slug": block_slug, "start": time_range.start, "end": time_range.end})
    rows = result.fetchall()

    data = [BillingKwhRow(
        bucket=r.bucket, sensor_id=r.sensor_id, sensor_tag=r.sensor_tag,
        equipment_tag=r.equipment_tag, block_slug=r.block_slug,
        site_slug=r.site_slug, kwh_value=r.kwh_value, unit=r.unit,
        sample_count=r.sample_count, quality_good_ratio=r.quality_good_ratio,
    ) for r in rows]

    total_kwh = sum(r.kwh_value or 0 for r in data)

    return BillingKwhResponse(
        block_slug=block_slug,
        start=time_range.start,
        end=time_range.end,
        total_kwh=round(total_kwh, 3),
        row_count=len(data),
        data=data,
    )


@router.get("/billing/kwht", response_model=BillingKwhtResponse)
async def billing_kwht(
    block_slug: str = Query(...),
    time_range: TimeRange = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    5-minute thermal energy (kWht) data for billing.
    Stream C uses this for heat credit calculations.
    """
    result = await db.execute(text("""
        SELECT bucket, sensor_id, sensor_tag, equipment_tag,
               block_slug, site_slug, kwht_value, unit,
               sample_count, quality_good_ratio
        FROM billing_kwht_5min
        WHERE block_slug = :block_slug
          AND bucket >= :start
          AND bucket < :end
        ORDER BY bucket, sensor_tag
    """), {"block_slug": block_slug, "start": time_range.start, "end": time_range.end})
    rows = result.fetchall()

    data = [BillingKwhtRow(
        bucket=r.bucket, sensor_id=r.sensor_id, sensor_tag=r.sensor_tag,
        equipment_tag=r.equipment_tag, block_slug=r.block_slug,
        site_slug=r.site_slug, kwht_value=r.kwht_value, unit=r.unit,
        sample_count=r.sample_count, quality_good_ratio=r.quality_good_ratio,
    ) for r in rows]

    total_kwht = sum(r.kwht_value or 0 for r in data)

    return BillingKwhtResponse(
        block_slug=block_slug,
        start=time_range.start,
        end=time_range.end,
        total_kwht=round(total_kwht, 3),
        row_count=len(data),
        data=data,
    )


@router.get("/billing/energy-daily", response_model=list[EnergyDailySummaryRow])
async def energy_daily_summary(
    block_slug: str = Query(...),
    time_range: TimeRange = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Daily energy summary per block — electrical kWh, thermal recovery,
    PUE estimate. Used by lender reports and ESG module.
    """
    result = await db.execute(text("""
        SELECT day, block_slug, site_slug,
               electrical_kwh, thermal_kwht_recovered,
               thermal_kwht_rejected, heat_recovery_ratio,
               pue_estimate
        FROM energy_daily_summary
        WHERE block_slug = :block_slug
          AND day >= :start
          AND day < :end
        ORDER BY day
    """), {"block_slug": block_slug, "start": time_range.start, "end": time_range.end})

    return [EnergyDailySummaryRow(
        day=r.day, block_slug=r.block_slug, site_slug=r.site_slug,
        electrical_kwh=float(r.electrical_kwh or 0),
        thermal_kwht_recovered=float(r.thermal_kwht_recovered or 0),
        thermal_kwht_rejected=float(r.thermal_kwht_rejected or 0),
        heat_recovery_ratio=float(r.heat_recovery_ratio or 0),
        pue_estimate=float(r.pue_estimate) if r.pue_estimate else None,
    ) for r in result.fetchall()]
