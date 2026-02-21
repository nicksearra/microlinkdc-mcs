"""
MCS Stream C — Task 9: Lender Report Generator
================================================
Automated report generation for project finance lenders.

Monthly report sections:
  1. Executive summary — fleet overview, revenue, operational highlights
  2. Financial summary — revenue per site, EBITDA proxy, cash position
  3. Operational metrics — availability %, PUE, utilisation %, heat export
  4. SLA compliance — summary across all customers
  5. Incident log — P0/P1 incidents with resolution details
  6. Capacity status — sold vs available, pipeline deals
  7. ESG section — carbon metrics, sustainability highlights
  8. Maintenance summary — completed and upcoming work orders
  9. Risk flags — SLA breaches, capacity issues, equipment concerns

Quarterly extends monthly with:
  - 90-day trending (revenue, availability, PUE, heat export)
  - Capex tracking vs budget
  - Market comparison vs industry benchmarks

Usage:
    generator = LenderReportGenerator(session, api_client)
    report = await generator.generate_monthly("2026-01")
    pdf_bytes = generator.render_pdf(report)
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)

from billing_models import Session


# ─────────────────────────────────────────────
# Brand
# ─────────────────────────────────────────────

BRAND = {
    "company": "MicroLink Data Centers",
    "primary": colors.HexColor("#0091ff"),
    "dark": colors.HexColor("#0d1219"),
    "text": colors.HexColor("#1a1a2e"),
    "muted": colors.HexColor("#5e6d80"),
    "border": colors.HexColor("#d0d7de"),
    "light_bg": colors.HexColor("#f6f8fa"),
    "green": colors.HexColor("#00d68f"),
    "red": colors.HexColor("#ef4444"),
    "amber": colors.HexColor("#f59e0b"),
}


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class SiteMetrics:
    site_id: str
    site_name: str
    # Financial
    revenue_total: Decimal = Decimal("0")
    revenue_colo: Decimal = Decimal("0")
    revenue_power: Decimal = Decimal("0")
    revenue_other: Decimal = Decimal("0")
    # Operational
    availability_pct: Decimal = Decimal("100")
    sla_target_pct: Decimal = Decimal("99.9")
    sla_met: bool = True
    pue: Decimal = Decimal("0")
    utilisation_pct: Decimal = Decimal("0")
    sold_kw: Decimal = Decimal("0")
    total_kw: Decimal = Decimal("0")
    heat_export_kwht: Decimal = Decimal("0")
    heat_export_pct: Decimal = Decimal("0")
    # Incidents
    p0_count: int = 0
    p1_count: int = 0
    total_downtime_min: Decimal = Decimal("0")


@dataclass
class IncidentEntry:
    site_id: str
    alarm_id: str
    priority: str
    description: str
    raised_at: str
    duration_min: Decimal
    root_cause: str
    resolution: str
    sla_impact: bool = False


@dataclass
class RiskFlag:
    site_id: str
    category: str  # sla_breach, capacity, equipment, financial
    severity: str  # high, medium, low
    description: str


@dataclass
class ESGSummary:
    total_scope1_kg: Decimal = Decimal("0")
    total_scope2_kg: Decimal = Decimal("0")
    total_offset_kg: Decimal = Decimal("0")
    net_carbon_kg: Decimal = Decimal("0")
    fleet_pue: Decimal = Decimal("0")
    total_kwht_exported: Decimal = Decimal("0")
    renewable_pct: Decimal = Decimal("0")


@dataclass
class TrendPoint:
    month: str  # "2026-01"
    revenue: Decimal = Decimal("0")
    availability: Decimal = Decimal("0")
    pue: Decimal = Decimal("0")
    heat_export: Decimal = Decimal("0")
    utilisation: Decimal = Decimal("0")


@dataclass
class LenderReport:
    # Header
    report_type: str  # "monthly" or "quarterly"
    period_label: str  # "January 2026" or "Q1 2026"
    period_start: date = date(2026, 1, 1)
    period_end: date = date(2026, 2, 1)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Fleet overview
    total_sites: int = 0
    total_capacity_mw: Decimal = Decimal("0")
    total_sold_mw: Decimal = Decimal("0")
    fleet_utilisation_pct: Decimal = Decimal("0")

    # Financial
    total_revenue: Decimal = Decimal("0")
    ebitda_proxy: Decimal = Decimal("0")
    ebitda_margin_pct: Decimal = Decimal("0")
    cash_position: Decimal = Decimal("0")  # placeholder

    # Per-site metrics
    sites: List[SiteMetrics] = field(default_factory=list)

    # SLA
    sla_customers_total: int = 0
    sla_customers_met: int = 0
    sla_compliance_pct: Decimal = Decimal("100")
    sla_credits_total: Decimal = Decimal("0")

    # Incidents
    incidents: List[IncidentEntry] = field(default_factory=list)

    # Capacity
    pipeline_deals: int = 0
    pipeline_value: Decimal = Decimal("0")

    # ESG
    esg: ESGSummary = field(default_factory=ESGSummary)

    # Maintenance
    maintenance_completed: int = 0
    maintenance_upcoming: int = 0

    # Risk
    risk_flags: List[RiskFlag] = field(default_factory=list)

    # Quarterly extras
    trend: List[TrendPoint] = field(default_factory=list)
    capex_budget: Decimal = Decimal("0")
    capex_actual: Decimal = Decimal("0")
    capex_variance_pct: Decimal = Decimal("0")

    # Config
    lender_name: str = ""
    include_sections: List[str] = field(default_factory=lambda: [
        "executive", "financial", "operational", "sla", "incidents",
        "capacity", "esg", "maintenance", "risk",
    ])


# ─────────────────────────────────────────────
# Report generator
# ─────────────────────────────────────────────

class LenderReportGenerator:
    """
    Assembles data from billing, SLA, ESG, and CRM modules
    into a comprehensive lender report and renders as PDF.
    """

    def __init__(self, session: Session, api_client=None):
        self.session = session
        self.api = api_client

    async def generate_monthly(
        self,
        month_str: str,
        lender_name: str = "",
        include_sections: Optional[List[str]] = None,
    ) -> LenderReport:
        """
        Generate monthly lender report.
        month_str: "2026-01" format
        """
        year, month = int(month_str[:4]), int(month_str[5:7])
        period_start = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        report = LenderReport(
            report_type="monthly",
            period_label=period_start.strftime("%B %Y"),
            period_start=period_start,
            period_end=next_month - timedelta(days=1),
            lender_name=lender_name,
        )
        if include_sections:
            report.include_sections = include_sections

        # In production, each section pulls from the respective module:
        #   - Financial: Invoice totals from billing DB
        #   - Operational: SLA engine, ESG calculator
        #   - Incidents: PagerDuty audit log / SLA engine
        #   - Capacity: CRM feed snapshots
        #   - ESG: ESG calculator
        # For now, populate from stub data
        await self._populate_stub_data(report, year, month)

        return report

    async def generate_quarterly(
        self,
        quarter_str: str,
        lender_name: str = "",
    ) -> LenderReport:
        """
        Generate quarterly deep-dive report.
        quarter_str: "2026-Q1" format
        """
        year = int(quarter_str[:4])
        q = int(quarter_str[-1])
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2

        period_start = date(year, start_month, 1)
        if end_month == 12:
            period_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            period_end = date(year, end_month + 1, 1) - timedelta(days=1)

        report = LenderReport(
            report_type="quarterly",
            period_label=f"Q{q} {year}",
            period_start=period_start,
            period_end=period_end,
            lender_name=lender_name,
            include_sections=[
                "executive", "financial", "operational", "sla", "incidents",
                "capacity", "esg", "maintenance", "risk", "trend", "capex", "benchmark",
            ],
        )

        await self._populate_stub_data(report, year, start_month)

        # Quarterly trending
        report.trend = []
        for i in range(3):
            m = start_month + i
            report.trend.append(TrendPoint(
                month=f"{year}-{m:02d}",
                revenue=Decimal("77000") + Decimal(str(i * 1200)),
                availability=Decimal("99.77") + Decimal(str(i)) * Decimal("0.05"),
                pue=Decimal("1.13") - Decimal(str(i)) * Decimal("0.005"),
                heat_export=Decimal("78") + Decimal(str(i * 2)),
                utilisation=Decimal("58") + Decimal(str(i * 3)),
            ))

        # Capex tracking
        report.capex_budget = Decimal("4000000")
        report.capex_actual = Decimal("3850000")
        report.capex_variance_pct = (
            (report.capex_actual - report.capex_budget) / report.capex_budget * Decimal("100")
        ).quantize(Decimal("0.1"))

        return report

    async def _populate_stub_data(self, report: LenderReport, year: int, month: int):
        """Populate report with realistic stub data for Baldwinsville."""
        site = SiteMetrics(
            site_id="BALD-01",
            site_name="Baldwinsville Brewery, NY",
            revenue_total=Decimal("77008.00"),
            revenue_colo=Decimal("71400.00"),
            revenue_power=Decimal("4107.00"),
            revenue_other=Decimal("1501.00"),
            availability_pct=Decimal("99.76"),
            sla_target_pct=Decimal("99.90"),
            sla_met=False,
            pue=Decimal("1.128"),
            utilisation_pct=Decimal("58.0"),
            sold_kw=Decimal("580"),
            total_kw=Decimal("1000"),
            heat_export_kwht=Decimal("109006"),
            heat_export_pct=Decimal("78.5"),
            p0_count=1,
            p1_count=2,
            total_downtime_min=Decimal("101"),
        )
        report.sites = [site]
        report.total_sites = 1
        report.total_capacity_mw = Decimal("1.0")
        report.total_sold_mw = Decimal("0.58")
        report.fleet_utilisation_pct = Decimal("58.0")

        report.total_revenue = site.revenue_total
        report.ebitda_proxy = (site.revenue_total * Decimal("0.45")).quantize(Decimal("0.01"))
        report.ebitda_margin_pct = Decimal("45.0")
        report.cash_position = Decimal("2450000")

        report.sla_customers_total = 1
        report.sla_customers_met = 0
        report.sla_compliance_pct = Decimal("0")
        report.sla_credits_total = Decimal("3570")

        report.incidents = [
            IncidentEntry("BALD-01", "ALM-001", "P0", "UPS on battery — utility failure",
                          f"{year}-{month:02d}-08T03:12:00Z", Decimal("50"),
                          "Utility outage", "UPS bridged to generator; utility restored", True),
            IncidentEntry("BALD-01", "ALM-002", "P1", "CDU1 inlet temp high",
                          f"{year}-{month:02d}-15T14:30:00Z", Decimal("34"),
                          "Flow restriction", "Valve actuator replaced", True),
            IncidentEntry("BALD-01", "ALM-003", "P1", "Fan tray 03 speed degraded",
                          f"{year}-{month:02d}-22T09:45:00Z", Decimal("17"),
                          "Bearing wear", "Fan tray swapped under maintenance", False),
        ]

        report.pipeline_deals = 3
        report.pipeline_value = Decimal("1200000")

        report.esg = ESGSummary(
            total_scope1_kg=Decimal("1340"),
            total_scope2_kg=Decimal("196370"),
            total_offset_kg=Decimal("4002"),
            net_carbon_kg=Decimal("193708"),
            fleet_pue=Decimal("1.128"),
            total_kwht_exported=Decimal("109006"),
            renewable_pct=Decimal("0"),
        )

        report.maintenance_completed = 4
        report.maintenance_upcoming = 2

        report.risk_flags = [
            RiskFlag("BALD-01", "sla_breach", "high",
                     "SLA breached: 99.76% vs 99.90% target — $3,570 credit applied"),
            RiskFlag("BALD-01", "capacity", "medium",
                     "Site at 58% utilisation — sales pipeline active with 3 deals"),
        ]

    # ─────────────────────────────────────────
    # PDF rendering
    # ─────────────────────────────────────────

    def render_pdf(self, report: LenderReport) -> bytes:
        """Render complete lender report as PDF."""
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            topMargin=0.5 * inch, bottomMargin=0.75 * inch,
            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        )

        styles = self._styles()
        story = []

        # Cover
        story.extend(self._render_cover(report, styles))
        story.append(PageBreak())

        # Sections
        section_renderers = {
            "executive": self._render_executive,
            "financial": self._render_financial,
            "operational": self._render_operational,
            "sla": self._render_sla,
            "incidents": self._render_incidents,
            "capacity": self._render_capacity,
            "esg": self._render_esg,
            "maintenance": self._render_maintenance,
            "risk": self._render_risk,
            "trend": self._render_trend,
            "capex": self._render_capex,
            "benchmark": self._render_benchmark,
        }

        for section in report.include_sections:
            renderer = section_renderers.get(section)
            if renderer:
                elements = renderer(report, styles)
                if elements:
                    story.extend(elements)
                    story.append(Spacer(1, 16))

        # Footer
        story.append(HRFlowable(width="100%", thickness=0.5, color=BRAND["border"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"Confidential — Prepared for {report.lender_name or 'Project Lenders'} | "
            f"Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"{BRAND['company']}",
            styles["footer"],
        ))

        doc.build(story)
        return buf.getvalue()

    def _styles(self) -> Dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        return {
            "title": ParagraphStyle("T", parent=base["Title"], fontSize=24, fontName="Helvetica-Bold",
                                    textColor=BRAND["primary"], spaceAfter=4),
            "subtitle": ParagraphStyle("ST", parent=base["Normal"], fontSize=12, fontName="Helvetica",
                                       textColor=BRAND["muted"], spaceAfter=12),
            "h1": ParagraphStyle("H1", parent=base["Heading1"], fontSize=14, fontName="Helvetica-Bold",
                                 textColor=BRAND["dark"], spaceBefore=16, spaceAfter=8),
            "h2": ParagraphStyle("H2", parent=base["Heading2"], fontSize=11, fontName="Helvetica-Bold",
                                 textColor=BRAND["primary"], spaceBefore=10, spaceAfter=6),
            "body": ParagraphStyle("B", parent=base["Normal"], fontSize=9, fontName="Helvetica",
                                   textColor=BRAND["text"], leading=13),
            "body_bold": ParagraphStyle("BB", parent=base["Normal"], fontSize=9, fontName="Helvetica-Bold",
                                        textColor=BRAND["text"]),
            "body_right": ParagraphStyle("BR", parent=base["Normal"], fontSize=9, fontName="Helvetica",
                                         textColor=BRAND["text"], alignment=TA_RIGHT),
            "metric_label": ParagraphStyle("ML", parent=base["Normal"], fontSize=7.5, fontName="Helvetica-Bold",
                                           textColor=BRAND["muted"], spaceAfter=1),
            "metric_value": ParagraphStyle("MV", parent=base["Normal"], fontSize=14, fontName="Helvetica-Bold",
                                           textColor=BRAND["dark"]),
            "small": ParagraphStyle("SM", parent=base["Normal"], fontSize=8, fontName="Helvetica",
                                    textColor=BRAND["muted"]),
            "footer": ParagraphStyle("FT", parent=base["Normal"], fontSize=7, fontName="Helvetica",
                                     textColor=BRAND["muted"], alignment=TA_CENTER),
            "th": ParagraphStyle("TH", parent=base["Normal"], fontSize=8, fontName="Helvetica-Bold",
                                 textColor=colors.white),
            "td": ParagraphStyle("TD", parent=base["Normal"], fontSize=8.5, fontName="Helvetica",
                                 textColor=BRAND["text"]),
            "td_right": ParagraphStyle("TDR", parent=base["Normal"], fontSize=8.5, fontName="Helvetica",
                                       textColor=BRAND["text"], alignment=TA_RIGHT),
        }

    def _render_cover(self, r: LenderReport, s: dict) -> list:
        return [
            Spacer(1, 2 * inch),
            Paragraph(BRAND["company"], s["title"]),
            Paragraph("Project Finance Lender Report", s["subtitle"]),
            Spacer(1, 0.5 * inch),
            Paragraph(r.period_label, ParagraphStyle("PL", parent=s["title"], fontSize=28)),
            Paragraph(
                f"{'Monthly' if r.report_type == 'monthly' else 'Quarterly'} Report",
                s["subtitle"],
            ),
            Spacer(1, inch),
            Paragraph(f"Prepared for: {r.lender_name or 'Project Lenders'}", s["body"]),
            Paragraph(f"Generated: {r.generated_at.strftime('%B %d, %Y at %H:%M UTC')}", s["body"]),
            Paragraph(f"Period: {r.period_start.strftime('%B %d, %Y')} — {r.period_end.strftime('%B %d, %Y')}", s["body"]),
            Spacer(1, 0.5 * inch),
            Paragraph("CONFIDENTIAL", ParagraphStyle("CONF", parent=s["body"], fontSize=10,
                      fontName="Helvetica-Bold", textColor=BRAND["red"])),
        ]

    def _render_executive(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("1. Executive Summary", s["h1"])]

        # KPI cards
        kpis = [
            ("Sites Operational", str(r.total_sites)),
            ("Fleet Capacity", f"{r.total_capacity_mw} MW"),
            ("Utilisation", f"{r.fleet_utilisation_pct}%"),
            ("Revenue", f"${r.total_revenue:,.0f}"),
        ]
        cells = []
        for label, value in kpis:
            cells.append([
                Paragraph(label, s["metric_label"]),
                Paragraph(value, s["metric_value"]),
            ])
        kpi_table = Table([cells], colWidths=[1.7 * inch] * 4)
        kpi_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), BRAND["light_bg"]),
            ("BOX", (0, 0), (-1, -1), 0.5, BRAND["border"]),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(kpi_table)
        elements.append(Spacer(1, 8))

        # Highlights
        highlights = []
        if r.risk_flags:
            high_risks = [f for f in r.risk_flags if f.severity == "high"]
            if high_risks:
                highlights.append(f"<b>Attention:</b> {len(high_risks)} high-severity risk flag(s) requiring review.")
        highlights.append(
            f"Fleet generated <b>${r.total_revenue:,.0f}</b> revenue with "
            f"<b>{r.ebitda_margin_pct}%</b> EBITDA margin."
        )
        if r.esg.total_kwht_exported > 0:
            highlights.append(
                f"Heat recovery: <b>{r.esg.total_kwht_exported:,.0f} kWht</b> exported, "
                f"offsetting <b>{r.esg.total_offset_kg:,.0f} kg CO2</b>."
            )

        for h in highlights:
            elements.append(Paragraph(h, s["body"]))
            elements.append(Spacer(1, 3))

        return elements

    def _render_financial(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("2. Financial Summary", s["h1"])]

        header = [Paragraph("Site", s["th"]), Paragraph("Colo", s["th"]),
                  Paragraph("Power", s["th"]), Paragraph("Other", s["th"]),
                  Paragraph("Total", s["th"])]
        rows = [header]
        for site in r.sites:
            rows.append([
                Paragraph(site.site_name, s["td"]),
                Paragraph(f"${site.revenue_colo:,.0f}", s["td_right"]),
                Paragraph(f"${site.revenue_power:,.0f}", s["td_right"]),
                Paragraph(f"${site.revenue_other:,.0f}", s["td_right"]),
                Paragraph(f"${site.revenue_total:,.0f}", s["td_right"]),
            ])
        # Total row
        rows.append([
            Paragraph("<b>Total</b>", s["td"]),
            Paragraph(f"<b>${sum(s2.revenue_colo for s2 in r.sites):,.0f}</b>", s["td_right"]),
            Paragraph(f"<b>${sum(s2.revenue_power for s2 in r.sites):,.0f}</b>", s["td_right"]),
            Paragraph(f"<b>${sum(s2.revenue_other for s2 in r.sites):,.0f}</b>", s["td_right"]),
            Paragraph(f"<b>${r.total_revenue:,.0f}</b>", s["td_right"]),
        ])

        table = self._make_table(rows, [2.0 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch])
        elements.append(table)
        elements.append(Spacer(1, 8))

        elements.append(Paragraph(
            f"EBITDA proxy: <b>${r.ebitda_proxy:,.0f}</b> ({r.ebitda_margin_pct}% margin) | "
            f"SLA credits: <b>${r.sla_credits_total:,.0f}</b>",
            s["body"],
        ))

        return elements

    def _render_operational(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("3. Operational Metrics", s["h1"])]

        header = [
            Paragraph("Site", s["th"]), Paragraph("Avail %", s["th"]),
            Paragraph("PUE", s["th"]), Paragraph("Util %", s["th"]),
            Paragraph("Heat kWht", s["th"]), Paragraph("Incidents", s["th"]),
        ]
        rows = [header]
        for site in r.sites:
            avail_color = BRAND["green"] if site.sla_met else BRAND["red"]
            rows.append([
                Paragraph(site.site_name, s["td"]),
                Paragraph(f"<font color='{avail_color.hexval()}'>{site.availability_pct}%</font>", s["td_right"]),
                Paragraph(str(site.pue), s["td_right"]),
                Paragraph(f"{site.utilisation_pct}%", s["td_right"]),
                Paragraph(f"{site.heat_export_kwht:,.0f}", s["td_right"]),
                Paragraph(f"{site.p0_count}×P0, {site.p1_count}×P1", s["td_right"]),
            ])

        table = self._make_table(rows, [1.8 * inch, 0.9 * inch, 0.7 * inch, 0.8 * inch, 1.2 * inch, 1.2 * inch])
        elements.append(table)
        return elements

    def _render_sla(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("4. SLA Compliance", s["h1"])]
        elements.append(Paragraph(
            f"Customers meeting SLA: <b>{r.sla_customers_met}/{r.sla_customers_total}</b> | "
            f"Credits issued: <b>${r.sla_credits_total:,.0f}</b>",
            s["body"],
        ))
        for site in r.sites:
            if not site.sla_met:
                elements.append(Paragraph(
                    f"<font color='{BRAND['red'].hexval()}'><b>{site.site_name}:</b></font> "
                    f"{site.availability_pct}% vs {site.sla_target_pct}% target — "
                    f"{site.total_downtime_min} min unplanned downtime",
                    s["body"],
                ))
        return elements

    def _render_incidents(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("5. Incident Log", s["h1"])]
        if not r.incidents:
            elements.append(Paragraph("No P0/P1 incidents recorded this period.", s["body"]))
            return elements

        header = [
            Paragraph("Date", s["th"]), Paragraph("Pri", s["th"]),
            Paragraph("Description", s["th"]), Paragraph("Duration", s["th"]),
            Paragraph("Root Cause", s["th"]), Paragraph("Resolution", s["th"]),
        ]
        rows = [header]
        for inc in r.incidents:
            rows.append([
                Paragraph(inc.raised_at[:10], s["td"]),
                Paragraph(inc.priority, s["td"]),
                Paragraph(inc.description, s["td"]),
                Paragraph(f"{inc.duration_min} min", s["td_right"]),
                Paragraph(inc.root_cause, s["td"]),
                Paragraph(inc.resolution, s["td"]),
            ])

        table = self._make_table(rows, [0.8 * inch, 0.4 * inch, 1.6 * inch, 0.7 * inch, 1.1 * inch, 2.0 * inch])
        elements.append(table)
        return elements

    def _render_capacity(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("6. Capacity Status", s["h1"])]
        for site in r.sites:
            avail = site.total_kw - site.sold_kw
            elements.append(Paragraph(
                f"<b>{site.site_name}:</b> {site.sold_kw} kW sold / {site.total_kw} kW total "
                f"({site.utilisation_pct}%) — <b>{avail} kW available</b>",
                s["body"],
            ))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(
            f"Sales pipeline: <b>{r.pipeline_deals} deals</b> valued at <b>${r.pipeline_value:,.0f}</b>",
            s["body"],
        ))
        return elements

    def _render_esg(self, r: LenderReport, s: dict) -> list:
        e = r.esg
        elements = [Paragraph("7. ESG / Sustainability", s["h1"])]

        kpis = [
            ("Heat Exported", f"{e.total_kwht_exported:,.0f} kWht"),
            ("CO2 Offset", f"{e.total_offset_kg:,.0f} kg"),
            ("Net Carbon", f"{e.net_carbon_kg:,.0f} kg"),
            ("Fleet PUE", str(e.fleet_pue)),
        ]
        cells = []
        for label, value in kpis:
            cells.append([Paragraph(label, s["metric_label"]), Paragraph(value, s["metric_value"])])

        kpi_table = Table([cells], colWidths=[1.7 * inch] * 4)
        kpi_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), BRAND["light_bg"]),
            ("BOX", (0, 0), (-1, -1), 0.5, BRAND["border"]),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(kpi_table)
        elements.append(Spacer(1, 6))
        elements.append(Paragraph(
            f"Scope 1: {e.total_scope1_kg:,.0f} kg | Scope 2: {e.total_scope2_kg:,.0f} kg | "
            f"Renewable: {e.renewable_pct}%",
            s["small"],
        ))
        return elements

    def _render_maintenance(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("8. Maintenance Summary", s["h1"])]
        elements.append(Paragraph(
            f"Completed: <b>{r.maintenance_completed}</b> work orders | "
            f"Upcoming: <b>{r.maintenance_upcoming}</b> scheduled",
            s["body"],
        ))
        return elements

    def _render_risk(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("9. Risk Flags", s["h1"])]
        if not r.risk_flags:
            elements.append(Paragraph("No risk flags this period.", s["body"]))
            return elements

        for flag in r.risk_flags:
            color = {"high": BRAND["red"], "medium": BRAND["amber"], "low": BRAND["muted"]}[flag.severity]
            elements.append(Paragraph(
                f"<font color='{color.hexval()}'><b>[{flag.severity.upper()}]</b></font> "
                f"{flag.category.replace('_', ' ').title()} — {flag.description}",
                s["body"],
            ))
            elements.append(Spacer(1, 2))
        return elements

    def _render_trend(self, r: LenderReport, s: dict) -> list:
        """Quarterly: 90-day trending table."""
        if not r.trend:
            return []
        elements = [Paragraph("10. Quarterly Trends", s["h1"])]

        header = [Paragraph("Month", s["th"]), Paragraph("Revenue", s["th"]),
                  Paragraph("Avail %", s["th"]), Paragraph("PUE", s["th"]),
                  Paragraph("Heat kWht %", s["th"]), Paragraph("Util %", s["th"])]
        rows = [header]
        for tp in r.trend:
            rows.append([
                Paragraph(tp.month, s["td"]),
                Paragraph(f"${tp.revenue:,.0f}", s["td_right"]),
                Paragraph(f"{tp.availability}%", s["td_right"]),
                Paragraph(str(tp.pue), s["td_right"]),
                Paragraph(f"{tp.heat_export}%", s["td_right"]),
                Paragraph(f"{tp.utilisation}%", s["td_right"]),
            ])

        table = self._make_table(rows, [1.1 * inch, 1.2 * inch, 1.0 * inch, 0.8 * inch, 1.2 * inch, 1.0 * inch])
        elements.append(table)
        return elements

    def _render_capex(self, r: LenderReport, s: dict) -> list:
        if r.capex_budget == 0:
            return []
        elements = [Paragraph("11. Capex Tracking", s["h1"])]
        elements.append(Paragraph(
            f"Budget: <b>${r.capex_budget:,.0f}</b> | "
            f"Actual: <b>${r.capex_actual:,.0f}</b> | "
            f"Variance: <b>{r.capex_variance_pct:+.1f}%</b>",
            s["body"],
        ))
        return elements

    def _render_benchmark(self, r: LenderReport, s: dict) -> list:
        elements = [Paragraph("12. Industry Benchmarks", s["h1"])]
        header = [Paragraph("Metric", s["th"]), Paragraph("MicroLink", s["th"]),
                  Paragraph("Industry Avg", s["th"]), Paragraph("Top Quartile", s["th"])]
        rows = [header]
        benchmarks = [
            ("PUE", str(r.esg.fleet_pue), "1.58", "1.20"),
            ("Availability", f"{r.sites[0].availability_pct}%" if r.sites else "N/A", "99.95%", "99.999%"),
            ("Heat Recovery", f"{r.sites[0].heat_export_pct}%" if r.sites else "N/A", "< 5%", "~30%"),
            ("WUE (L/kWh)", "0.00", "1.80", "0.50"),
        ]
        for metric, ml, avg, top in benchmarks:
            rows.append([Paragraph(metric, s["td"]), Paragraph(f"<b>{ml}</b>", s["td"]),
                         Paragraph(avg, s["td"]), Paragraph(top, s["td"])])

        table = self._make_table(rows, [1.7 * inch] * 4)
        elements.append(table)
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(
            "Sources: Uptime Institute 2025 Global Survey, EPA Energy Star, The Green Grid",
            s["small"],
        ))
        return elements

    def _make_table(self, rows: list, col_widths: list) -> Table:
        """Standard branded table."""
        table = Table(rows, colWidths=col_widths, repeatRows=1)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND["dark"]),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, -1), (-1, -1), 1, BRAND["border"]),
        ]
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), BRAND["light_bg"]))
            style_cmds.append(("LINEBELOW", (0, i), (-1, i), 0.5, BRAND["border"]))
        table.setStyle(TableStyle(style_cmds))
        return table


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    from unittest.mock import MagicMock

    print("=" * 60)
    print("Lender Report Generator — Stub Test")
    print("=" * 60)

    mock_session = MagicMock(spec=Session)
    generator = LenderReportGenerator(mock_session)

    # ── Monthly report ──
    print("\n─── Monthly Report ─────────────────────────────────")
    report = await generator.generate_monthly("2026-01", lender_name="Macquarie Infrastructure")
    print(f"  Period: {report.period_label}")
    print(f"  Sites: {report.total_sites} | Capacity: {report.total_capacity_mw} MW")
    print(f"  Revenue: ${report.total_revenue:,.0f} | EBITDA: ${report.ebitda_proxy:,.0f} ({report.ebitda_margin_pct}%)")
    print(f"  Utilisation: {report.fleet_utilisation_pct}%")
    print(f"  SLA: {report.sla_customers_met}/{report.sla_customers_total} met | Credits: ${report.sla_credits_total:,.0f}")
    print(f"  Incidents: {len(report.incidents)}")
    print(f"  Risk flags: {len(report.risk_flags)}")
    print(f"  ESG: {report.esg.total_kwht_exported:,.0f} kWht exported, {report.esg.total_offset_kg:,.0f} kg CO2 offset")

    pdf_monthly = generator.render_pdf(report)
    monthly_path = "/home/claude/lender_monthly_sample.pdf"
    with open(monthly_path, "wb") as f:
        f.write(pdf_monthly)
    print(f"\n  PDF: {monthly_path} ({len(pdf_monthly):,} bytes)")

    assert len(pdf_monthly) > 2000
    assert pdf_monthly[:5] == b"%PDF-"
    assert report.total_sites == 1
    assert report.total_revenue > 0
    assert len(report.incidents) == 3
    assert len(report.risk_flags) == 2
    print("  ✓ Monthly assertions passed")

    # ── Quarterly report ──
    print("\n─── Quarterly Report ───────────────────────────────")
    q_report = await generator.generate_quarterly("2026-Q1", lender_name="Macquarie Infrastructure")
    print(f"  Period: {q_report.period_label}")
    print(f"  Trend points: {len(q_report.trend)}")
    print(f"  Capex: ${q_report.capex_actual:,.0f} / ${q_report.capex_budget:,.0f} ({q_report.capex_variance_pct:+.1f}%)")
    print(f"  Sections: {len(q_report.include_sections)}")

    pdf_quarterly = generator.render_pdf(q_report)
    quarterly_path = "/home/claude/lender_quarterly_sample.pdf"
    with open(quarterly_path, "wb") as f:
        f.write(pdf_quarterly)
    print(f"  PDF: {quarterly_path} ({len(pdf_quarterly):,} bytes)")

    assert len(pdf_quarterly) > len(pdf_monthly), "Quarterly should be longer than monthly"
    assert len(q_report.trend) == 3
    assert "benchmark" in q_report.include_sections
    print("  ✓ Quarterly assertions passed")

    print("\n✓ All assertions passed")


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
