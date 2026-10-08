"""payments MCP server (ARCHITECTURE.md §5.5) — :8105.

UPI QR + UTR, verified against the bank's credit SMS (§7.6). This server owns the payment record:
it creates the invoice and its link, takes the customer's UTR, cancels, and records an admin's
manual confirmation. It never decides on its own that money arrived. The automatic path to `paid`
is the bank-alert match in app/payments/upi_verifier.py; the manual one is mark_paid_manually,
which needs an admin's id and a note.

The amount is computed here, in code, from service_catalog's labour fee plus the price of the part
compatible with the ticket's device. No tool takes an amount, so neither a model nor a customer
can set one. Amounts leave this server as two-decimal strings ("6199.00"), never as floats.

Every state change runs in one transaction with the payment (or ticket) row locked, so two calls
racing on one payment can't both win.
"""

import logging
import secrets
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
from mcp.server.mcpserver import MCPServer

from app.core.config import get_settings
from app.payments.money import (
    MAX_UTR_ATTEMPTS,
    PAYMENT_CURRENCY,
    PAYMENT_PROVIDER,
    PUBLIC_TOKEN,
    UTR,
    free_under_warranty,
    money_str,
)
from mcp_servers import run
from mcp_servers.common import db, events

log = logging.getLogger(__name__)
mcp = MCPServer(name="payments", instructions=__doc__)

# Open = a customer can still pay it or is waiting on its verification. One per ticket.
OPEN = "(status = 'verifying' OR (status = 'pending' AND expires_at > now()))"

_PAYMENT = """
SELECT p.id, p.ticket_id, p.customer_id, p.service_code, p.amount, p.currency, p.line_items,
       p.status, p.provider, p.public_token, p.invoice_number, p.utr, p.utr_submitted_at,
       p.utr_attempts, p.verified_at, p.verified_by, p.needs_review, p.service_address_id,
       p.expires_at, p.paid_at, p.created_at, p.expires_at <= now() AS past_expiry,
       t.ticket_number
FROM payments p JOIN tickets t ON t.id = p.ticket_id
"""


def _pay_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/pay/{token}"


