"""The invoice: one view of a payment for the pay page, the emails, and the chat replies (§7.6).

GET /api/pay/{token}, the payment_link and payment_confirmed emails, and the workflow's receipt all
read a payment through `load_invoice`, so the number, the line items, and the total a customer
sees are the same everywhere. The money comes from the payments row (computed in code by the
payments MCP server, §5.5); nothing here recomputes or rounds it.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.payments.money import format_inr, format_ist, format_ist_date, money_str, upi_uri

_INVOICE = text("""
SELECT p.id, p.ticket_id, p.customer_id, p.service_code, p.amount, p.currency, p.line_items,
       p.status, p.public_token, p.invoice_number, p.utr, p.utr_submitted_at, p.utr_attempts,
       p.verified_at, p.verified_by, p.needs_review, p.expires_at, p.paid_at, p.created_at,
       t.ticket_number, t.title AS ticket_title, t.ai_summary, t.description AS ticket_description,
       c.full_name, c.email, c.phone,
       a.line1, a.line2, a.city, a.state, a.postal_code,
       pr.serial_number, pr.warranty_until, pm.model_number, pm.name AS model_name,
       sc.name AS service_name, sc.part_type AS service_part_type, sc.requires_visit AS service_requires_visit,
       v.name AS verified_by_name
FROM payments p
JOIN tickets t              ON t.id = p.ticket_id
JOIN customers c            ON c.id = p.customer_id
LEFT JOIN addresses a       ON a.id = p.service_address_id
LEFT JOIN products pr       ON pr.id = t.product_id
LEFT JOIN product_models pm ON pm.id = pr.model_id
LEFT JOIN service_catalog sc ON sc.code = p.service_code
LEFT JOIN staff_users v     ON CAST(v.id AS text) = p.verified_by
""")


async def load_invoice(
    *, payment_id: uuid.UUID | str | None = None, token: str | None = None,
    session: AsyncSession | None = None, settings: Settings | None = None,
) -> dict[str, Any] | None:
    """The invoice for one payment, by id or by public token. None when there is no such payment."""
    if (payment_id is None) == (token is None):
        raise ValueError("give exactly one of payment_id or token")
    where, params = (" WHERE p.id = :id", {"id": str(payment_id)}) if payment_id is not None \
        else (" WHERE p.public_token = :token", {"token": token})
    query = text(_INVOICE.text + where)
    if session is not None:
        row = (await session.execute(query, params)).mappings().one_or_none()
    else:
        async with SessionLocal() as own:
            row = (await own.execute(query, params)).mappings().one_or_none()
    return invoice_view(dict(row), settings or get_settings()) if row is not None else None


def invoice_view(row: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """A payments row (joined as in _INVOICE) as the invoice every template and the pay page use.

    JSON-safe: money as two-decimal strings plus a display form, times as ISO strings plus an IST
    display form. Every key is always present (None when unknown), because the email templates are
    rendered with StrictUndefined.
    """
    status = row["status"]
    if status == "pending" and row["expires_at"] <= datetime.now(UTC):
        status = "expired"  # what the clock says; the verifier's sweep persists it
    address = None
    if row.get("line1"):
        address = {
            "line1": row["line1"], "line2": row.get("line2"), "city": row["city"],
            "state": row.get("state"), "postal_code": row.get("postal_code"),
        }
        address["text"] = address_text(address)
    device = None
    if row.get("serial_number"):
        device = {"name": row.get("model_name"), "model_number": row.get("model_number"),
                  "serial_number": row["serial_number"]}
    return {
        "payment_id": str(row["id"]),
        "ticket_id": str(row["ticket_id"]),
        "customer_id": str(row["customer_id"]),
        "invoice_number": row["invoice_number"],
        "invoice_date": format_ist_date(row["created_at"]),
        "issued_at": row["created_at"].isoformat(),
        "status": status,
        "ticket_number": row["ticket_number"],
        # The ticket's title, not ai_summary: the summary is written for agents and can carry
        # internal guidance ("verify ... per playbook") that has no place on a customer's invoice.
        "problem_summary": (row.get("ticket_title") or row.get("ai_summary") or "").strip(),
        # What the customer wrote when they raised it (tickets.description); the PDF receipt prints it.
        "reported_issue": (row.get("ticket_description") or "").strip() or None,
        "device": device,
        "warranty_until": row["warranty_until"].isoformat() if row.get("warranty_until") else None,
        "customer": {"full_name": row.get("full_name"), "email": row.get("email"), "phone": row.get("phone")},
        "service_address": address,
        "service": {"code": row["service_code"], "name": row.get("service_name") or row["service_code"],
                    "fulfilment": fulfilment(row)},
        "line_items": [
            {**item, "amount": money_str(item["amount"]), "amount_display": format_inr(item["amount"])}
            for item in (row["line_items"] or [])
        ],
        "total": money_str(row["amount"]),
        "total_display": format_inr(row["amount"]),
        "currency": row["currency"],
        "expires_at": row["expires_at"].isoformat(),
        "expires_display": format_ist(row["expires_at"]),
        "pay_url": pay_url(row["public_token"], settings),
        "utr": row.get("utr"),
        "utr_submitted_at": _iso(row.get("utr_submitted_at")),
        "utr_attempts": row.get("utr_attempts") or 0,
        "paid_at": _iso(row.get("paid_at")),
        "paid_display": format_ist(row.get("paid_at")),
        "verified_by": row.get("verified_by"),
        "verified_by_name": row.get("verified_by_name"),  # the admin who confirmed it; None for a bank alert
        "needs_review": bool(row.get("needs_review")),
        "upi_id": settings.upi_id.strip() or None,
        "payee_name": settings.upi_payee_name.strip() or None,
    }


def pay_url(token: str, settings: Settings | None = None) -> str:
    """{FRONTEND_URL}/pay/{token}: the page with the QR and the UTR form (§7.6, §11.2)."""
    return f"{(settings or get_settings()).frontend_url.rstrip('/')}/pay/{token}"


def upi_link(invoice: dict[str, Any], settings: Settings | None = None) -> str | None:
    """The upi://pay link for this invoice, or None when UPI_ID / UPI_PAYEE_NAME aren't set."""
    settings = settings or get_settings()
    if not settings.upi_configured:
        return None
    return upi_uri(settings.upi_id, settings.upi_payee_name, invoice["total"], invoice["ticket_number"])


def address_text(address: dict[str, Any]) -> str:
    """"Flat 12B, Lake View Apartments, Koramangala, Bengaluru, Karnataka 560034"."""
    place = " ".join(part for part in (address.get("state"), address.get("postal_code")) if part)
    parts = [address.get("line1"), address.get("line2"), address.get("city"), place]
    return ", ".join(part.strip() for part in parts if part and part.strip())


def fulfilment(row: dict[str, Any]) -> str:
    """How the service reaches the customer after payment: "visit", "shipped" (a part, no visit), or "none"."""
    if "service_requires_visit" not in row or row["service_requires_visit"]:
        return "visit"
    return "shipped" if row.get("service_part_type") else "none"


def maps_links(address: dict[str, Any]) -> list[dict[str, str]]:
    """Where the address is (§7.7): the customer's own maps link if they pasted one, otherwise Google
    Maps and Apple Maps searches for the address text. No geocoding."""
    if address.get("location_url"):
        return [{"label": "Customer's map link", "url": address["location_url"]}]
    query = quote_plus(address_text(address))
    return [{"label": "Google Maps", "url": f"https://www.google.com/maps/search/?api=1&query={query}"},
            {"label": "Apple Maps", "url": f"https://maps.apple.com/?q={query}"}]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
