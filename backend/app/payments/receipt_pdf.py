"""The PDF receipt attached to the payment_confirmed email (ARCHITECTURE.md §7.6).

Built from the same invoice the email body renders (app/payments/invoice.py invoice_view()), so
the PDF and the mail can't disagree. Only real data is printed: anything missing is left out,
never shown as "None". The technician and the visit date aren't known when a payment is confirmed,
so they aren't on it.

ReportLab platypus, A4, text kept as text (selectable and searchable). DejaVu Sans is bundled in
app/assets/fonts with its licence, because the PDF base fonts have no rupee sign.
"""

import io
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import Settings
from app.payments.money import IST

ASSETS = Path(__file__).resolve().parents[1] / "assets"
FONT, FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
FOOTER = "This is a computer-generated receipt. No signature is needed."
# The customer's own words can be long; a receipt keeps the gist.
MAX_ISSUE_CHARS = 600

INK = colors.HexColor("#1d1d1f")
MUTED = colors.HexColor("#6e6e73")
HAIRLINE = colors.HexColor("#d2d2d7")
PAID_GREEN = colors.HexColor("#248a3d")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
# SimpleDocTemplate's frame pads its content by 6 pt a side; the page margins allow for it, so text
# and the full-width tables both start at MARGIN.
FRAME_PADDING = 6
CONTENT_W = PAGE_W - 2 * MARGIN

_fonts_lock = threading.Lock()
_fonts_ready = False


