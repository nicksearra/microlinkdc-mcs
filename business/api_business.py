"""
MCS Stream C — Task 10: Business API Endpoints
================================================
FastAPI router that exposes all business data for Stream D to display.

Endpoints:
  Billing:
    GET /billing/current?customer_id=X         — current month estimate (live)
    GET /billing/invoices?customer_id=X        — invoice history
    GET /billing/invoices/{id}                 — invoice detail with line items
    GET /billing/invoices/{id}/pdf             — download PDF

  SLA:
    GET /sla/report?customer_id=X&month=2026-01 — SLA report for period
    GET /sla/current?customer_id=X              — current month tracking (live)

  ESG:
    GET /esg/metrics?site_id=X&period=month     — ESG metrics
    GET /esg/report?site_id=X&year=2026         — annual ESG summary

  Capacity:
    GET /capacity?site_id=X                     — current capacity status

  Contracts:
    GET /contracts?customer_id=X                — active contracts

  Host:
    GET /host/dashboard?site_id=X               — host-specific metrics

  Lender:
    GET /lender/summary                         — portfolio overview
    GET /lender/report/{month}/pdf              — download lender report PDF

Auth: JWT from Stream B's auth system, role-based access control.

Usage:
    from api_business import create_app
    app = create_app(session_factory, api_client)
    # uvicorn api_business:app --port 8002
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import io


# ─────────────────────────────────────────────
# Pydantic response models
# ─────────────────────────────────────────────

# ── Auth ──

class UserRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    CUSTOMER = "customer"
    HOST = "host"
    LENDER = "lender"


class AuthUser(BaseModel):
    user_id: str
    role: UserRole
    customer_id: Optional[int] = None
    site_ids: List[str] = []


# ── Billing ──

class LineItemResponse(BaseModel):
    line_type: str
    description: str
    quantity: str
    unit: str
    unit_price: str
    amount: str

class BillingEstimateResponse(BaseModel):
    customer_id: int
    customer_name: str
    period: str
    status: str = "estimate"
    line_items: List[LineItemResponse]
    subtotal: str
    tax_rate: str
    tax_amount: str
    total: str
    total_kwh: str
    peak_demand_kw: str
    pue: str
    currency: str = "USD"
    as_of: str

class InvoiceSummaryResponse(BaseModel):
    id: int
    invoice_number: str
    period_start: str
    period_end: str
    status: str
    subtotal: str
    tax: str
    total: str
    currency: str
    due_date: str
    issued_at: Optional[str] = None

class InvoiceDetailResponse(InvoiceSummaryResponse):
    customer_name: str
    contract_ref: str
    line_items: List[LineItemResponse]
    usage_summary: Dict[str, str]
    payment_terms_days: int


# ── SLA ──

class IncidentResponse(BaseModel):
    alarm_id: str
    priority: str
    description: str
    started_at: str
    duration_minutes: str
    root_cause: str
    excluded: bool = False

class SLAReportResponse(BaseModel):
    customer_id: int
    period: str
    availability_class: str
    target_pct: str
    actual_pct: str
    sla_met: bool
    unplanned_downtime_minutes: str
    credit_amount: str
    credit_tier_pct: str
    incidents: List[IncidentResponse]
    summary: str

class SLACurrentResponse(BaseModel):
    customer_id: int
    period: str
    availability_class: str
    target_pct: str
    current_pct: str
    sla_met: bool
    downtime_budget_minutes: str
    downtime_used_minutes: str
    downtime_remaining_minutes: str
    budget_consumed_pct: str
    active_incidents: int
    warning: bool = False
    warning_message: str = ""


# ── ESG ──

class EmissionsResponse(BaseModel):
    scope_1_kg: str
    scope_2_kg: str
    total_kg: str
    diesel_litres: str
    generator_hours: str

class OffsetResponse(BaseModel):
    kwht_exported: str
    offset_kg: str
    grid_emission_factor: str

class EfficiencyResponse(BaseModel):
    pue: str
    wue: str
    it_kwh: str
    total_facility_kwh: str
    heat_recovery_pct: str

class ESGMetricsResponse(BaseModel):
    site_id: str
    period: str
    emissions: EmissionsResponse
    offset: OffsetResponse
    efficiency: EfficiencyResponse
    net_carbon_kg: str
    carbon_negative: bool
    renewable_pct: str

class ESGAnnualResponse(BaseModel):
    site_id: str
    year: int
    emissions: EmissionsResponse
    offset: OffsetResponse
    efficiency: EfficiencyResponse
    net_carbon_kg: str
    carbon_negative: bool
    trend: List[Dict[str, str]]
    methodology: Dict[str, str]


# ── Capacity ──

class CapacityResponse(BaseModel):
    site_id: str
    site_name: str
    total_capacity_kw: str
    sold_kw: str
    available_kw: str
    utilisation_pct: str
    total_racks: int
    available_racks: int
    pue_current: str
    heat_export_pct: str
    rate_per_kw_month: str
    last_updated: str


# ── Contracts ──

class RackAssignmentResponse(BaseModel):
    block_id: str
    rack_ids: List[str]
    committed_kw: str
    availability_class: str

class ContractResponse(BaseModel):
    id: int
    contract_ref: str
    contract_type: str
    site_id: str
    status: str
    start_date: str
    end_date: Optional[str] = None
    payment_terms_days: int
    rack_assignments: List[RackAssignmentResponse]


# ── Host ──

class HostDashboardResponse(BaseModel):
    site_id: str
    site_name: str
    period: str
    heat_exported_kwht: str
    heat_credit_amount: str
    heat_pricing_model: str
    avg_thermal_kw: str
    avg_delta_t_k: str
    co2_offset_kg: str
    budget_neutral: bool
    host_revenue_share: str
    export_hours: str


# ── Lender ──

class LenderSiteSummary(BaseModel):
    site_id: str
    site_name: str
    revenue: str
    availability_pct: str
    utilisation_pct: str
    pue: str
    heat_export_kwht: str

class LenderPortfolioResponse(BaseModel):
    total_sites: int
    total_capacity_mw: str
    fleet_utilisation_pct: str
    total_revenue: str
    ebitda_proxy: str
    ebitda_margin_pct: str
    sla_compliance_pct: str
    net_carbon_kg: str
    sites: List[LenderSiteSummary]
    risk_flags: List[Dict[str, str]]
    as_of: str


# ── Common ──

class ErrorResponse(BaseModel):
    detail: str
    code: str = "error"


# ─────────────────────────────────────────────
# Auth dependency (placeholder)
# ─────────────────────────────────────────────

async def get_current_user() -> AuthUser:
    """
    Placeholder: in production, decode JWT from Authorization header,
    validate against Stream B's auth service, and return user.
    """
    # Default to admin for development
    return AuthUser(user_id="dev", role=UserRole.ADMIN)


def require_role(*roles: UserRole):
    """Dependency that checks user has one of the required roles."""
    async def check(user: AuthUser = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return check


def check_customer_access(user: AuthUser, customer_id: int):
    """Customers can only see their own data."""
    if user.role == UserRole.CUSTOMER and user.customer_id != customer_id:
        raise HTTPException(status_code=403, detail="Access denied to this customer's data")


def check_site_access(user: AuthUser, site_id: str):
    """Hosts can only see their own sites."""
    if user.role == UserRole.HOST and site_id not in user.site_ids:
        raise HTTPException(status_code=403, detail="Access denied to this site")


# ─────────────────────────────────────────────
# Stub data service
# ─────────────────────────────────────────────

class BusinessDataService:
    """
    Service layer that wraps all business logic modules.
    In production, this calls the actual calculators (Tasks 2-9).
    For now, returns stub data matching pilot site outputs.
    """

    def __init__(self, session=None, api_client=None):
        self.session = session
        self.api = api_client

    # ── Billing ──

    async def get_billing_estimate(self, customer_id: int) -> BillingEstimateResponse:
        """Live calculation for current month."""
        # In production: KWhCalculator.calculate(customer_id, now.year, now.month)
        return BillingEstimateResponse(
            customer_id=customer_id,
            customer_name="TensorFlow Cloud Services",
            period="2026-01",
            line_items=[
                LineItemResponse(line_type="colo", description="Colocation — 420 kW committed",
                                 quantity="420.00", unit="kW", unit_price="170.0000", amount="71400.00"),
                LineItemResponse(line_type="power", description="Metered IT power — 48,319 kWh",
                                 quantity="48318.56", unit="kWh", unit_price="0.0850", amount="4107.08"),
                LineItemResponse(line_type="demand", description="Peak demand charge — 71.5 kW",
                                 quantity="71.48", unit="kW", unit_price="12.5000", amount="893.50"),
                LineItemResponse(line_type="cooling", description="Cooling overhead — PUE 1.150",
                                 quantity="7247.78", unit="kWh", unit_price="0.0850", amount="616.06"),
            ],
            subtotal="77016.64", tax_rate="0.08", tax_amount="6161.33",
            total="83177.97", total_kwh="48319", peak_demand_kw="71.5", pue="1.150",
            as_of=datetime.now(timezone.utc).isoformat(),
        )

    async def get_invoices(self, customer_id: int) -> List[InvoiceSummaryResponse]:
        # In production: query Invoice table
        return [InvoiceSummaryResponse(
            id=1, invoice_number="ML-BALD-202601-001",
            period_start="2026-01-01", period_end="2026-01-31",
            status="sent", subtotal="74146.64", tax="5931.73", total="80078.37",
            currency="USD", due_date="2026-03-02", issued_at="2026-02-01",
        )]

    async def get_invoice_detail(self, invoice_id: int) -> InvoiceDetailResponse:
        return InvoiceDetailResponse(
            id=invoice_id, invoice_number="ML-BALD-202601-001",
            period_start="2026-01-01", period_end="2026-01-31",
            status="sent", subtotal="74146.64", tax="5931.73", total="80078.37",
            currency="USD", due_date="2026-03-02", issued_at="2026-02-01",
            customer_name="TensorFlow Cloud Services",
            contract_ref="ML-BALD-2026-001",
            line_items=[
                LineItemResponse(line_type="colo", description="Colocation — 420 kW committed",
                                 quantity="420.00", unit="kW", unit_price="170.0000", amount="71400.00"),
                LineItemResponse(line_type="power", description="Metered IT power",
                                 quantity="48318.56", unit="kWh", unit_price="0.0850", amount="4107.08"),
                LineItemResponse(line_type="demand", description="Peak demand charge",
                                 quantity="71.48", unit="kW", unit_price="12.5000", amount="893.50"),
                LineItemResponse(line_type="cooling", description="Cooling overhead",
                                 quantity="7247.78", unit="kWh", unit_price="0.0850", amount="616.06"),
                LineItemResponse(line_type="cross_connect", description="Cross-connects (2x)",
                                 quantity="2.00", unit="ea", unit_price="350.0000", amount="700.00"),
                LineItemResponse(line_type="sla_credit", description="SLA credit — 99.76% availability",
                                 quantity="1.00", unit="ea", unit_price="-3570.0000", amount="-3570.00"),
            ],
            usage_summary={"total_kwh": "48319", "peak_kw": "71.5", "avg_pue": "1.150",
                           "availability_pct": "99.76"},
            payment_terms_days=30,
        )

    async def get_invoice_pdf(self, invoice_id: int) -> bytes:
        # In production: InvoiceGenerator.render_pdf(invoice)
        # Return a minimal placeholder
        return b"%PDF-1.4\n%placeholder\n"

    # ── SLA ──

    async def get_sla_report(self, customer_id: int, month: str) -> SLAReportResponse:
        return SLAReportResponse(
            customer_id=customer_id, period=month,
            availability_class="B", target_pct="99.90", actual_pct="99.7625",
            sla_met=False, unplanned_downtime_minutes="101",
            credit_amount="3570.00", credit_tier_pct="5",
            incidents=[
                IncidentResponse(alarm_id="ALM-001", priority="P0",
                                 description="UPS on battery — utility failure",
                                 started_at="2026-01-08T03:12:00Z",
                                 duration_minutes="50", root_cause="Utility outage"),
                IncidentResponse(alarm_id="ALM-002", priority="P1",
                                 description="CDU1 inlet temp high",
                                 started_at="2026-01-15T14:30:00Z",
                                 duration_minutes="34", root_cause="Flow restriction"),
                IncidentResponse(alarm_id="ALM-003", priority="P1",
                                 description="Fan tray 03 speed degraded",
                                 started_at="2026-01-22T09:45:00Z",
                                 duration_minutes="17", root_cause="Bearing wear"),
            ],
            summary="Availability: 99.7625% (target: 99.90%, class B) — BREACHED",
        )

    async def get_sla_current(self, customer_id: int) -> SLACurrentResponse:
        return SLACurrentResponse(
            customer_id=customer_id, period="2026-02",
            availability_class="B", target_pct="99.90",
            current_pct="99.98", sla_met=True,
            downtime_budget_minutes="40.3",
            downtime_used_minutes="8.0",
            downtime_remaining_minutes="32.3",
            budget_consumed_pct="19.9",
            active_incidents=0,
            warning=False,
        )

    # ── ESG ──

    async def get_esg_metrics(self, site_id: str, period: str) -> ESGMetricsResponse:
        return ESGMetricsResponse(
            site_id=site_id, period=period,
            emissions=EmissionsResponse(scope_1_kg="1340", scope_2_kg="196370",
                                        total_kg="197710", diesel_litres="500",
                                        generator_hours="2.0"),
            offset=OffsetResponse(kwht_exported="109006", offset_kg="4002",
                                  grid_emission_factor="0.24"),
            efficiency=EfficiencyResponse(pue="1.128", wue="0.0000",
                                          it_kwh="725330", total_facility_kwh="818210",
                                          heat_recovery_pct="100.0"),
            net_carbon_kg="193708", carbon_negative=False, renewable_pct="0",
        )

    async def get_esg_annual(self, site_id: str, year: int) -> ESGAnnualResponse:
        return ESGAnnualResponse(
            site_id=site_id, year=year,
            emissions=EmissionsResponse(scope_1_kg="16080", scope_2_kg="2356444",
                                        total_kg="2372524", diesel_litres="6000",
                                        generator_hours="24.0"),
            offset=OffsetResponse(kwht_exported="1308072", offset_kg="48020",
                                  grid_emission_factor="0.24"),
            efficiency=EfficiencyResponse(pue="1.125", wue="0.0012",
                                          it_kwh="8703960", total_facility_kwh="9792000",
                                          heat_recovery_pct="95.2"),
            net_carbon_kg="2324504", carbon_negative=False,
            trend=[{"month": f"2026-{m:02d}", "pue": "1.13", "offset_kg": "4002"} for m in range(1, 13)],
            methodology={"pue": "PUE = facility_kWh / IT_kWh", "scope_2": "Scope 2 = kWh x grid_factor"},
        )

    # ── Capacity ──

    async def get_capacity(self, site_id: str) -> CapacityResponse:
        return CapacityResponse(
            site_id=site_id, site_name="Baldwinsville Brewery — Block 01",
            total_capacity_kw="1000", sold_kw="580", available_kw="420",
            utilisation_pct="58.0", total_racks=14, available_racks=6,
            pue_current="1.128", heat_export_pct="78.5",
            rate_per_kw_month="170.00",
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    # ── Contracts ──

    async def get_contracts(self, customer_id: int) -> List[ContractResponse]:
        return [ContractResponse(
            id=1, contract_ref="ML-BALD-2026-001", contract_type="colo_msa",
            site_id="BALD-01", status="active", start_date="2026-01-01",
            payment_terms_days=30,
            rack_assignments=[RackAssignmentResponse(
                block_id="BALD-BLK-01",
                rack_ids=["R01", "R02", "R03", "R04", "R05", "R06"],
                committed_kw="420", availability_class="B",
            )],
        )]

    # ── Host ──

    async def get_host_dashboard(self, site_id: str) -> HostDashboardResponse:
        return HostDashboardResponse(
            site_id=site_id, site_name="AB InBev Baldwinsville Brewery",
            period="2026-01",
            heat_exported_kwht="109006", heat_credit_amount="6023.00",
            heat_pricing_model="credit", avg_thermal_kw="150.0",
            avg_delta_t_k="13.5", co2_offset_kg="3924",
            budget_neutral=True, host_revenue_share="0.00",
            export_hours="726.5",
        )

    # ── Lender ──

    async def get_lender_summary(self) -> LenderPortfolioResponse:
        return LenderPortfolioResponse(
            total_sites=1, total_capacity_mw="1.0",
            fleet_utilisation_pct="58.0", total_revenue="77008",
            ebitda_proxy="34654", ebitda_margin_pct="45.0",
            sla_compliance_pct="0", net_carbon_kg="193708",
            sites=[LenderSiteSummary(
                site_id="BALD-01", site_name="Baldwinsville Brewery, NY",
                revenue="77008", availability_pct="99.76",
                utilisation_pct="58.0", pue="1.128",
                heat_export_kwht="109006",
            )],
            risk_flags=[
                {"severity": "high", "category": "sla_breach",
                 "description": "SLA breached at BALD-01: 99.76% vs 99.90%"},
            ],
            as_of=datetime.now(timezone.utc).isoformat(),
        )

    async def get_lender_report_pdf(self, month: str) -> bytes:
        # In production: LenderReportGenerator.generate_monthly(month) + render_pdf()
        return b"%PDF-1.4\n%placeholder-lender-report\n"


# ─────────────────────────────────────────────
# Router factory
# ─────────────────────────────────────────────

def create_business_router(service: BusinessDataService) -> APIRouter:
    """Create the business API router with all endpoints."""

    router = APIRouter(prefix="/api/v1/business", tags=["business"])

    # ── Billing ──

    @router.get("/billing/current", response_model=BillingEstimateResponse)
    async def billing_current(
        customer_id: int = Query(..., description="Customer ID"),
        user: AuthUser = Depends(get_current_user),
    ):
        """Current month billing estimate (live calculation)."""
        check_customer_access(user, customer_id)
        return await service.get_billing_estimate(customer_id)

    @router.get("/billing/invoices", response_model=List[InvoiceSummaryResponse])
    async def billing_invoices(
        customer_id: int = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Invoice history for a customer."""
        check_customer_access(user, customer_id)
        return await service.get_invoices(customer_id)

    @router.get("/billing/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
    async def billing_invoice_detail(
        invoice_id: int,
        user: AuthUser = Depends(get_current_user),
    ):
        """Invoice detail with line items."""
        return await service.get_invoice_detail(invoice_id)

    @router.get("/billing/invoices/{invoice_id}/pdf")
    async def billing_invoice_pdf(
        invoice_id: int,
        user: AuthUser = Depends(get_current_user),
    ):
        """Download invoice PDF."""
        pdf_bytes = await service.get_invoice_pdf(invoice_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="invoice-{invoice_id}.pdf"'},
        )

    # ── SLA ──

    @router.get("/sla/report", response_model=SLAReportResponse)
    async def sla_report(
        customer_id: int = Query(...),
        month: str = Query(..., description="YYYY-MM format"),
        user: AuthUser = Depends(get_current_user),
    ):
        """SLA report for a billing period."""
        check_customer_access(user, customer_id)
        return await service.get_sla_report(customer_id, month)

    @router.get("/sla/current", response_model=SLACurrentResponse)
    async def sla_current(
        customer_id: int = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Current month SLA tracking (live)."""
        check_customer_access(user, customer_id)
        return await service.get_sla_current(customer_id)

    # ── ESG ──

    @router.get("/esg/metrics", response_model=ESGMetricsResponse)
    async def esg_metrics(
        site_id: str = Query(...),
        period: str = Query("month", description="'month' or 'YYYY-MM'"),
        user: AuthUser = Depends(get_current_user),
    ):
        """ESG metrics for a site."""
        check_site_access(user, site_id)
        return await service.get_esg_metrics(site_id, period)

    @router.get("/esg/report", response_model=ESGAnnualResponse)
    async def esg_annual(
        site_id: str = Query(...),
        year: int = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Annual ESG summary with trends and methodology."""
        check_site_access(user, site_id)
        return await service.get_esg_annual(site_id, year)

    # ── Capacity ──

    @router.get("/capacity", response_model=CapacityResponse)
    async def capacity(
        site_id: str = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Current site capacity status."""
        return await service.get_capacity(site_id)

    # ── Contracts ──

    @router.get("/contracts", response_model=List[ContractResponse])
    async def contracts(
        customer_id: int = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Active contracts for a customer."""
        check_customer_access(user, customer_id)
        return await service.get_contracts(customer_id)

    # ── Host ──

    @router.get("/host/dashboard", response_model=HostDashboardResponse)
    async def host_dashboard(
        site_id: str = Query(...),
        user: AuthUser = Depends(get_current_user),
    ):
        """Host-specific dashboard: heat, revenue share, ESG."""
        check_site_access(user, site_id)
        return await service.get_host_dashboard(site_id)

    # ── Lender ──

    @router.get("/lender/summary", response_model=LenderPortfolioResponse)
    async def lender_summary(
        user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.LENDER)),
    ):
        """Portfolio overview for lender portal."""
        return await service.get_lender_summary()

    @router.get("/lender/report/{month}/pdf")
    async def lender_report_pdf(
        month: str,
        user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.LENDER)),
    ):
        """Download lender report PDF for a given month."""
        pdf_bytes = await service.get_lender_report_pdf(month)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="lender-report-{month}.pdf"'},
        )

    return router