def _out(row: asyncpg.Record) -> dict[str, Any]:
    """A payment as the tools return it. Money as strings; the status as a customer would see it."""
    status = row["status"]
    if status == "pending" and row["past_expiry"]:
        status = "expired"
    return {
        "payment_id": str(row["id"]),
        "ticket_id": str(row["ticket_id"]),
        "ticket_number": row["ticket_number"],
        "customer_id": str(row["customer_id"]),
        "invoice_number": row["invoice_number"],
        "service_code": row["service_code"],
        "amount": money_str(row["amount"]),
        "currency": row["currency"],
        "line_items": row["line_items"],
        "status": status,
        "provider": row["provider"],
        "pay_url": _pay_url(row["public_token"]),
        "utr": row["utr"],
        "utr_submitted_at": _iso(row["utr_submitted_at"]),
        "utr_attempts": row["utr_attempts"],
        "utr_attempts_left": max(0, MAX_UTR_ATTEMPTS - row["utr_attempts"]),
        "verified_at": _iso(row["verified_at"]),
        "verified_by": row["verified_by"],
        "needs_review": row["needs_review"],
        "service_address_id": str(row["service_address_id"]) if row["service_address_id"] else None,
        "expires_at": _iso(row["expires_at"]),
        "paid_at": _iso(row["paid_at"]),
        "created_at": _iso(row["created_at"]),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _uuid(value: str | None) -> str | None:
    """The id as a canonical UUID string, or None when it isn't one (asyncpg would raise instead)."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _refused(error: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, **extra}


async def _ticket_event(conn: asyncpg.Connection, ticket_id: Any, type: str, actor: str,
                        payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1,$2,$3,$4)",
        ticket_id, type, payload, actor,
    )


def _note(note: str | None) -> str | None:
    """A staff note with its whitespace collapsed, or None when it is too short to say anything."""
    text = " ".join((note or "").split())[:500]
    return text if len(text) >= 3 else None


async def _admin(staff_user_id: str | None, note: str | None) -> tuple[asyncpg.Record | None, str, dict[str, Any] | None]:
    """(the admin, their note, None), or (None, "", the refusal). Every manual write on the payments page
    (§11.2) is an admin's, with a note saying what they checked; the timeline names them as the actor."""
    sid = _uuid(staff_user_id)
    if sid is None:
        return None, "", _refused("invalid_id", "staff_user_id must be a UUID")
    text = _note(note)
    if text is None:
        return None, "", _refused("note_required", "say what was checked, e.g. 'UTR seen on the bank statement'")
    staff = await db.fetchrow("SELECT id, name, role FROM staff_users WHERE id = $1", sid)
    if staff is None:
        return None, "", _refused("staff_not_found", "no such staff user")
    if staff["role"] != "admin":
        return None, "", _refused("not_admin", "only an admin can change a payment by hand")
    return staff, text, None


def _by(staff: asyncpg.Record | None, note: str) -> dict[str, Any]:
    """What a timeline event says about the admin who acted, if one did."""
    return {"staff_id": str(staff["id"]), "staff_name": staff["name"], "note": note} if staff else {}


async def _expire(conn: asyncpg.Connection, row: asyncpg.Record) -> None:
    """Persist what the clock already says: a pending link past expires_at is expired."""
    await conn.execute("UPDATE payments SET status = 'expired' WHERE id = $1 AND status = 'pending'", row["id"])
    await _ticket_event(conn, row["ticket_id"], "payment_expired", "system",
                        {"payment_id": str(row["id"]), "invoice_number": row["invoice_number"]})


# ---------- §5.5 tools ----------


@mcp.tool()
async def create_payment_request(ticket_id: str, customer_id: str, service_code: str, address_id: str,
                                 staff_user_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Create the invoice and its tokenized UPI payment link for one service on a ticket (§7.6).

    The amount is computed here from service_catalog's labour fee plus the price of the part
    compatible with the ticket's device; it is never an argument. Creates invoice_number
    (INV-YYYY-NNNNN), public_token and expires_at (PAYMENT_LINK_TTL_MINUTES), sets the ticket to
    awaiting_payment and emits payment.link_sent. A ticket with an open payment is refused, and so is
    a covered repair on a device in warranty, which is free (app/payments/money.py).

    From the payments page, an admin's staff_user_id and note (what they checked) are given too, and
    the timeline names that admin as the actor; the /payments pipeline gives neither.
    """
    settings = get_settings()
    ids = {name: _uuid(value) for name, value in
           (("ticket_id", ticket_id), ("customer_id", customer_id), ("address_id", address_id))}
    bad = [name for name, value in ids.items() if value is None]
    if bad:
        return _refused("invalid_id", f"not a UUID: {', '.join(bad)}")
    staff, note_text, refusal = (None, "", None)
    if staff_user_id is not None:
        staff, note_text, refusal = await _admin(staff_user_id, note)
        if refusal:
            return refusal
    actor = str(staff["id"]) if staff else "system"

    async with db.acquire() as conn:
        async with conn.transaction():
            # Locking the ticket serialises two requests for it, so it can't end up with two open links.
            ticket = await conn.fetchrow(
                "SELECT t.id, t.ticket_number, t.customer_id, t.status, t.priority, p.model_id,"
                " COALESCE(p.warranty_until >= CAST(now() AT TIME ZONE 'Asia/Kolkata' AS date), FALSE)"
                "   AS in_warranty"
                " FROM tickets t LEFT JOIN products p ON p.id = t.product_id"
                " WHERE t.id = $1 FOR UPDATE OF t", ids["ticket_id"])
            if ticket is None:
                return _refused("ticket_not_found", "no such ticket")
            if str(ticket["customer_id"]) != ids["customer_id"]:
                return _refused("customer_mismatch", "the ticket belongs to a different customer")
            if ticket["status"] in ("resolved", "closed"):
                return _refused("ticket_closed", f"the ticket is {ticket['status']}")
            address = await conn.fetchrow(
                "SELECT id FROM addresses WHERE id = $1 AND customer_id = $2", ids["address_id"], ids["customer_id"])
            if address is None:
                return _refused("address_not_found", "no such address for this customer")
            open_payment = await conn.fetchrow(
                f"SELECT id, invoice_number, status FROM payments WHERE ticket_id = $1 AND {OPEN} LIMIT 1",
                ids["ticket_id"])
            if open_payment is not None:
                return _refused("open_payment_exists", "the ticket already has an open payment; cancel it first",
                                payment_id=str(open_payment["id"]), invoice_number=open_payment["invoice_number"],
                                status=open_payment["status"])

            service = await conn.fetchrow(
                "SELECT code, name, part_type, labour_fee FROM service_catalog WHERE code = upper($1)",
                service_code.strip())
            if service is None:
                return _refused("unknown_service", f"no service {service_code!r} in the catalog")
            if free_under_warranty(service["code"], ticket["in_warranty"]):
                return _refused("free_under_warranty",
                                f"the device is in warranty, so {service['name'].lower()} is free; nothing to bill")

            line_items: list[dict[str, Any]] = []
            if service["part_type"]:
                if ticket["model_id"] is None:
                    return _refused("no_device", "the ticket has no verified device, so its part can't be priced")
                part = await conn.fetchrow(
                    "SELECT p.id, p.sku, p.name, p.unit_price FROM parts p"
                    " JOIN part_compatibility pc ON pc.part_id = p.id"
                    " WHERE pc.model_id = $1 AND p.part_type = $2 ORDER BY p.unit_price, p.sku LIMIT 1",
                    ticket["model_id"], service["part_type"])
                if part is None:
                    return _refused("no_compatible_part",
                                    f"no {service['part_type']} part is compatible with this device")
                line_items.append({"kind": "part", "part_id": str(part["id"]), "sku": part["sku"],
                                   "label": f"{part['name']} ({part['sku']})", "amount": money_str(part["unit_price"])})
            line_items.append({"kind": "labour", "label": f"Labour: {service['name']}",
                               "amount": money_str(service["labour_fee"])})
            amount = sum((Decimal(item["amount"]) for item in line_items), Decimal("0.00"))
            if amount <= 0:
                return _refused("zero_amount", "this service costs nothing, so there is nothing to pay")

            # INV-YYYY-NNNNN, numbered per Indian calendar year. The advisory lock makes the
            # read-then-insert safe against a concurrent request; UNIQUE is the backstop.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('payments.invoice_number'))")
            year = await conn.fetchval("SELECT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY')")
            last = await conn.fetchval(
                "SELECT COALESCE(MAX(CAST(split_part(invoice_number, '-', 3) AS int)), 0) FROM payments"
                " WHERE invoice_number LIKE 'INV-' || $1 || '-%'", year)
            invoice_number = f"INV-{year}-{last + 1:05d}"

            row = await conn.fetchrow(
                "INSERT INTO payments (ticket_id, customer_id, service_code, amount, currency, line_items,"
                " status, provider, public_token, invoice_number, service_address_id, expires_at)"
                " VALUES ($1,$2,$3,$4,$5,$6,'pending',$7,$8,$9,$10, now() + make_interval(mins => $11))"
                " RETURNING id",
                ids["ticket_id"], ids["customer_id"], service["code"], amount, PAYMENT_CURRENCY,
                line_items, PAYMENT_PROVIDER, secrets.token_urlsafe(24), invoice_number,
                ids["address_id"], settings.payment_link_ttl_minutes,
            )
            await conn.execute("UPDATE tickets SET status = 'awaiting_payment', updated_at = now() WHERE id = $1",
                               ids["ticket_id"])
            await _ticket_event(conn, ids["ticket_id"], "payment_link_created", actor, {
                "payment_id": str(row["id"]), "invoice_number": invoice_number,
                "service_code": service["code"], "amount": money_str(amount), "currency": PAYMENT_CURRENCY,
                **_by(staff, note_text)})
            await _ticket_event(conn, ids["ticket_id"], "status_changed", actor, {
                "status": "awaiting_payment", "note": f"Payment link {invoice_number} created"
                + (f" by {staff['name']}" if staff else "")})
            payment = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", row["id"])

    out = _out(payment)
    await events.publish("payment.link_sent", {
        "payment_id": out["payment_id"], "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "invoice_number": invoice_number, "amount": out["amount"], "currency": out["currency"],
        "expires_at": out["expires_at"],
    })
    await events.publish("ticket.updated", {
        "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "status": "awaiting_payment", "priority": ticket["priority"], "note": f"Payment link {invoice_number} created",
    })
    return {"ok": True, **out, "public_token": payment["public_token"], "service_name": service["name"]}


@mcp.tool()
async def get_payment_status(payment_id: str) -> dict[str, Any]:
    """A payment's status, invoice, amount, UTR, verification and needs_review flag (§5.5)."""
    pid = _uuid(payment_id)
    if pid is None:
        return {"found": False, "error": "invalid_id"}
    row = await db.fetchrow(_PAYMENT + " WHERE p.id = $1", pid)
    if row is None:
        return {"found": False, "payment_id": payment_id}
    return {"found": True, **_out(row)}


@mcp.tool()
async def cancel_payment(payment_id: str, staff_user_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Cancel a payment that isn't paid, which expires its link (§5.5).

    A paid payment is refused (that's a refund, not a cancellation). Cancelling a payment whose UTR
    is still being verified is allowed, for staff who know it will never arrive, and the result
    warns that money may already be on its way. From the payments page, the admin's staff_user_id and
    note are given and the timeline names them; a financial record is cancelled, never deleted.
    """
    pid = _uuid(payment_id)
    if pid is None:
        return _refused("invalid_id", "payment_id is not a UUID")
    staff, note_text, refusal = (None, "", None)
    if staff_user_id is not None:
        staff, note_text, refusal = await _admin(staff_user_id, note)
        if refusal:
            return refusal
    actor = str(staff["id"]) if staff else "system"
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1 FOR UPDATE OF p", pid)
            if row is None:
                return _refused("not_found", "no such payment")
            if row["status"] in ("paid", "refunded"):
                return _refused(row["status"], f"the payment is {row['status']}; it can't be cancelled")
            if row["status"] in ("cancelled", "expired"):
                return {"ok": True, "already_closed": True, **_out(row)}
            had_utr = row["status"] == "verifying"
            await conn.execute("UPDATE payments SET status = 'cancelled', needs_review = FALSE WHERE id = $1", pid)
            await _ticket_event(conn, row["ticket_id"], "payment_cancelled", actor,
                                {"payment_id": pid, "invoice_number": row["invoice_number"], "utr": row["utr"],
                                 **_by(staff, note_text)})
            ticket = await conn.fetchrow(
                "UPDATE tickets SET status = 'in_progress', updated_at = now()"
                " WHERE id = $1 AND status = 'awaiting_payment' RETURNING ticket_number, status, priority",
                row["ticket_id"])
            if ticket is not None:
                await _ticket_event(conn, row["ticket_id"], "status_changed", actor, {
                    "status": "in_progress", "note": f"Payment {row['invoice_number']} cancelled"
                    + (f" by {staff['name']}" if staff else "")})
            cancelled = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", pid)
    await events.publish("ticket.updated", {
        "ticket_id": str(row["ticket_id"]), "ticket_number": row["ticket_number"],
        "status": ticket["status"] if ticket else None, "note": f"Payment {row['invoice_number']} cancelled",
    })
    out = {"ok": True, **_out(cancelled)}
    if had_utr:
        out["warning"] = (f"UTR {row['utr']} was submitted and not yet verified; check the bank statement "
                          "before billing this customer again")
    return out


@mcp.tool()
async def submit_utr(token: str, utr: str) -> dict[str, Any]:
    """The customer's 12-digit UTR from the pay page (§7.6). Sets the payment to verifying.

    Refused: a malformed UTR, an expired, paid, cancelled or refunded payment, a UTR already used by
    another payment, and more than MAX_UTR_ATTEMPTS submissions. Emits payment.utr_submitted.
    Matching against the bank alert happens afterwards, in the app (app/payments/upi_verifier.py).
    A different UTR while verifying (or failed) replaces the one held: a corrected typo.
    """
    if not PUBLIC_TOKEN.fullmatch(token or ""):
        if not UTR.fullmatch((utr or "").strip()):
            return _refused("invalid_utr", "Enter the 12-digit UTR (UPI reference number) from your payment app.")
        return _refused("not_found", "This payment link is not valid.")
    return await _take_utr("p.public_token = $1", token, utr, actor="customer")


@mcp.tool()
async def correct_utr_manually(payment_id: str, staff_user_id: str, utr: str, note: str) -> dict[str, Any]:
    """An admin enters or corrects the UTR on the customer's behalf (the payments page, §11.2).

    Exactly as if the customer sent it from the pay page (submit_utr): the same checks, and it counts
    against the same MAX_UTR_ATTEMPTS. The timeline names the admin and their note. Never offered to a
    model (router.MODEL_FORBIDDEN_TOOLS).
    """
    pid = _uuid(payment_id)
    if pid is None:
        return _refused("invalid_id", "payment_id must be a UUID")
    staff, note_text, refusal = await _admin(staff_user_id, note)
    if refusal:
        return refusal
    return await _take_utr("p.id = $1", pid, utr, actor=str(staff["id"]), by=_by(staff, note_text))


async def _take_utr(where: str, key: str, utr: str, *, actor: str, by: dict[str, Any] | None = None) -> dict[str, Any]:
    """One UTR attempt on one payment, from the pay page or an admin: the one code path for both."""
    utr = (utr or "").strip()
    if not UTR.fullmatch(utr):
        return _refused("invalid_utr", "Enter the 12-digit UTR (UPI reference number) from your payment app.")

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_PAYMENT + f" WHERE {where} FOR UPDATE OF p", key)
            if row is None:
                return _refused("not_found", "This payment link is not valid.")
            status = row["status"]
            if status == "paid":
                return _refused("already_paid", "This invoice is already paid.", **_out(row))
            if status in ("cancelled", "refunded"):
                return _refused(status, f"This invoice was {status}.", **_out(row))
            if status == "expired" or (status == "pending" and row["past_expiry"]):
                if status == "pending":
                    await _expire(conn, row)
                return _refused("expired", "This payment link has expired. Please ask us for a new one.")
            if status == "verifying" and row["utr"] == utr:
                return {"ok": True, "already_submitted": True, **_out(row)}
            if row["utr_attempts"] >= MAX_UTR_ATTEMPTS:
                return _refused("too_many_attempts",
                                "Too many UTR attempts on this link. We'll check your payment and get back to you.")

            # Every well-formed attempt counts, refused ones included, so a link can't probe UTRs.
            attempt = row["utr_attempts"] + 1
            await conn.execute("UPDATE payments SET utr_attempts = $2 WHERE id = $1", row["id"], attempt)
            used = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM payments WHERE utr = $1 AND id <> $2)"
                " OR EXISTS (SELECT 1 FROM bank_alerts WHERE utr = $1 AND matched_payment_id IS NOT NULL"
                "            AND matched_payment_id <> $2)", utr, row["id"])
            if used:
                return _refused("utr_used", "This UTR has already been used.",
                                utr_attempts_left=max(0, MAX_UTR_ATTEMPTS - attempt))
            try:
                async with conn.transaction():  # a savepoint: a lost race on UNIQUE(utr) keeps the attempt count
                    await conn.execute(
                        "UPDATE payments SET utr = $2, utr_submitted_at = now(), status = 'verifying',"
                        " needs_review = FALSE WHERE id = $1", row["id"], utr)
            except asyncpg.UniqueViolationError:
                return _refused("utr_used", "This UTR has already been used.",
                                utr_attempts_left=max(0, MAX_UTR_ATTEMPTS - attempt))
            # A UTR that replaces an earlier one is a correction (a typo fixed on the pay page, or by an
            # admin): staff see which attempt it was, what it replaced, and who entered it.
            previous = row["utr"]
            note = f"UTR {utr}, attempt {attempt} of {MAX_UTR_ATTEMPTS}" + (
                f", corrected from {previous}" if previous else "")
            if by:
                note += f", entered by {by['staff_name']}: {by['note']}"
            await _ticket_event(conn, row["ticket_id"], "payment_utr_submitted", actor, {
                "payment_id": str(row["id"]), "invoice_number": row["invoice_number"], "utr": utr, "attempt": attempt,
                "corrected": previous is not None, "previous_utr": previous, **(by or {}), "note": note})
            submitted = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", row["id"])

    out = _out(submitted)
    await events.publish("payment.utr_submitted", {
        "payment_id": out["payment_id"], "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "invoice_number": out["invoice_number"], "attempt": attempt, "corrected": previous is not None,
    })
    return {"ok": True, **out, "corrected": previous is not None}


@mcp.tool()
async def mark_paid_manually(payment_id: str, staff_user_id: str, note: str) -> dict[str, Any]:
    """Admin backup when a bank alert never arrives (§7.6): mark a payment paid, logged with the admin's id.

    Only an admin may, with a note saying what they checked (e.g. the bank statement). Never
    offered to a model (router.MODEL_FORBIDDEN_TOOLS). Emits payment.paid.
    """
    pid, sid = _uuid(payment_id), _uuid(staff_user_id)
    if pid is None or sid is None:
        return _refused("invalid_id", "payment_id and staff_user_id must be UUIDs")
    note = " ".join((note or "").split())
    if len(note) < 3:
        return _refused("note_required", "say what was checked, e.g. 'UTR seen on the bank statement'")
    staff = await db.fetchrow("SELECT id, name, role FROM staff_users WHERE id = $1", sid)
    if staff is None:
        return _refused("staff_not_found", "no such staff user")
    if staff["role"] != "admin":
        return _refused("not_admin", "only an admin can mark a payment paid")

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1 FOR UPDATE OF p", pid)
            if row is None:
                return _refused("not_found", "no such payment")
            if row["status"] == "paid":
                return {"ok": True, "already_paid": True, **_out(row)}
            if row["status"] in ("cancelled", "refunded"):
                return _refused(row["status"], f"the payment is {row['status']}")
            await conn.execute(
                "UPDATE payments SET status = 'paid', paid_at = now(), verified_at = now(), verified_by = $2,"
                " needs_review = FALSE WHERE id = $1", pid, sid)
            await _ticket_event(conn, row["ticket_id"], "payment_paid", sid, {
                "payment_id": pid, "invoice_number": row["invoice_number"], "amount": money_str(row["amount"]),
                "utr": row["utr"], "verified_by": sid, "method": "manual", "note": note, "staff_name": staff["name"]})
            paid = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", pid)

    out = _out(paid)
    log.info("payment %s marked paid by admin %s", out["invoice_number"], sid)
    await events.publish("payment.paid", {
        "payment_id": out["payment_id"], "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "invoice_number": out["invoice_number"], "amount": out["amount"], "currency": out["currency"],
        "utr": out["utr"], "verified_by": sid, "method": "manual",
    })
    return {"ok": True, **out}


@mcp.tool()
async def reject_payment(payment_id: str, staff_user_id: str, reason: str) -> dict[str, Any]:
    """An admin rejects a verifying payment after checking the bank statement (the payments page, §11.2).

    It becomes failed with the admin's reason on the timeline; the customer may send another UTR while
    attempts remain. Only a verifying payment can be rejected. Emits payment.failed. Never offered to
    a model (router.MODEL_FORBIDDEN_TOOLS).
    """
    pid = _uuid(payment_id)
    if pid is None:
        return _refused("invalid_id", "payment_id must be a UUID")
    staff, reason_text, refusal = await _admin(staff_user_id, reason)
    if refusal:
        return refusal
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1 FOR UPDATE OF p", pid)
            if row is None:
                return _refused("not_found", "no such payment")
            if row["status"] != "verifying":
                current = _out(row)["status"]
                return _refused("already_paid" if current == "paid" else "not_verifying",
                                f"the payment is {current}; only a verifying payment can be rejected", status=current)
            await conn.execute("UPDATE payments SET status = 'failed', needs_review = FALSE WHERE id = $1", pid)
            await _ticket_event(conn, row["ticket_id"], "payment_failed", str(staff["id"]), {
                "payment_id": pid, "invoice_number": row["invoice_number"], "utr": row["utr"], "method": "manual",
                "reason": f"rejected by {staff['name']}: {reason_text}", **_by(staff, reason_text)})
            failed = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", pid)
    out = _out(failed)
    await events.publish("payment.failed", {
        "payment_id": pid, "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "invoice_number": out["invoice_number"], "utr": out["utr"], "reason": reason_text, "method": "manual"})
    await events.publish("ticket.updated", {"ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
                                            "note": f"Payment {out['invoice_number']} rejected"})
    return {"ok": True, **out}


# How far an admin may push a link's expiry, in minutes: at least five, at most a week.
EXTEND_MINUTES = (5, 7 * 24 * 60)


@mcp.tool()
async def extend_payment(payment_id: str, staff_user_id: str, expires_in_minutes: int, note: str) -> dict[str, Any]:
    """An admin gives an unpaid link more time (the payments page, §11.2): expires_at = now + the minutes.

    A pending link, or one that expired, becomes pending again. An expired one is refused when the
    ticket has another open payment, and its ticket waits on payment again. A verifying, failed, paid
    or cancelled payment has no link to extend. Never offered to a model (router.MODEL_FORBIDDEN_TOOLS).
    """
    pid = _uuid(payment_id)
    if pid is None:
        return _refused("invalid_id", "payment_id must be a UUID")
    staff, note_text, refusal = await _admin(staff_user_id, note)
    if refusal:
        return refusal
    low, high = EXTEND_MINUTES
    if not isinstance(expires_in_minutes, int) or not low <= expires_in_minutes <= high:
        return _refused("invalid_minutes", f"expires_in_minutes must be between {low} and {high}")
    actor = str(staff["id"])
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1 FOR UPDATE OF p", pid)
            if row is None:
                return _refused("not_found", "no such payment")
            current = _out(row)["status"]
            if current not in ("pending", "expired"):
                return _refused("already_paid" if current == "paid" else "not_extendable",
                                f"the payment is {current}; only an unpaid link can be extended", status=current)
            ticket = await conn.fetchrow("SELECT status, priority FROM tickets WHERE id = $1 FOR UPDATE",
                                         row["ticket_id"])
            if current == "expired":
                if ticket["status"] in ("resolved", "closed"):
                    return _refused("ticket_closed", f"the ticket is {ticket['status']}")
                other = await conn.fetchrow(
                    f"SELECT invoice_number FROM payments WHERE ticket_id = $1 AND id <> $2 AND {OPEN} LIMIT 1",
                    row["ticket_id"], pid)
                if other is not None:
                    return _refused("open_payment_exists",
                                    f"the ticket already has an open payment {other['invoice_number']}")
            await conn.execute(
                "UPDATE payments SET status = 'pending', expires_at = now() + make_interval(mins => $2) WHERE id = $1",
                pid, expires_in_minutes)
            await _ticket_event(conn, row["ticket_id"], "payment_link_extended", actor, {
                "payment_id": pid, "invoice_number": row["invoice_number"], "previous_status": current,
                "minutes": expires_in_minutes, **_by(staff, note_text)})
            moved = None
            if current == "expired" and ticket["status"] != "awaiting_payment":
                moved = await conn.fetchval(
                    "UPDATE tickets SET status = 'awaiting_payment', updated_at = now() WHERE id = $1 RETURNING status",
                    row["ticket_id"])
                await _ticket_event(conn, row["ticket_id"], "status_changed", actor, {
                    "status": "awaiting_payment",
                    "note": f"Payment link {row['invoice_number']} reopened by {staff['name']}"})
            extended = await conn.fetchrow(_PAYMENT + " WHERE p.id = $1", pid)
    out = _out(extended)
    await events.publish("ticket.updated", {
        "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"], "status": moved,
        "priority": ticket["priority"], "note": f"Payment link {out['invoice_number']} extended"})
    return {"ok": True, **out}


PAYMENT_STATUSES = ("pending", "verifying", "paid", "failed", "expired", "cancelled", "refunded")
LIST_LIMIT = 200

_LIST = """
SELECT p.id, p.ticket_id, p.customer_id, p.service_code, p.amount, p.currency, p.line_items,
       p.status, p.provider, p.public_token, p.invoice_number, p.utr, p.utr_submitted_at,
       p.utr_attempts, p.verified_at, p.verified_by, p.needs_review, p.service_address_id,
       p.expires_at, p.paid_at, p.created_at, p.expires_at <= now() AS past_expiry,
       t.ticket_number, c.full_name AS customer_name, c.email AS customer_email, s.name AS service_name,
       v.name AS verified_by_name, count(*) OVER () AS total
FROM payments p
JOIN tickets t               ON t.id = p.ticket_id
JOIN customers c             ON c.id = p.customer_id
LEFT JOIN service_catalog s  ON s.code = p.service_code
LEFT JOIN staff_users v      ON CAST(v.id AS text) = p.verified_by
WHERE ($1::text IS NULL
       OR ($1 = 'expired' AND (p.status = 'expired' OR (p.status = 'pending' AND p.expires_at <= now())))
       OR ($1 = 'pending' AND p.status = 'pending' AND p.expires_at > now())
       OR ($1 NOT IN ('expired', 'pending') AND p.status = $1))
  AND ($2::boolean IS NULL OR p.needs_review = $2)
  AND ($3::text IS NULL OR p.invoice_number ILIKE $3 OR p.utr ILIKE $3 OR t.ticket_number ILIKE $3
       OR c.full_name ILIKE $3 OR c.email ILIKE $3)
  AND ($6::uuid IS NULL OR p.id = $6)
ORDER BY p.created_at DESC
LIMIT $4 OFFSET $5
"""


@mcp.tool()
async def list_payments(status: str | None = None, needs_review: bool | None = None, query: str | None = None,
                        limit: int = 50, offset: int = 0, payment_id: str | None = None) -> dict[str, Any]:
    """Every payment, newest first, for the payments page (§11.2): filter by status (a pending link past its
    expiry counts as expired), needs_review, and text (invoice, UTR, ticket number, customer name or email);
    payment_id gives that one payment's row. Read-only, but staff-only: never offered to a model
    (router.MODEL_FORBIDDEN_TOOLS)."""
    if status is not None and status not in PAYMENT_STATUSES:
        return _refused("bad_status", f"status must be one of {', '.join(PAYMENT_STATUSES)}")
    pid = _uuid(payment_id) if payment_id is not None else None
    if payment_id is not None and pid is None:
        return _refused("invalid_id", "payment_id must be a UUID")
    text = " ".join((query or "").split())[:100]
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if text else None
    limit = max(1, min(int(limit or 50), LIST_LIMIT))
    rows = await db.fetch(_LIST, status, needs_review, pattern, limit, max(0, int(offset or 0)), pid)
    payments = [{**_out(row), "customer_name": row["customer_name"], "customer_email": row["customer_email"],
                 "service_name": row["service_name"], "verified_by_name": row["verified_by_name"]} for row in rows]
    return {"ok": True, "total": rows[0]["total"] if rows else 0, "limit": limit, "offset": max(0, int(offset or 0)),
            "payments": payments}


if __name__ == "__main__":
    run(mcp, "payments")