def _register_fonts() -> None:
    global _fonts_ready
    with _fonts_lock:
        if _fonts_ready:
            return
        pdfmetrics.registerFont(TTFont(FONT, str(ASSETS / "fonts" / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(ASSETS / "fonts" / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
        _fonts_ready = True


def _style(name: str, **kw: Any) -> ParagraphStyle:
    return ParagraphStyle(name, **{"fontName": FONT, "fontSize": 9.5, "leading": 13, "textColor": INK, **kw})


BODY = _style("body")
BOLD = _style("bold", fontName=FONT_BOLD)
SMALL = _style("small", fontSize=8.5, leading=11.5, textColor=MUTED)
LABEL = _style("label", fontSize=8, leading=11, textColor=MUTED)
COMPANY = _style("company", fontName=FONT_BOLD, fontSize=13, leading=16)
TITLE = _style("title", fontName=FONT_BOLD, fontSize=17, leading=21, alignment=TA_RIGHT)
HEADING = _style("heading", fontName=FONT_BOLD, fontSize=10.5, leading=14, spaceBefore=4)
AMOUNT = _style("amount", alignment=TA_RIGHT)
TOTAL = _style("total", fontName=FONT_BOLD, fontSize=11.5, leading=15)
TOTAL_AMOUNT = _style("total_amount", fontName=FONT_BOLD, fontSize=11.5, leading=15, alignment=TA_RIGHT)
PAID_MARK = _style("paid", fontName=FONT_BOLD, fontSize=11, leading=13, textColor=colors.white, alignment=1)


def build_receipt_pdf(invoice: dict[str, Any], settings: Settings) -> bytes:
    """The receipt for one paid invoice, as PDF bytes. Raises on anything it can't render."""
    _register_fonts()
    buffer = io.BytesIO()
    number = _clean(invoice.get("invoice_number")) or "receipt"
    company = _clean(settings.company_name)
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=MARGIN - FRAME_PADDING, rightMargin=MARGIN - FRAME_PADDING, topMargin=16 * mm, bottomMargin=22 * mm,
        title=f"Receipt {number}", author=company or "", subject=f"Payment receipt {number}",
        creator=company or "")
    story: list[Any] = [_header(settings), Spacer(1, 6 * mm), _summary(invoice), Spacer(1, 5 * mm)]
    story += _parties(invoice)
    story += _section("Device", [
        ("Model", _device_name(invoice.get("device"))),
        ("Serial number", _clean((invoice.get("device") or {}).get("serial_number"))),
        ("Warranty", warranty_text(invoice)),
    ])
    story += _section("Service", [
        ("Service", _clean((invoice.get("service") or {}).get("name"))),
        ("Reason", _clean(invoice.get("problem_summary"))),
        ("Reported issue", _reported_issue(invoice)),
    ])
    story += _line_items(invoice)
    story += _section("Payment", [
        ("Method", "UPI"),
        ("UTR", _clean(invoice.get("utr"))),
        ("Verified", verification_text(invoice)),
    ])
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


# ---------- the parts ----------


def _header(settings: Settings) -> Table:
    lines = [Paragraph(_text(settings.company_name), COMPANY)]
    for value in (settings.company_address, settings.company_phone, settings.company_email):
        if _clean(value):
            lines.append(Paragraph(_text(value), SMALL))
    if _clean(settings.company_gstin):
        lines.append(Paragraph(f"GSTIN {_text(settings.company_gstin)}", SMALL))

    paid = Table([[Paragraph("PAID", PAID_MARK)]], colWidths=[22 * mm], hAlign="RIGHT")
    paid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAID_GREEN),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    title = [Paragraph("Payment receipt", TITLE), Spacer(1, 2 * mm), paid]

    logo = Image(str(ASSETS / "logo.png"), width=14 * mm, height=14 * mm)
    table = Table([[logo, lines, title]], colWidths=[18 * mm, CONTENT_W - 18 * mm - 62 * mm, 62 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _summary(invoice: dict[str, Any]) -> Table:
    """Invoice number, when it was paid, and the ticket, in one band under the header."""
    cells = [(label, value) for label, value in (
        ("Invoice", _clean(invoice.get("invoice_number"))),
        ("Paid on", _clean(invoice.get("paid_display"))),
        ("Ticket", _clean(invoice.get("ticket_number"))),
    ) if value]
    row = [[Paragraph(label, LABEL), Paragraph(_text(value), BOLD)] for label, value in cells]
    table = Table([row], colWidths=[CONTENT_W / max(len(row), 1)] * len(row)) if row else Table([[""]])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f5f7")),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _parties(invoice: dict[str, Any]) -> list[Any]:
    """Billed to, and the service address, side by side (either may be missing)."""
    customer = invoice.get("customer") or {}
    billed = [_clean(customer.get(key)) for key in ("full_name", "email", "phone")]
    columns = []
    if any(billed):
        columns.append([Paragraph("Billed to", HEADING)] + [
            Paragraph(_text(value), BOLD if index == 0 else BODY) for index, value in enumerate(billed) if value])
    address = _clean((invoice.get("service_address") or {}).get("text"))
    if address:
        columns.append([Paragraph("Service address", HEADING), Paragraph(_text(address), BODY)])
    if not columns:
        return []
    width = CONTENT_W / len(columns)
    table = Table([columns], colWidths=[width] * len(columns))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [table, Spacer(1, 4 * mm)]


def _section(title: str, rows: list[tuple[str, str | None]]) -> list[Any]:
    """A heading over label/value rows; rows with no value are dropped, and an empty section too."""
    kept = [(label, value) for label, value in rows if value]
    if not kept:
        return []
    table = Table([[Paragraph(label, LABEL), Paragraph(_text(value), BODY)] for label, value in kept],
                  colWidths=[34 * mm, CONTENT_W - 34 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, HAIRLINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return [Paragraph(title, HEADING), table, Spacer(1, 4 * mm)]


def _line_items(invoice: dict[str, Any]) -> list[Any]:
    rows = [[Paragraph(_text(item["label"]), BODY), Paragraph(_text(item["amount_display"]), AMOUNT)]
            for item in invoice.get("line_items") or []
            if _clean(item.get("label")) and _clean(item.get("amount_display"))]
    total = _clean(invoice.get("total_display"))
    if total:
        rows.append([Paragraph("Total paid", TOTAL), Paragraph(_text(total), TOTAL_AMOUNT)])
    if not rows:
        return []
    table = Table(rows, colWidths=[CONTENT_W - 40 * mm, 40 * mm])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, HAIRLINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if total:
        style += [("LINEABOVE", (0, -1), (-1, -1), 0.75, INK), ("TOPPADDING", (0, -1), (-1, -1), 6)]
    table.setStyle(TableStyle(style))
    return [Paragraph("Charges", HEADING), table, Spacer(1, 4 * mm)]


def _footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setStrokeColor(HAIRLINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 15 * mm, PAGE_W - MARGIN, 15 * mm)
    canvas.setFont(FONT, 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 10.5 * mm, FOOTER)
    canvas.drawRightString(PAGE_W - MARGIN, 10.5 * mm, f"Page {document.page}")
    canvas.restoreState()


# ---------- wording ----------


def warranty_text(invoice: dict[str, Any]) -> str | None:
    """The device's warranty on the day it was paid, or None when its end date isn't known."""
    until = invoice.get("warranty_until")
    if not until:
        return None
    end = date.fromisoformat(str(until)[:10])
    paid_at = invoice.get("paid_at")
    on = datetime.fromisoformat(paid_at).astimezone(IST).date() if paid_at else datetime.now(IST).date()
    shown = f"{end.day} {end:%b %Y}"
    return f"In warranty until {shown}" if end >= on else f"Out of warranty (ended {shown})"


def verification_text(invoice: dict[str, Any]) -> str | None:
    """How the payment was confirmed: the bank's credit alert, or the admin who checked the statement."""
    if invoice.get("verified_by") == "bank_alert":
        return "Matched to the bank's credit alert"
    name = _clean(invoice.get("verified_by_name"))
    return f"Confirmed by {name} (admin)" if name else None


def _device_name(device: dict[str, Any] | None) -> str | None:
    if not device:
        return None
    name, model = _clean(device.get("name")), _clean(device.get("model_number"))
    if name and model and model not in name:
        return f"{name} ({model})"
    return name or model


def _reported_issue(invoice: dict[str, Any]) -> str | None:
    issue = _clean(invoice.get("reported_issue"))
    if not issue or issue == _clean(invoice.get("problem_summary")):
        return None
    return issue if len(issue) <= MAX_ISSUE_CHARS else issue[:MAX_ISSUE_CHARS].rstrip() + "…"


def _clean(value: Any) -> str | None:
    """A printable value, or None for a missing or blank one (so nothing prints "None")."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text(value: Any) -> str:
    """Paragraph markup for plain text: escaped, so a customer's "<" or "&" can't become a tag; line
    breaks kept."""
    return escape(str(value).strip()).replace("\n", "<br/>")