"""
MCS Stream C — Task 5: Invoice Generator
==========================================
Service that assembles billing data from Tasks 2-4 into invoices,
persists the invoice record, and generates professional PDF output.

Orchestrates:
  - Task 2 (KWhCalculator)  → electrical line items
  - Task 3 (KWhtCalculator) → host heat credit (for host invoices)
  - Task 4 (SLAEngine)      → SLA credits
  - Manual adjustments      → one-off charges/credits

Usage:
    generator = InvoiceGenerator(api_client, session)

    # Single customer
    invoice = await generator.generate(customer_id=1, year=2026, month=1)

    # Batch — all customers at a site
    invoices = await generator.generate_batch(site_id="BALD-01", year=2026, month=1)

    # PDF
    pdf_bytes = generator.render_pdf(invoice)
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
from reportlab.lib.units import inch, mm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)

# Local imports — previous tasks
from billing_models import (
    Session,
    Customer, Contract, Invoice, InvoiceLineItem, ManualAdjustment,
    ContractType, ContractStatus, InvoiceStatus, LineItemType,
    RateType, AvailabilityClass,
    get_active_contract, get_active_contracts_for_site,
    get_customer_rack_assignments, get_rate, get_manual_adjustments,
    generate_invoice_number, get_sla_credit_pct, SLA_TARGETS,
)
from kwh_calculator import KWhCalculator, KWhBillingResult, StreamBClient as KWhStreamBClient
from sla_engine import SLAEngine, SLAReport, StreamBClient as SLAStreamBClient


# ─────────────────────────────────────────────
# Brand constants
# ─────────────────────────────────────────────

BRAND = {
    "company": "MicroLink Data Centers",
    "tagline": "Sustainable Compute Infrastructure",
    "address_1": "MicroLink Data Centers, Inc.",
    "address_2": "",
    "email": "billing@microlink.io",
    "phone": "",
    "website": "www.microlink.io",
    "primary_color": colors.HexColor("#0091ff"),
    "dark_color": colors.HexColor("#0d1219"),
    "text_color": colors.HexColor("#1a1a2e"),
    "muted_color": colors.HexColor("#5e6d80"),
    "border_color": colors.HexColor("#d0d7de"),
    "light_bg": colors.HexColor("#f6f8fa"),
    "accent_green": colors.HexColor("#00d68f"),
    "accent_red": colors.HexColor("#ef4444"),
}


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class InvoiceData:
    """Assembled invoice ready for PDF rendering and DB persistence."""
    # Header
    invoice_number: str
    customer_name: str
    customer_email: str
    customer_address: Dict[str, str]
    contract_ref: str

    # Dates
    period_start: date
    period_end: date
    issue_date: date
    due_date: date

    # Line items
    line_items: List[Dict[str, Any]]

    # Totals
    subtotal: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total: Decimal
    currency: str

    # Usage summary
    total_kwh: Decimal = Decimal("0")
    peak_demand_kw: Decimal = Decimal("0")
    avg_pue: Decimal = Decimal("0")

    # SLA summary
    availability_pct: Decimal = Decimal("100")
    sla_target_pct: Decimal = Decimal("99.9")
    sla_class: str = "B"
    sla_credit: Decimal = Decimal("0")

    # Metadata
    customer_id: int = 0
    contract_id: int = 0
    site_id: str = ""
    site_code: str = ""
    status: str = "draft"
    payment_terms_days: int = 30
    notes: str = ""

    # Data quality flags
    quality_flags: List[str] = field(default_factory=list)

    # Billing detail (for DB persistence)
    billing_result: Optional[KWhBillingResult] = None
    sla_report: Optional[SLAReport] = None


# ─────────────────────────────────────────────
# Core generator
# ─────────────────────────────────────────────

class InvoiceGenerator:
    """
    Orchestrates billing calculations and produces invoices.
    """

    def __init__(self, api_client, session: Session):
        """
        api_client: StreamBClient instance (shared across calculators).
        session: SQLAlchemy session for billing DB.
        """
        self.api = api_client
        self.session = session
        self.kwh_calc = KWhCalculator(api_client, session)
        self.sla_engine = SLAEngine(api_client, session)

    async def generate(
        self,
        customer_id: int,
        year: int,
        month: int,
        draft: bool = True,
        skip_db: bool = False,
    ) -> InvoiceData:
        """
        Generate an invoice for a single customer.

        Args:
            customer_id: Target customer
            year, month: Billing period
            draft: If True, create as DRAFT (review before sending)
            skip_db: If True, don't persist to DB (for preview/testing)
        """

        # ── 1. Load customer & contract ──
        contract = get_active_contract(self.session, customer_id, ContractType.COLO_MSA)
        if contract is None:
            raise ValueError(f"No active colo MSA for customer {customer_id}")

        customer = contract.customer

        # Period
        period_start = date(year, month, 1)
        period_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        # Site code from contract site_id (e.g. "BALD-01" → "BALD")
        site_code = contract.site_id.split("-")[0] if "-" in contract.site_id else contract.site_id

        # ── 2. Run kWh calculator ──
        billing_result = await self.kwh_calc.calculate(customer_id, year, month)

        # ── 3. Run SLA engine ──
        sla_report = await self.sla_engine.calculate(customer_id, year, month)

        # ── 4. Assemble line items ──
        line_items = []

        # Electrical line items from Task 2
        for item in billing_result.line_items:
            line_items.append({
                "line_type": item.line_type,
                "description": item.description,
                "quantity": item.quantity,
                "unit": item.unit,
                "unit_price": item.unit_price,
                "amount": item.amount,
                "metadata": item.metadata,
            })

        # Cross-connects (if rate exists)
        rate_xc = get_rate(self.session, contract.id, RateType.CROSS_CONNECT, period_start)
        if rate_xc and rate_xc.rate_value > 0:
            # Count from contract terms (default 2)
            xc_count = (contract.terms_json or {}).get("cross_connects", 0)
            if xc_count > 0:
                xc_amount = (Decimal(str(xc_count)) * rate_xc.rate_value).quantize(
                    Decimal("0.01"), ROUND_HALF_UP,
                )
                line_items.append({
                    "line_type": LineItemType.CROSS_CONNECT.value,
                    "description": f"Cross-connects ({xc_count}×)",
                    "quantity": Decimal(str(xc_count)),
                    "unit": "ea",
                    "unit_price": rate_xc.rate_value,
                    "amount": xc_amount,
                    "metadata": {},
                })

        # SLA credit (if breached)
        if sla_report.credit_amount > 0:
            line_items.append({
                "line_type": LineItemType.SLA_CREDIT.value,
                "description": (
                    f"SLA credit — {sla_report.availability_pct}% availability "
                    f"(target: {sla_report.sla_target_pct}%)"
                ),
                "quantity": Decimal("1"),
                "unit": "ea",
                "unit_price": -sla_report.credit_amount,
                "amount": -sla_report.credit_amount,
                "metadata": {
                    "availability_pct": str(sla_report.availability_pct),
                    "target_pct": str(sla_report.sla_target_pct),
                    "credit_tier_pct": str(sla_report.credit_tier_pct),
                    "incidents": len(sla_report.contributing_incidents),
                    "downtime_minutes": str(sla_report.unplanned_downtime_minutes),
                },
            })

        # Manual adjustments
        adjustments = get_manual_adjustments(
            self.session, customer_id, period_start, period_end,
        )
        for adj in adjustments:
            line_items.append({
                "line_type": LineItemType.MANUAL_ADJUSTMENT.value,
                "description": adj.description,
                "quantity": Decimal("1"),
                "unit": "ea",
                "unit_price": adj.amount,
                "amount": adj.amount,
                "metadata": {"adjustment_id": adj.id, "approved_by": adj.approved_by},
            })

        # ── 5. Totals ──
        subtotal = sum(item["amount"] for item in line_items)
        tax_rate = contract.tax_rate_pct
        tax_amount = (subtotal * tax_rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
        total = subtotal + tax_amount

        # ── 6. Invoice number ──
        invoice_number = generate_invoice_number(self.session, site_code, year, month)

        # ── 7. Dates ──
        issue_date = date.today()
        due_date = issue_date + timedelta(days=contract.payment_terms_days)

        # ── Build InvoiceData ──
        invoice = InvoiceData(
            invoice_number=invoice_number,
            customer_name=customer.name,
            customer_email=customer.billing_contact_email,
            customer_address=customer.billing_address or {},
            contract_ref=contract.contract_ref,
            period_start=period_start,
            period_end=period_end - timedelta(days=1),  # inclusive end
            issue_date=issue_date,
            due_date=due_date,
            line_items=line_items,
            subtotal=subtotal.quantize(Decimal("0.01")),
            tax_rate=tax_rate,
            tax_amount=tax_amount,
            total=total.quantize(Decimal("0.01")),
            currency=billing_result.currency,
            total_kwh=billing_result.total_kwh,
            peak_demand_kw=billing_result.peak_demand_kw,
            avg_pue=billing_result.facility_pue,
            availability_pct=sla_report.availability_pct,
            sla_target_pct=sla_report.sla_target_pct,
            sla_class=sla_report.availability_class,
            sla_credit=sla_report.credit_amount,
            customer_id=customer_id,
            contract_id=contract.id,
            site_id=contract.site_id,
            site_code=site_code,
            status="draft" if draft else "sent",
            payment_terms_days=contract.payment_terms_days,
            quality_flags=(
                billing_result.quality.flags + (
                    [f"SLA breached: {sla_report.summary_text}"]
                    if not sla_report.sla_met else []
                )
            ),
            billing_result=billing_result,
            sla_report=sla_report,
        )

        # ── 8. Persist to DB ──
        if not skip_db:
            self._persist_invoice(invoice, adjustments)

        return invoice

    async def generate_batch(
        self,
        site_id: str,
        year: int,
        month: int,
        draft: bool = True,
    ) -> List[InvoiceData]:
        """Generate invoices for all active colo customers at a site."""
        contracts = get_active_contracts_for_site(self.session, site_id)
        colo_contracts = [c for c in contracts if c.contract_type == ContractType.COLO_MSA]

        invoices = []
        for contract in colo_contracts:
            try:
                inv = await self.generate(
                    contract.customer_id, year, month, draft=draft,
                )
                invoices.append(inv)
            except Exception as e:
                print(f"[INVOICE ERROR] customer={contract.customer_id}: {e}")

        return invoices

    def _persist_invoice(
        self,
        invoice_data: InvoiceData,
        adjustments: List[ManualAdjustment],
    ):
        """
        Write invoice and line items to billing DB.
        In production, this commits to the real session.
        """
        # Create Invoice record
        inv = Invoice(
            invoice_number=invoice_data.invoice_number,
            customer_id=invoice_data.customer_id,
            contract_id=invoice_data.contract_id,
            period_start=invoice_data.period_start,
            period_end=invoice_data.period_end + timedelta(days=1),
            status=InvoiceStatus(invoice_data.status),
            subtotal=invoice_data.subtotal,
            tax=invoice_data.tax_amount,
            total=invoice_data.total,
            currency=invoice_data.currency,
            due_date=invoice_data.due_date,
            usage_summary={
                "total_kwh": str(invoice_data.total_kwh),
                "peak_kw": str(invoice_data.peak_demand_kw),
                "avg_pue": str(invoice_data.avg_pue),
                "availability_pct": str(invoice_data.availability_pct),
            },
        )

        self.session.add(inv)
        self.session.flush()  # get ID

        # Line items
        for i, item in enumerate(invoice_data.line_items):
            li = InvoiceLineItem(
                invoice_id=inv.id,
                line_type=LineItemType(item["line_type"]),
                description=item["description"],
                quantity=item["quantity"],
                unit=item["unit"],
                unit_price=item["unit_price"],
                amount=item["amount"],
                sort_order=i,
                metadata_json=item.get("metadata"),
            )
            self.session.add(li)

        # Mark adjustments as applied
        for adj in adjustments:
            adj.applied_to_invoice_id = inv.id

        self.session.commit()

    # ─────────────────────────────────────────
    # Stripe integration placeholder
    # ─────────────────────────────────────────

    async def submit_to_stripe(self, invoice_data: InvoiceData) -> Dict[str, Any]:
        """
        Placeholder: submit finalised invoice to Stripe API.
        In production, creates a Stripe Invoice with line items.
        """
        # import stripe
        # stripe.api_key = settings.STRIPE_SECRET_KEY
        #
        # stripe_invoice = stripe.Invoice.create(
        #     customer=customer.stripe_customer_id,
        #     collection_method="send_invoice",
        #     days_until_due=invoice_data.payment_terms_days,
        # )
        # for item in invoice_data.line_items:
        #     stripe.InvoiceItem.create(
        #         customer=customer.stripe_customer_id,
        #         invoice=stripe_invoice.id,
        #         description=item["description"],
        #         amount=int(item["amount"] * 100),  # cents
        #         currency=invoice_data.currency.lower(),
        #     )
        # stripe_invoice.send_invoice()

        return {
            "status": "placeholder",
            "message": "Stripe integration not yet configured",
            "invoice_number": invoice_data.invoice_number,
        }

    # ─────────────────────────────────────────
    # PDF rendering
    # ─────────────────────────────────────────

    def render_pdf(self, invoice: InvoiceData) -> bytes:
        """Generate PDF invoice and return as bytes."""
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            topMargin=0.5 * inch,
            bottomMargin=0.75 * inch,
            leftMargin=0.7 * inch,
            rightMargin=0.7 * inch,
        )

        styles = self._build_styles()
        story = []

        # ── Header ──
        story.extend(self._render_header(invoice, styles))
        story.append(Spacer(1, 16))

        # ── Invoice meta (number, dates, customer) ──
        story.extend(self._render_meta(invoice, styles))
        story.append(Spacer(1, 20))

        # ── Line items table ──
        story.extend(self._render_line_items(invoice, styles))
        story.append(Spacer(1, 12))

        # ── Totals ──
        story.extend(self._render_totals(invoice, styles))
        story.append(Spacer(1, 20))

        # ── Usage summary ──
        story.extend(self._render_usage_summary(invoice, styles))
        story.append(Spacer(1, 12))

        # ── SLA summary ──
        story.extend(self._render_sla_summary(invoice, styles))
        story.append(Spacer(1, 20))

        # ── Payment terms ──
        story.extend(self._render_payment_terms(invoice, styles))
        story.append(Spacer(1, 24))

        # ── Data quality flags ──
        if invoice.quality_flags:
            story.extend(self._render_quality_flags(invoice, styles))
            story.append(Spacer(1, 16))

        # ── Footer ──
        story.extend(self._render_footer(styles))

        doc.build(story)
        return buf.getvalue()

    def _build_styles(self) -> Dict[str, ParagraphStyle]:
        """Custom paragraph styles for the invoice."""
        base = getSampleStyleSheet()
        return {
            "brand_name": ParagraphStyle(
                "BrandName", parent=base["Normal"],
                fontSize=18, fontName="Helvetica-Bold",
                textColor=BRAND["primary_color"], spaceAfter=2,
            ),
            "brand_tag": ParagraphStyle(
                "BrandTag", parent=base["Normal"],
                fontSize=8, fontName="Helvetica",
                textColor=BRAND["muted_color"],
            ),
            "invoice_title": ParagraphStyle(
                "InvoiceTitle", parent=base["Normal"],
                fontSize=22, fontName="Helvetica-Bold",
                textColor=BRAND["text_color"], alignment=TA_RIGHT,
            ),
            "meta_label": ParagraphStyle(
                "MetaLabel", parent=base["Normal"],
                fontSize=7.5, fontName="Helvetica-Bold",
                textColor=BRAND["muted_color"],
                spaceAfter=1,
            ),
            "meta_value": ParagraphStyle(
                "MetaValue", parent=base["Normal"],
                fontSize=9.5, fontName="Helvetica",
                textColor=BRAND["text_color"], spaceAfter=6,
            ),
            "section_head": ParagraphStyle(
                "SectionHead", parent=base["Normal"],
                fontSize=9, fontName="Helvetica-Bold",
                textColor=BRAND["primary_color"],
                spaceAfter=6, spaceBefore=4,
            ),
            "body": ParagraphStyle(
                "Body", parent=base["Normal"],
                fontSize=9, fontName="Helvetica",
                textColor=BRAND["text_color"], leading=13,
            ),
            "body_small": ParagraphStyle(
                "BodySmall", parent=base["Normal"],
                fontSize=8, fontName="Helvetica",
                textColor=BRAND["muted_color"], leading=11,
            ),
            "body_right": ParagraphStyle(
                "BodyRight", parent=base["Normal"],
                fontSize=9, fontName="Helvetica",
                textColor=BRAND["text_color"], alignment=TA_RIGHT,
            ),
            "total_label": ParagraphStyle(
                "TotalLabel", parent=base["Normal"],
                fontSize=10, fontName="Helvetica-Bold",
                textColor=BRAND["text_color"], alignment=TA_RIGHT,
            ),
            "total_value": ParagraphStyle(
                "TotalValue", parent=base["Normal"],
                fontSize=14, fontName="Helvetica-Bold",
                textColor=BRAND["primary_color"], alignment=TA_RIGHT,
            ),
            "footer": ParagraphStyle(
                "Footer", parent=base["Normal"],
                fontSize=7.5, fontName="Helvetica",
                textColor=BRAND["muted_color"], alignment=TA_CENTER,
            ),
        }

    def _render_header(self, inv: InvoiceData, s: dict) -> list:
        """Logo placeholder + company name + INVOICE title."""
        # Two-column header: brand left, INVOICE right
        left = []
        left.append(Paragraph(BRAND["company"], s["brand_name"]))
        left.append(Paragraph(BRAND["tagline"], s["brand_tag"]))

        right = []
        right.append(Paragraph("INVOICE", s["invoice_title"]))
        status_text = "DRAFT" if inv.status == "draft" else ""
        if status_text:
            right.append(Paragraph(
                status_text,
                ParagraphStyle(
                    "DraftBadge", parent=s["body"],
                    fontSize=10, fontName="Helvetica-Bold",
                    textColor=colors.HexColor("#f59e0b"), alignment=TA_RIGHT,
                ),
            ))

        header_table = Table(
            [[left, right]],
            colWidths=[3.5 * inch, 3.3 * inch],
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        return [
            header_table,
            Spacer(1, 6),
            HRFlowable(
                width="100%", thickness=2,
                color=BRAND["primary_color"], spaceBefore=0, spaceAfter=0,
            ),
        ]

    def _render_meta(self, inv: InvoiceData, s: dict) -> list:
        """Invoice number, dates, customer details in two columns."""
        # Left column: Bill To
        addr = inv.customer_address
        addr_lines = [inv.customer_name]
        if addr.get("street"):
            addr_lines.append(addr["street"])
        city_line = ", ".join(filter(None, [
            addr.get("city", ""), addr.get("state", ""), addr.get("zip", ""),
        ]))
        if city_line:
            addr_lines.append(city_line)
        if addr.get("country"):
            addr_lines.append(addr["country"])

        left_content = [
            Paragraph("BILL TO", s["meta_label"]),
            Paragraph("<br/>".join(addr_lines), s["meta_value"]),
            Paragraph("CONTRACT", s["meta_label"]),
            Paragraph(inv.contract_ref, s["meta_value"]),
        ]

        # Right column: Invoice details
        right_content = [
            Paragraph("INVOICE NUMBER", s["meta_label"]),
            Paragraph(inv.invoice_number, s["meta_value"]),
            Paragraph("BILLING PERIOD", s["meta_label"]),
            Paragraph(
                f"{inv.period_start.strftime('%B %d, %Y')} — "
                f"{inv.period_end.strftime('%B %d, %Y')}",
                s["meta_value"],
            ),
            Paragraph("ISSUE DATE", s["meta_label"]),
            Paragraph(inv.issue_date.strftime("%B %d, %Y"), s["meta_value"]),
            Paragraph("DUE DATE", s["meta_label"]),
            Paragraph(inv.due_date.strftime("%B %d, %Y"), s["meta_value"]),
        ]

        meta_table = Table(
            [[left_content, right_content]],
            colWidths=[3.5 * inch, 3.3 * inch],
        )
        meta_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        return [meta_table]

    def _render_line_items(self, inv: InvoiceData, s: dict) -> list:
        """Line items table with header row."""
        # Table header
        header = [
            Paragraph("Description", ParagraphStyle("TH", parent=s["body"], fontName="Helvetica-Bold", textColor=colors.white, fontSize=8)),
            Paragraph("Qty", ParagraphStyle("THR", parent=s["body"], fontName="Helvetica-Bold", textColor=colors.white, fontSize=8, alignment=TA_RIGHT)),
            Paragraph("Unit", ParagraphStyle("THC", parent=s["body"], fontName="Helvetica-Bold", textColor=colors.white, fontSize=8, alignment=TA_CENTER)),
            Paragraph("Rate", ParagraphStyle("THR2", parent=s["body"], fontName="Helvetica-Bold", textColor=colors.white, fontSize=8, alignment=TA_RIGHT)),
            Paragraph("Amount", ParagraphStyle("THR3", parent=s["body"], fontName="Helvetica-Bold", textColor=colors.white, fontSize=8, alignment=TA_RIGHT)),
        ]

        rows = [header]

        for item in inv.line_items:
            is_credit = item["amount"] < 0
            amt_style = ParagraphStyle(
                "Amt", parent=s["body"], fontSize=9, alignment=TA_RIGHT,
                textColor=BRAND["accent_green"] if is_credit else BRAND["text_color"],
            )
            qty_str = f"{item['quantity']:,.2f}" if item["quantity"] != Decimal("1") else "1"

            rows.append([
                Paragraph(item["description"], s["body"]),
                Paragraph(qty_str, ParagraphStyle("QR", parent=s["body"], fontSize=9, alignment=TA_RIGHT)),
                Paragraph(item["unit"], ParagraphStyle("UC", parent=s["body"], fontSize=9, alignment=TA_CENTER)),
                Paragraph(f"${item['unit_price']:,.4f}", ParagraphStyle("RR", parent=s["body"], fontSize=9, alignment=TA_RIGHT)),
                Paragraph(f"${item['amount']:,.2f}", amt_style),
            ])

        table = Table(
            rows,
            colWidths=[2.9 * inch, 0.8 * inch, 0.6 * inch, 1.2 * inch, 1.3 * inch],
            repeatRows=1,
        )

        # Styling
        style_cmds = [
            # Header row
            ("BACKGROUND", (0, 0), (-1, 0), BRAND["dark_color"]),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            # All cells
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # Grid
            ("LINEBELOW", (0, 0), (-1, 0), 1, BRAND["dark_color"]),
            ("LINEBELOW", (0, -1), (-1, -1), 1, BRAND["border_color"]),
        ]

        # Alternate row shading
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), BRAND["light_bg"]))
            # Light bottom border for each row
            style_cmds.append(("LINEBELOW", (0, i), (-1, i), 0.5, BRAND["border_color"]))

        table.setStyle(TableStyle(style_cmds))

        return [
            Paragraph("Charges & Credits", s["section_head"]),
            table,
        ]

    def _render_totals(self, inv: InvoiceData, s: dict) -> list:
        """Subtotal, tax, total block aligned right."""
        rows = [
            ["Subtotal", f"${inv.subtotal:,.2f}"],
        ]
        if inv.tax_rate > 0:
            rows.append([f"Tax ({inv.tax_rate * 100:.2f}%)", f"${inv.tax_amount:,.2f}"])
        rows.append(["Total Due", f"${inv.total:,.2f}"])

        data = []
        for i, (label, value) in enumerate(rows):
            is_total = (i == len(rows) - 1)
            l_style = s["total_label"] if is_total else ParagraphStyle(
                "SL", parent=s["body"], fontSize=9, alignment=TA_RIGHT,
            )
            v_style = s["total_value"] if is_total else ParagraphStyle(
                "SV", parent=s["body"], fontSize=9, alignment=TA_RIGHT,
                fontName="Helvetica-Bold",
            )
            data.append([
                Paragraph(label, l_style),
                Paragraph(value, v_style),
            ])

        table = Table(
            data,
            colWidths=[5.0 * inch, 1.8 * inch],
        )
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEABOVE", (1, -1), (1, -1), 1.5, BRAND["primary_color"]),
        ]))

        return [table]

    def _render_usage_summary(self, inv: InvoiceData, s: dict) -> list:
        """Usage metrics: kWh, peak kW, PUE."""
        metrics = [
            ("Total IT Energy", f"{inv.total_kwh:,.0f} kWh"),
            ("Peak Demand", f"{inv.peak_demand_kw:,.1f} kW"),
            ("Average PUE", f"{inv.avg_pue:.3f}"),
        ]

        cells = []
        for label, value in metrics:
            cells.append([
                Paragraph(label, s["meta_label"]),
                Paragraph(value, ParagraphStyle(
                    "Metric", parent=s["body"],
                    fontSize=12, fontName="Helvetica-Bold",
                )),
            ])

        # Lay out as 3-column row
        metric_row = []
        for cell in cells:
            metric_row.append(cell)

        table = Table(
            [metric_row],
            colWidths=[2.27 * inch] * 3,
        )
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, -1), BRAND["light_bg"]),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("BOX", (0, 0), (-1, -1), 0.5, BRAND["border_color"]),
        ]))

        return [
            Paragraph("Usage Summary", s["section_head"]),
            table,
        ]

    def _render_sla_summary(self, inv: InvoiceData, s: dict) -> list:
        """SLA availability and credit summary."""
        met = inv.availability_pct >= inv.sla_target_pct
        status_color = BRAND["accent_green"] if met else BRAND["accent_red"]
        status_text = "MET" if met else "BREACHED"

        text = (
            f"<b>Availability:</b> {inv.availability_pct}% &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Target:</b> {inv.sla_target_pct}% (Class {inv.sla_class}) &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Status:</b> <font color='{status_color.hexval()}'>{status_text}</font>"
        )

        elements = [
            Paragraph("SLA Compliance", s["section_head"]),
            Paragraph(text, s["body"]),
        ]

        if inv.sla_credit > 0:
            elements.append(Spacer(1, 4))
            elements.append(Paragraph(
                f"SLA credit applied: <font color='{BRAND['accent_green'].hexval()}'>"
                f"-${inv.sla_credit:,.2f}</font>",
                s["body"],
            ))

        return elements

    def _render_payment_terms(self, inv: InvoiceData, s: dict) -> list:
        """Payment terms and due date."""
        return [
            HRFlowable(width="100%", thickness=0.5, color=BRAND["border_color"]),
            Spacer(1, 8),
            Paragraph(
                f"<b>Payment Terms:</b> Net {inv.payment_terms_days} days &nbsp;&nbsp;|&nbsp;&nbsp; "
                f"<b>Due:</b> {inv.due_date.strftime('%B %d, %Y')} &nbsp;&nbsp;|&nbsp;&nbsp; "
                f"<b>Currency:</b> {inv.currency}",
                s["body"],
            ),
        ]

    def _render_quality_flags(self, inv: InvoiceData, s: dict) -> list:
        """Data quality warnings (visible on draft invoices)."""
        elements = [
            Paragraph("Data Quality Notes", ParagraphStyle(
                "QH", parent=s["section_head"],
                textColor=colors.HexColor("#f59e0b"),
            )),
        ]
        for flag in inv.quality_flags:
            elements.append(Paragraph(f"  {flag}", s["body_small"]))
        return elements

    def _render_footer(self, s: dict) -> list:
        """Company contact footer."""
        return [
            HRFlowable(width="100%", thickness=0.5, color=BRAND["border_color"]),
            Spacer(1, 6),
            Paragraph(
                f"{BRAND['company']} &nbsp;|&nbsp; "
                f"{BRAND['email']} &nbsp;|&nbsp; "
                f"{BRAND['website']}",
                s["footer"],
            ),
            Paragraph(
                "Thank you for choosing MicroLink. Sustainable compute, delivered.",
                ParagraphStyle("FootNote", parent=s["footer"], fontSize=7, spaceBefore=4),
            ),
        ]


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def _run_stub_test():
    """Full pipeline test: calculate billing → generate invoice → render PDF."""
    from unittest.mock import MagicMock
    import sys

    print("=" * 60)
    print("Invoice Generator — Stub Test")
    print("=" * 60)

    # ── Mock DB objects ──
    mock_session = MagicMock(spec=Session)
    mock_session.add = MagicMock()
    mock_session.flush = MagicMock()
    mock_session.commit = MagicMock()

    mock_customer = MagicMock(spec=Customer)
    mock_customer.id = 1
    mock_customer.name = "TensorFlow Cloud Services"
    mock_customer.billing_contact_email = "billing@tfcloud.io"
    mock_customer.billing_address = {
        "street": "100 Technology Drive, Suite 400",
        "city": "Syracuse",
        "state": "NY",
        "zip": "13201",
        "country": "United States",
    }

    mock_contract = MagicMock(spec=Contract)
    mock_contract.id = 1
    mock_contract.customer = mock_customer
    mock_contract.customer_id = 1
    mock_contract.contract_type = ContractType.COLO_MSA
    mock_contract.site_id = "BALD-01"
    mock_contract.contract_ref = "ML-BALD-2026-001"
    mock_contract.start_date = date(2026, 1, 1)
    mock_contract.end_date = None
    mock_contract.status = ContractStatus.ACTIVE
    mock_contract.payment_terms_days = 30
    mock_contract.tax_rate_pct = Decimal("0.08")
    mock_contract.terms_json = {"cross_connects": 2}
    mock_contract.billing_meters = []
    mock_contract.rate_schedules = []

    mock_assignment = MagicMock()
    mock_assignment.block_id = "BALD-BLK-01"
    mock_assignment.committed_kw = Decimal("420")
    mock_assignment.availability_class = AvailabilityClass.B
    mock_assignment.rack_ids = ["R01", "R02", "R03", "R04", "R05", "R06"]

    # Rates
    def mock_get_rate(s, cid, rt, d=None):
        rates = {
            RateType.COLO_PER_KW: Decimal("170.00"),
            RateType.POWER_PER_KWH: Decimal("0.085"),
            RateType.DEMAND_CHARGE: Decimal("12.50"),
            RateType.COOLING_PUE: Decimal("0.085"),
            RateType.CROSS_CONNECT: Decimal("350.00"),
        }
        if rt in rates:
            m = MagicMock()
            m.rate_value = rates[rt]
            m.currency = "USD"
            return m
        return None

    # Patch all module-level functions across all imported modules
    import kwh_calculator as kwh_mod
    import sla_engine as sla_mod
    this_module = sys.modules[__name__]

    patches = {}
    for mod in [this_module, kwh_mod, sla_mod]:
        for fn_name in [
            "get_active_contract", "get_customer_rack_assignments",
            "get_rate", "get_manual_adjustments", "generate_invoice_number",
            "get_active_contracts_for_site", "get_planned_maintenance_windows",
        ]:
            if hasattr(mod, fn_name):
                patches[(mod, fn_name)] = getattr(mod, fn_name)

    # Apply patches
    for mod in [this_module, kwh_mod, sla_mod]:
        if hasattr(mod, "get_active_contract"):
            setattr(mod, "get_active_contract", lambda s, cid, ct=None: mock_contract)
        if hasattr(mod, "get_customer_rack_assignments"):
            setattr(mod, "get_customer_rack_assignments", lambda s, cid, bid=None: [mock_assignment])
        if hasattr(mod, "get_rate"):
            setattr(mod, "get_rate", mock_get_rate)
        if hasattr(mod, "get_manual_adjustments"):
            setattr(mod, "get_manual_adjustments", lambda s, cid, ps, pe: [])
        if hasattr(mod, "generate_invoice_number"):
            setattr(mod, "generate_invoice_number", lambda s, sc, y, m: f"ML-BALD-{y}{m:02d}-001")
        if hasattr(mod, "get_active_contracts_for_site"):
            setattr(mod, "get_active_contracts_for_site", lambda s, sid: [mock_contract])
        if hasattr(mod, "get_planned_maintenance_windows"):
            setattr(mod, "get_planned_maintenance_windows", lambda s, bid, ps, pe, valid_only=True: [])

    try:
        # Use combined stub client that covers both kWh and SLA endpoints
        from kwh_calculator import StubStreamBClient as KWhStub
        from sla_engine import StubStreamBClient as SLAStub

        class CombinedStub(KWhStub):
            """Stub that handles both kWh and SLA API calls."""
            def __init__(self):
                super().__init__()
                self._sla_stub = SLAStub()

            async def get_alarms(self, block_id, start, end, state=None):
                return await self._sla_stub.get_alarms(block_id, start, end, state)

            async def get_active_alarms(self, block_id):
                return await self._sla_stub.get_active_alarms(block_id)

        stub_api = CombinedStub()

        generator = InvoiceGenerator(stub_api, mock_session)
        invoice = await generator.generate(
            customer_id=1, year=2026, month=1,
            draft=True, skip_db=True,
        )

        # ── Display ──
        print(f"\nInvoice: {invoice.invoice_number}")
        print(f"Customer: {invoice.customer_name}")
        print(f"Period: {invoice.period_start} → {invoice.period_end}")
        print(f"Contract: {invoice.contract_ref}")
        print()

        print("─── Line Items ─────────────────────────────────────")
        for item in invoice.line_items:
            prefix = "  " if item["amount"] >= 0 else "  "
            print(f"{prefix}{item['description']}")
            print(f"    {item['quantity']:>10,.2f} {item['unit']} × ${item['unit_price']:,.4f} = ${item['amount']:>10,.2f}")
        print()
        print(f"  {'Subtotal':>50} ${invoice.subtotal:>10,.2f}")
        if invoice.tax_rate > 0:
            print(f"  {'Tax (' + str(invoice.tax_rate * 100) + '%)':>50} ${invoice.tax_amount:>10,.2f}")
        print(f"  {'TOTAL DUE':>50} ${invoice.total:>10,.2f}")
        print()

        print(f"─── Usage ──────────────────────────────────────────")
        print(f"  kWh: {invoice.total_kwh:,.0f}  |  Peak: {invoice.peak_demand_kw:.1f} kW  |  PUE: {invoice.avg_pue}")
        print()

        print(f"─── SLA ────────────────────────────────────────────")
        print(f"  {invoice.availability_pct}% (target {invoice.sla_target_pct}%, class {invoice.sla_class})")
        if invoice.sla_credit > 0:
            print(f"  Credit: -${invoice.sla_credit:,.2f}")
        print()

        if invoice.quality_flags:
            print(f"─── Flags ──────────────────────────────────────────")
            for f in invoice.quality_flags:
                print(f"  ⚠ {f}")
            print()

        # ── Render PDF ──
        print("Rendering PDF...")
        pdf_bytes = generator.render_pdf(invoice)
        pdf_path = "/home/claude/invoice_sample.pdf"
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        print(f"PDF written: {pdf_path} ({len(pdf_bytes):,} bytes)")

        # ── Assertions ──
        print()
        assert invoice.subtotal > 0, "Subtotal should be > 0"
        assert invoice.total > invoice.subtotal, "Total should include tax"
        assert len(invoice.line_items) >= 4, "Should have colo, power, demand, cooling at minimum"
        assert invoice.invoice_number.startswith("ML-BALD-"), "Invoice number format"
        assert len(pdf_bytes) > 1000, "PDF should be non-trivial"
        assert pdf_bytes[:5] == b"%PDF-", "Should be valid PDF"

        print("✓ All assertions passed")

    finally:
        # Restore all patches
        for (mod, fn_name), orig in patches.items():
            setattr(mod, fn_name, orig)
        await stub_api.close()


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