# ─────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────

def create_app(session=None, api_client=None) -> FastAPI:
    """Create the FastAPI application with business routes."""
    app = FastAPI(
        title="MicroLink MCS — Business API",
        description="Stream C business endpoints for Stream D dashboards",
        version="1.0.0",
        docs_url="/api/v1/business/docs",
        redoc_url="/api/v1/business/redoc",
    )

    service = BusinessDataService(session, api_client)
    router = create_business_router(service)
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "mcs-business-api", "version": "1.0.0"}

    return app


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

def _run_tests():
    """Test all endpoints using FastAPI's TestClient."""
    from fastapi.testclient import TestClient

    print("=" * 60)
    print("Business API Endpoints — Test Suite")
    print("=" * 60)

    app = create_app()
    client = TestClient(app)

    tests = [
        ("Health", "GET", "/health", None),
        ("Billing estimate", "GET", "/api/v1/business/billing/current?customer_id=1", None),
        ("Invoice list", "GET", "/api/v1/business/billing/invoices?customer_id=1", None),
        ("Invoice detail", "GET", "/api/v1/business/billing/invoices/1", None),
        ("Invoice PDF", "GET", "/api/v1/business/billing/invoices/1/pdf", None),
        ("SLA report", "GET", "/api/v1/business/sla/report?customer_id=1&month=2026-01", None),
        ("SLA current", "GET", "/api/v1/business/sla/current?customer_id=1", None),
        ("ESG metrics", "GET", "/api/v1/business/esg/metrics?site_id=BALD-01&period=month", None),
        ("ESG annual", "GET", "/api/v1/business/esg/report?site_id=BALD-01&year=2026", None),
        ("Capacity", "GET", "/api/v1/business/capacity?site_id=BALD-01", None),
        ("Contracts", "GET", "/api/v1/business/contracts?customer_id=1", None),
        ("Host dashboard", "GET", "/api/v1/business/host/dashboard?site_id=BALD-01", None),
        ("Lender summary", "GET", "/api/v1/business/lender/summary", None),
        ("Lender PDF", "GET", "/api/v1/business/lender/report/2026-01/pdf", None),
    ]

    passed = 0
    failed = 0

    for name, method, path, body in tests:
        try:
            if method == "GET":
                resp = client.get(path)
            else:
                resp = client.post(path, json=body)

            ok = resp.status_code in (200, 201)
            status = "✓" if ok else "✗"

            if ok:
                passed += 1
            else:
                failed += 1

            # Verify response structure
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                data = resp.json()
                detail = f"{len(data)} keys" if isinstance(data, dict) else f"{len(data)} items"
            elif "pdf" in content_type:
                detail = f"{len(resp.content)} bytes PDF"
            else:
                detail = f"{len(resp.content)} bytes"

            print(f"  {status} {name:24s} {resp.status_code} — {detail}")

        except Exception as e:
            failed += 1
            print(f"  ✗ {name:24s} ERROR: {e}")

    print(f"\n  Results: {passed} passed, {failed} failed, {passed + failed} total")

    # Verify key response shapes
    print("\n─── Response shape validation ──────────────────────")

    # Billing estimate
    r = client.get("/api/v1/business/billing/current?customer_id=1").json()
    assert "line_items" in r and len(r["line_items"]) > 0
    assert "subtotal" in r and "total" in r
    print("  ✓ Billing estimate has line_items and totals")

    # SLA report
    r = client.get("/api/v1/business/sla/report?customer_id=1&month=2026-01").json()
    assert "incidents" in r and len(r["incidents"]) == 3
    assert r["sla_met"] is False
    print("  ✓ SLA report has incidents and breach status")

    # ESG metrics
    r = client.get("/api/v1/business/esg/metrics?site_id=BALD-01&period=month").json()
    assert "emissions" in r and "offset" in r and "efficiency" in r
    print("  ✓ ESG metrics has emissions/offset/efficiency")

    # Capacity
    r = client.get("/api/v1/business/capacity?site_id=BALD-01").json()
    assert r["available_kw"] == "420"
    assert r["utilisation_pct"] == "58.0"
    print("  ✓ Capacity shows correct availability")

    # Host dashboard
    r = client.get("/api/v1/business/host/dashboard?site_id=BALD-01").json()
    assert r["heat_exported_kwht"] == "109006"
    print("  ✓ Host dashboard shows heat export")

    # Lender summary
    r = client.get("/api/v1/business/lender/summary").json()
    assert len(r["sites"]) == 1
    assert len(r["risk_flags"]) > 0
    print("  ✓ Lender summary has sites and risk flags")

    # OpenAPI spec
    r = client.get("/openapi.json")
    spec = r.json()
    paths = [p for p in spec["paths"] if "/business/" in p]
    print(f"\n  OpenAPI: {len(paths)} business endpoints documented")

    assert failed == 0, f"{failed} tests failed"
    print("\n✓ All tests passed")


if __name__ == "__main__":
    _run_tests()
