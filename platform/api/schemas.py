"""
MCS API — Response Schemas

Pydantic models for all API responses. These define the contract
that Stream C and Stream D develop against.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── Sites ────────────────────────────────────────────────────────────────

class SiteResponse(BaseModel):
    id: int
    slug: str
    name: str
    region: Optional[str] = None
    status: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    block_count: int = 0
    total_capacity_mw: float = 0.0

    model_config = {"from_attributes": True}


class SiteDetailResponse(SiteResponse):
    config: Optional[dict] = None
    created_at: Optional[datetime] = None


# ── Blocks ───────────────────────────────────────────────────────────────

class BlockResponse(BaseModel):
    id: int
    slug: str
    site_slug: str
    site_name: str
    capacity_mw: float
    status: str
    commissioned_at: Optional[datetime] = None
    sensor_count: int = 0

    model_config = {"from_attributes": True}


class BlockDetailResponse(BlockResponse):
    config: Optional[dict] = None
    equipment_count: int = 0


# ── Equipment ────────────────────────────────────────────────────────────

class EquipmentResponse(BaseModel):
    id: int
    tag: str
    type: str
    subsystem: str
    block_slug: str
    metadata: Optional[dict] = None
    sensor_count: int = 0

    model_config = {"from_attributes": True}


# ── Sensors ──────────────────────────────────────────────────────────────

class SensorResponse(BaseModel):
    id: int
    tag: str
    description: Optional[str] = None
    unit: Optional[str] = None
    subsystem: str
    equipment_tag: str
    block_slug: str
    range_min: Optional[float] = None
    range_max: Optional[float] = None
    poll_rate_ms: Optional[int] = None
    alarm_thresholds: Optional[dict] = None

    model_config = {"from_attributes": True}


# ── Telemetry ────────────────────────────────────────────────────────────

class TelemetryPoint(BaseModel):
    """Single telemetry data point (raw or aggregated)."""
    bucket: datetime
    val_avg: float
    val_min: float
    val_max: float
    val_last: float
    sample_count: int
    quality_ratio: Optional[float] = None


class TelemetryResponse(BaseModel):
    """Telemetry query response with metadata."""
    sensor_id: int
    sensor_tag: str
    unit: Optional[str] = None
    tier: str = Field(description="Aggregate tier used: telemetry, agg_1min, agg_5min, agg_1hour, agg_1day")
    start: datetime
    end: datetime
    point_count: int
    data: list[TelemetryPoint]


class LatestValueResponse(BaseModel):
    """Latest value for a single sensor."""
    sensor_id: int
    tag: str
    subsystem: str
    unit: Optional[str] = None
    value: float
    quality: int
    timestamp: datetime


class BlockLatestResponse(BaseModel):
    """Latest values for all sensors in a block."""
    block_slug: str
    sensor_count: int
    readings: list[LatestValueResponse]


# ── Alarms ───────────────────────────────────────────────────────────────

class AlarmResponse(BaseModel):
    id: Optional[int] = None
    sensor_id: int
    priority: str
    state: str
    tag: str
    subsystem: str
    site_id: str
    block_id: str
    value_at_raise: Optional[float] = None
    value_at_clear: Optional[float] = None
    threshold_value: Optional[float] = None
    threshold_direction: Optional[str] = None
    raised_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    acked_by: Optional[str] = None
    cleared_at: Optional[datetime] = None
    shelved_at: Optional[datetime] = None
    shelved_by: Optional[str] = None
    shelved_until: Optional[datetime] = None
    shelve_reason: Optional[str] = None
    suppressed_by_alarm_id: Optional[int] = None
    response_time_seconds: Optional[float] = None
    last_value: Optional[float] = None
    last_seen: Optional[datetime] = None


class AlarmAckRequest(BaseModel):
    operator: str = Field(min_length=1, description="Operator name or ID")


class AlarmShelveRequest(BaseModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500, description="Reason for shelving (required)")
    duration_hours: float = Field(default=8.0, ge=0.5, le=24.0, description="Shelve duration in hours (max 24)")


class AlarmListResponse(BaseModel):
    total: int
    standing: int
    alarms: list[AlarmResponse]


# ── Events ───────────────────────────────────────────────────────────────

class EventResponse(BaseModel):
    id: int
    block_id: Optional[int] = None
    event_type: str
    payload: Optional[dict] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    total: int
    events: list[EventResponse]


# ── Billing ──────────────────────────────────────────────────────────────

class BillingKwhRow(BaseModel):
    bucket: datetime
    sensor_id: int
    sensor_tag: str
    equipment_tag: str
    block_slug: str
    site_slug: str
    kwh_value: Optional[float] = None
    unit: str
    sample_count: int
    quality_good_ratio: Optional[float] = None


class BillingKwhResponse(BaseModel):
    block_slug: str
    start: datetime
    end: datetime
    total_kwh: float
    row_count: int
    data: list[BillingKwhRow]


class BillingKwhtRow(BaseModel):
    bucket: datetime
    sensor_id: int
    sensor_tag: str
    equipment_tag: str
    block_slug: str
    site_slug: str
    kwht_value: Optional[float] = None
    unit: str
    sample_count: int
    quality_good_ratio: Optional[float] = None


class BillingKwhtResponse(BaseModel):
    block_slug: str
    start: datetime
    end: datetime
    total_kwht: float
    row_count: int
    data: list[BillingKwhtRow]


class EnergyDailySummaryRow(BaseModel):
    day: datetime
    block_slug: str
    site_slug: str
    electrical_kwh: float
    thermal_kwht_recovered: float
    thermal_kwht_rejected: float
    heat_recovery_ratio: float
    pue_estimate: Optional[float] = None


# ── Health ───────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    db_connected: bool
    redis_connected: bool
    uptime_seconds: float


class StatsResponse(BaseModel):
    ingestion: dict
    alarms: dict
    cache: dict
    database: dict
