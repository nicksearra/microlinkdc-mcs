"""
MCS Stream C — Task 1: Billing Data Model + Contract Management
================================================================
SQLAlchemy ORM models for the MicroLink billing and contract system.

Tables:
  customers, contracts, contract_rack_assignments, rate_schedules,
  invoices, invoice_line_items, host_agreements, billing_meters,
  planned_maintenance, manual_adjustments

Designed for PostgreSQL (TimescaleDB instance shared with Stream B).
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Numeric, Boolean, Date,
    DateTime, Enum, ForeignKey, Index, CheckConstraint, UniqueConstraint,
    JSON, func, and_, or_, select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, Session,
)
from sqlalchemy.sql import expression
import uuid


# ─────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class ContractType(str, enum.Enum):
    COLO_MSA = "colo_msa"
    HOST_AGREEMENT = "host_agreement"
    LENDER = "lender"


class ContractStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


class AvailabilityClass(str, enum.Enum):
    A = "A"   # 99.0 %
    B = "B"   # 99.9 %
    C = "C"   # 99.99 %


class RateType(str, enum.Enum):
    COLO_PER_KW = "colo_per_kw"
    POWER_PER_KWH = "power_per_kwh"
    DEMAND_CHARGE = "demand_charge"
    COOLING_PUE = "cooling_pue"
    HEAT_CREDIT = "heat_credit"
    REVENUE_SHARE = "revenue_share"
    CROSS_CONNECT = "cross_connect"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class LineItemType(str, enum.Enum):
    COLO_FEE = "colo_fee"
    METERED_POWER = "metered_power"
    COOLING_OVERHEAD = "cooling_overhead"
    HEAT_CREDIT = "heat_credit"
    DEMAND_CHARGE = "demand_charge"
    CROSS_CONNECT = "cross_connect"
    SLA_CREDIT = "sla_credit"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    TAX = "tax"


class HeatPricingModel(str, enum.Enum):
    CREDIT = "credit"
    REVENUE_SHARE = "revenue_share"


class MeterType(str, enum.Enum):
    ELECTRICAL = "electrical"
    THERMAL = "thermal"


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class Customer(Base):
    """A colocation tenant purchasing rack space."""
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
        comment="FK to Stream B tenants table (cross-schema reference)",
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    billing_contact_email: Mapped[str] = mapped_column(String(320), nullable=False)
    billing_address: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Structured address: street, city, state, zip, country",
    )
    payment_method: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="stripe / wire / ach",
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    # Relationships
    contracts: Mapped[List[Contract]] = relationship(back_populates="customer", lazy="selectin")
    invoices: Mapped[List[Invoice]] = relationship(back_populates="customer", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Customer id={self.id} name={self.name!r}>"


class Contract(Base):
    """
    Master service agreement, host agreement, or lender agreement.
    Each contract maps to one site and one customer.
    """
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    contract_type: Mapped[ContractType] = mapped_column(Enum(ContractType), nullable=False)
    site_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="FK to Stream B sites table",
    )
    contract_ref: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
        comment="Human-readable reference e.g. ML-BALD-2026-001",
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, server_default=expression.false())
    status: Mapped[ContractStatus] = mapped_column(
        Enum(ContractStatus), server_default=ContractStatus.DRAFT.value,
    )
    terms_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Flexible terms: notice period, payment terms (net-30), etc.",
    )
    payment_terms_days: Mapped[int] = mapped_column(Integer, server_default="30")
    tax_rate_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default="0.0000",
        comment="Applicable tax rate as decimal (e.g. 0.08 = 8%)",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    # Relationships
    customer: Mapped[Customer] = relationship(back_populates="contracts")
    rack_assignments: Mapped[List[ContractRackAssignment]] = relationship(
        back_populates="contract", lazy="selectin",
    )
    rate_schedules: Mapped[List[RateSchedule]] = relationship(
        back_populates="contract", lazy="selectin", order_by="RateSchedule.effective_from.desc()",
    )
    invoices: Mapped[List[Invoice]] = relationship(back_populates="contract")
    host_agreement: Mapped[Optional[HostAgreement]] = relationship(
        back_populates="contract", uselist=False,
    )
    billing_meters: Mapped[List[BillingMeter]] = relationship(back_populates="contract")

    __table_args__ = (
        Index("ix_contracts_customer_status", "customer_id", "status"),
        Index("ix_contracts_site_status", "site_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Contract id={self.id} ref={self.contract_ref!r} type={self.contract_type.value}>"


class ContractRackAssignment(Base):
    """Maps a contract to specific racks within a compute block."""
    __tablename__ = "contract_rack_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    block_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="FK to Stream B blocks table",
    )
    rack_ids: Mapped[list] = mapped_column(
        ARRAY(String(32)), nullable=False, comment="Array of rack identifiers",
    )
    committed_kw: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False,
        comment="Total committed power for this rack group in kW",
    )
    availability_class: Mapped[AvailabilityClass] = mapped_column(
        Enum(AvailabilityClass), nullable=False,
    )

    # Relationships
    contract: Mapped[Contract] = relationship(back_populates="rack_assignments")

    __table_args__ = (
        Index("ix_rack_assignments_block", "block_id"),
    )


class RateSchedule(Base):
    """
    Rate for a specific charge type, effective from a given date.
    Multiple schedules per contract allow rate changes over time — the
    active rate for a billing period is the one with the latest
    effective_from ≤ period_end.
    """
    __tablename__ = "rate_schedules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    rate_type: Mapped[RateType] = mapped_column(Enum(RateType), nullable=False)
    rate_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False,
        comment="Rate value in contract currency (interpretation depends on rate_type)",
    )
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    contract: Mapped[Contract] = relationship(back_populates="rate_schedules")

    __table_args__ = (
        UniqueConstraint("contract_id", "effective_from", "rate_type", name="uq_rate_schedule"),
        Index("ix_rate_schedules_lookup", "contract_id", "rate_type", "effective_from"),
    )


class Invoice(Base):
    """Generated invoice for a billing period."""
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
        comment="Format: ML-{SITE}-{YYYYMM}-{SEQ}",
    )
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), server_default=InvoiceStatus.DRAFT.value,
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0.00")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Usage summary stored for quick reference (also in line items)
    usage_summary: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Snapshot: total_kwh, peak_kw, avg_pue, availability_pct",
    )

    # Relationships
    customer: Mapped[Customer] = relationship(back_populates="invoices")
    contract: Mapped[Contract] = relationship(back_populates="invoices")
    line_items: Mapped[List[InvoiceLineItem]] = relationship(
        back_populates="invoice", lazy="selectin",
        order_by="InvoiceLineItem.sort_order",
    )

    __table_args__ = (
        Index("ix_invoices_customer_period", "customer_id", "period_start"),
        Index("ix_invoices_status", "status"),
        CheckConstraint("period_end > period_start", name="ck_invoices_period"),
    )


class InvoiceLineItem(Base):
    """Individual charge or credit on an invoice."""
    __tablename__ = "invoice_line_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    line_type: Mapped[LineItemType] = mapped_column(Enum(LineItemType), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="kW, kWh, kWht, month, ea, pct",
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False,
        comment="= quantity × unit_price (negative for credits)",
    )
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    metadata_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Extra detail: meter readings count, quality flags, etc.",
    )

    # Relationships
    invoice: Mapped[Invoice] = relationship(back_populates="line_items")

    __table_args__ = (
        Index("ix_line_items_invoice", "invoice_id"),
    )


class HostAgreement(Base):
    """
    Host-specific terms — one per host contract.
    Governs heat credit or revenue share calculations.
    """
    __tablename__ = "host_agreements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id"), unique=True, nullable=False,
    )
    heat_pricing_model: Mapped[HeatPricingModel] = mapped_column(
        Enum(HeatPricingModel), nullable=False,
    )
    host_energy_rate: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False,
        comment="Host's existing energy cost ($/kWh) for displaced fuel",
    )
    displacement_efficiency: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default="0.800",
        comment="Fraction of heat that displaces purchased energy (0.7–0.9 typical)",
    )
    revenue_share_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default="0.0000",
        comment="Host's share of colo revenue (e.g. 0.05 = 5%)",
    )
    budget_neutral_threshold: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), server_default="0.00",
        comment="Monthly minimum value host must receive (budget-neutral promise)",
    )
    host_existing_efficiency: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), server_default="0.850",
        comment="Efficiency of host's existing heating system (gas boiler ~0.85)",
    )
    grid_emission_factor: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), server_default="0.2400",
        comment="Regional grid CO₂ factor (kg CO₂/kWh) — US-NY default 0.24",
    )

    # Relationships
    contract: Mapped[Contract] = relationship(back_populates="host_agreement")


class BillingMeter(Base):
    """
    Maps a physical sensor (from Stream A's point schedule) to a contract
    for billing purposes. Billing-grade meters require Class 0.5 accuracy.
    """
    __tablename__ = "billing_meters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    sensor_tag: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="References Stream A point schedule tag, e.g. MET-01, MET-T1",
    )
    meter_type: Mapped[MeterType] = mapped_column(Enum(MeterType), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=expression.true())
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    accuracy_class: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="IEC accuracy class, e.g. 0.5",
    )
    last_calibration: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Relationships
    contract: Mapped[Contract] = relationship(back_populates="billing_meters")

    __table_args__ = (
        Index("ix_billing_meters_sensor", "sensor_tag"),
        UniqueConstraint("contract_id", "sensor_tag", name="uq_billing_meter_sensor"),
    )


class PlannedMaintenance(Base):
    """
    Maintenance windows — excluded from SLA downtime calculations
    only if notice was sent ≥ 48 hours before start.
    """
    __tablename__ = "planned_maintenance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    block_id: Mapped[str] = mapped_column(String(64), nullable=False)
    site_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    affected_customers: Mapped[list | None] = mapped_column(
        ARRAY(BigInteger), nullable=True,
        comment="Customer IDs affected (null = all customers on block)",
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_planned_maintenance_block_time", "block_id", "start_at", "end_at"),
        CheckConstraint("end_at > start_at", name="ck_maintenance_window"),
    )

    @property
    def is_valid_exclusion(self) -> bool:
        """Maintenance only excluded from SLA if notice was ≥ 48h before start."""
        if self.notice_sent_at is None:
            return False
        return self.notice_sent_at <= self.start_at - timedelta(hours=48)


class ManualAdjustment(Base):
    """
    One-off credits or charges added to an invoice manually
    (e.g. goodwill credits, setup fees, one-time charges).
    """
    __tablename__ = "manual_adjustments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, comment="Positive = charge, negative = credit",
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    applied_to_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmissionFactor(Base):
    """Regional grid emission factors — configurable per site / region."""
    __tablename__ = "emission_factors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="e.g. US-NY, EU, ZA")
    factor_kg_per_kwh: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    effective_year: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("region_code", "effective_year", name="uq_emission_factor_region_year"),
    )


# ─────────────────────────────────────────────
# Helper functions (service layer)
# ─────────────────────────────────────────────

def get_active_contract(
    session: Session, customer_id: int, contract_type: ContractType | None = None,
) -> Contract | None:
    """Return the active contract for a customer, optionally filtered by type."""
    q = (
        select(Contract)
        .where(
            Contract.customer_id == customer_id,
            Contract.status == ContractStatus.ACTIVE,
        )
    )
    if contract_type:
        q = q.where(Contract.contract_type == contract_type)
    return session.execute(q).scalars().first()


def get_active_contracts_for_site(session: Session, site_id: str) -> List[Contract]:
    """Return all active contracts at a given site."""
    q = (
        select(Contract)
        .where(Contract.site_id == site_id, Contract.status == ContractStatus.ACTIVE)
    )
    return list(session.execute(q).scalars().all())


def get_rate(
    session: Session,
    contract_id: int,
    rate_type: RateType,
    as_of: date | None = None,
) -> RateSchedule | None:
    """
    Get the effective rate for a contract and rate type.
    Returns the schedule with the latest effective_from ≤ as_of.
    """
    if as_of is None:
        as_of = date.today()

    q = (
        select(RateSchedule)
        .where(
            RateSchedule.contract_id == contract_id,
            RateSchedule.rate_type == rate_type,
            RateSchedule.effective_from <= as_of,
        )
        .order_by(RateSchedule.effective_from.desc())
        .limit(1)
    )
    return session.execute(q).scalars().first()


def get_customer_rack_assignments(
    session: Session, customer_id: int, block_id: str | None = None,
) -> List[ContractRackAssignment]:
    """Get rack assignments for a customer's active contracts."""
    q = (
        select(ContractRackAssignment)
        .join(Contract)
        .where(
            Contract.customer_id == customer_id,
            Contract.status == ContractStatus.ACTIVE,
        )
    )
    if block_id:
        q = q.where(ContractRackAssignment.block_id == block_id)
    return list(session.execute(q).scalars().all())


def get_host_agreement_for_site(session: Session, site_id: str) -> HostAgreement | None:
    """Get the host agreement for a site (via the host_agreement contract)."""
    q = (
        select(HostAgreement)
        .join(Contract)
        .where(
            Contract.site_id == site_id,
            Contract.contract_type == ContractType.HOST_AGREEMENT,
            Contract.status == ContractStatus.ACTIVE,
        )
    )
    return session.execute(q).scalars().first()


def get_planned_maintenance_windows(
    session: Session,
    block_id: str,
    period_start: datetime,
    period_end: datetime,
    valid_only: bool = True,
) -> List[PlannedMaintenance]:
    """Get maintenance windows overlapping a period for SLA exclusion."""
    q = (
        select(PlannedMaintenance)
        .where(
            PlannedMaintenance.block_id == block_id,
            PlannedMaintenance.start_at < period_end,
            PlannedMaintenance.end_at > period_start,
        )
        .order_by(PlannedMaintenance.start_at)
    )
    results = list(session.execute(q).scalars().all())
    if valid_only:
        results = [m for m in results if m.is_valid_exclusion]
    return results


def get_manual_adjustments(
    session: Session,
    customer_id: int,
    period_start: date,
    period_end: date,
) -> List[ManualAdjustment]:
    """Get unapplied manual adjustments for a customer billing period."""
    q = (
        select(ManualAdjustment)
        .where(
            ManualAdjustment.customer_id == customer_id,
            ManualAdjustment.period_start >= period_start,
            ManualAdjustment.period_end <= period_end,
            ManualAdjustment.applied_to_invoice_id.is_(None),
        )
    )
    return list(session.execute(q).scalars().all())


def generate_invoice_number(session: Session, site_code: str, year: int, month: int) -> str:
    """
    Generate next invoice number: ML-{SITE}-{YYYYMM}-{SEQ}
    Example: ML-BALD-202601-001
    """
    prefix = f"ML-{site_code}-{year}{month:02d}-"
    q = (
        select(func.count(Invoice.id))
        .where(Invoice.invoice_number.like(f"{prefix}%"))
    )
    count = session.execute(q).scalar() or 0
    return f"{prefix}{count + 1:03d}"


def get_total_committed_kw(session: Session, site_id: str) -> Decimal:
    """Sum of all committed kW across active contracts at a site."""
    q = (
        select(func.coalesce(func.sum(ContractRackAssignment.committed_kw), 0))
        .join(Contract)
        .where(Contract.site_id == site_id, Contract.status == ContractStatus.ACTIVE)
    )
    return session.execute(q).scalar()


def get_available_capacity_kw(session: Session, site_id: str, total_capacity_kw: Decimal) -> Decimal:
    """Available = total site capacity minus committed."""
    sold = get_total_committed_kw(session, site_id)
    return total_capacity_kw - sold


# ─────────────────────────────────────────────
# SLA availability targets (lookup)
# ─────────────────────────────────────────────

SLA_TARGETS = {
    AvailabilityClass.A: Decimal("99.00"),
    AvailabilityClass.B: Decimal("99.90"),
    AvailabilityClass.C: Decimal("99.99"),
}

SLA_CREDIT_TIERS = [
    # (delta_below_target, credit_pct)  — applied from top, first match wins
    (Decimal("2.0"), Decimal("0.50")),  # < target - 2.0% → 50%
    (Decimal("1.0"), Decimal("0.25")),  # < target - 1.0% → 25%
    (Decimal("0.5"), Decimal("0.10")),  # < target - 0.5% → 10%
    (Decimal("0.0"), Decimal("0.05")),  # < target        →  5%
]


def get_sla_credit_pct(availability_class: AvailabilityClass, actual_pct: Decimal) -> Decimal:
    """Determine SLA credit percentage based on availability vs target."""
    target = SLA_TARGETS[availability_class]
    if actual_pct >= target:
        return Decimal("0.00")
    for delta, credit_pct in SLA_CREDIT_TIERS:
        if actual_pct < target - delta:
            return credit_pct
    return Decimal("0.05")  # Just below target
