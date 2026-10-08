"""Event-driven automations (ARCHITECTURE.md §4.2): fixed chains of MCP tool calls, no model choices.

    payment.paid
      -> the payment re-read from the database: nothing happens unless it really is paid
      -> the receipt, claimed once (a payment_confirmed timeline event under an advisory lock):
         messaging.send_email payment_confirmed.html, messaging.send_reply, tickets.update_status(in_progress)
      -> the booking (book() below), claimed once per payment (a job_requested timeline event)

    a free warranty repair, once the customer's details are in (commands.finish_payment_details)
      -> the booking, claimed once per /payments request, authorized by the staff member who ran
         /payments (never by customer text), with no invoice

    book()
      visit    inventory.find_compatible_part -> inventory.reserve_part (the central warehouse)
               -> dispatch.find_technician (the next working day, Mon-Sat IST, up to 6 working days)
               -> dispatch.create_job -> messaging.send_email job_assigned (the technician)
               -> messaging.notify_staff (the technician) -> messaging.send_email visit_scheduled
               -> messaging.send_reply -> tickets.update_status(scheduled)
      shipped  inventory.find_compatible_part -> inventory.reserve_part -> messaging.notify_staff(admin:
               ship it) -> messaging.send_reply "on its way" -> tickets.update_status(in_progress)
      neither  (UPI_TEST) nothing after the receipt
      no stock, or no technician in the customer's city within 6 working days: no job.
               inventory.release_part (anything reserved) -> messaging.notify_staff(admin, the reason)
               -> messaging.send_reply "an agent will confirm the visit date" -> ticket in_progress.
               A job never goes out without its part, and a part is never left reserved without its job.

    job.status_changed (dispatch.update_job_status, from the job API)
      en_route   -> messaging.send_reply "on the way"
      cancelled  -> inventory.release_part -> tickets.update_status(in_progress) -> messaging.notify_staff(admin)
    job.rejected (dispatch.reject_job, from the job API: the assigned technician can't take it)
      -> claimed once per job -> the part stays reserved -> dispatch.find_technician without the
         rejecters, from the job's date (or the next working day) for BOOKING_DAYS working days
         -> dispatch.create_job -> job_assigned email + notify_staff (the new technician)
         -> messaging.send_reply (who is coming, when) -> tickets.update_status(scheduled, the note)
         -> messaging.notify_staff(admin, job_rejected)
      nobody has room -> inventory.release_part -> tickets.update_status(in_progress)
         -> messaging.send_reply "an agent will confirm" -> messaging.notify_staff(admin, job_rejected)
    job.completed
      -> inventory.consume_part -> (restock due) inventory.create_restock_request
         -> messaging.send_email restock_alert (WAREHOUSE_ALERT_EMAIL) + messaging.notify_staff(admin)
      -> tickets.update_status(resolved) -> messaging.send_reply (the closing message)

    ticket.updated, status resolved (tickets.update_status from anywhere, or the ticket page)  §7.9
      -> the ticket re-read: resolved, and resolved in the last RESOLVED_RECENTLY
      -> claimed once per resolution (a resolution_notified timeline event)
      -> at the same time: messaging.send_email ticket_resolved (the customer's address) and
         messaging.send_reply on their own channel, unless the caller already sent its closing
         message (customer_told: /close, a completed job)
    a ticket deleted (DELETE /api/tickets/{id}, after the delete commits)  §7.9
      -> at the same time: messaging.send_email ticket_deleted and messaging.send_reply on their channel
    Neither emails an automated address, nor sends a second email to a customer whose channel is email.

Every call goes through an allowlist gate (role automation, §4.1), publishes agent.tool_called for
the Agent Activity rail, and each run writes one ai_runs row (§4.3 step 4). Every customer text is a
fixed template: there is no model call anywhere in these chains.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.brain.commands import CommandData, CommandTools
from app.brain.runtime import AiRunRecord, RunLogger, log_ai_run, log_run_in_background
from app.channels.base import CHANNEL_NAMES
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.events import Event, EventBus, bus
from app.payments.invoice import address_text, load_invoice, maps_links
from app.payments.money import IST, free_under_warranty

log = logging.getLogger(__name__)

ROLE = "automation"
PAYMENT_PAID = "payment.paid"
WARRANTY_REPAIR = "warranty_repair"  # the ai_runs trigger of a free booking
JOB_STATUS_CHANGED = "job.status_changed"
JOB_COMPLETED = "job.completed"
JOB_REJECTED = "job.rejected"
TICKET_UPDATED = "ticket.updated"
TICKET_RESOLVED = "ticket.resolved"  # the ai_runs trigger of the resolved notification; not a §9 event
TICKET_DELETED = "ticket.deleted"    # the same for the deleted notification

RECEIPT_TOOLS: frozenset[str] = frozenset({
    "messaging__send_email",
    "messaging__send_reply",
    "tickets__update_status",
})
BOOKING_TOOLS: frozenset[str] = frozenset({
    "inventory__find_compatible_part",
    "inventory__reserve_part",
    "inventory__release_part",
    "inventory__create_restock_request",  # the low-stock alert when a booking takes the part to its threshold
    "dispatch__find_technician",
    "dispatch__create_job",
    "messaging__send_email",
    "messaging__send_reply",
    "messaging__notify_staff",
    "tickets__update_status",
})
PAYMENT_PAID_TOOLS: frozenset[str] = RECEIPT_TOOLS | BOOKING_TOOLS
JOB_TOOLS: frozenset[str] = frozenset({
    "inventory__consume_part",
    "inventory__release_part",
    "inventory__create_restock_request",
    "messaging__send_email",
    "messaging__send_reply",
    "messaging__notify_staff",
    "tickets__update_status",
})

# A rejected job's hand-over (§7.7): find another technician and book them; the part stays reserved.
REJECT_TOOLS: frozenset[str] = frozenset({
    "dispatch__find_technician",
    "dispatch__create_job",
    "inventory__release_part",
    "messaging__send_email",
    "messaging__send_reply",
    "messaging__notify_staff",
    "tickets__update_status",
})

# Telling the customer a ticket was resolved or deleted (§7.9): their own channel and their email.
NOTIFY_TOOLS: frozenset[str] = frozenset({"messaging__send_email", "messaging__send_reply"})
# Only a fresh resolution is announced: a later event on a long-resolved ticket (a summary rewrite, a
# follow-up) carries status resolved too, and must not send it again or late.
RESOLVED_RECENTLY = timedelta(minutes=10)

# Working days tried for a visit, after today (§7.7). Sunday is not a working day.
BOOKING_DAYS = 6
# create_job refusing a technician who filled up meanwhile: how often to ask for the next one, per day.
TECHNICIAN_TRIES_PER_DAY = 3

# A pending link that expired unpaid (§7.6 sweeps): the customer hears it on their channel, once.
PAYMENT_EXPIRED = "payment.expired"  # the ai_runs trigger; the link's expiry is not a §9 event
EXPIRED_TOOLS: frozenset[str] = frozenset({"messaging__send_reply"})
EXPIRED_REPLY = (
    "Your payment link for invoice {invoice} (ticket {ticket}) expired before we received a payment. If you already "
    "paid, reply here with the 12-digit UTR from your UPI app and we'll check it. Otherwise reply here when you'd "
    "like to go ahead and we'll send you a new link."
)
RECEIPT_REPLY = (
    "Payment received, thank you! We've confirmed {total} for invoice {invoice}{utr} on ticket {ticket}. "
    "{email_note}{next_step}"
)
RECEIPT_NEXT = {
    "visit": "We're booking your technician visit now.",
    "shipped": "We're arranging your replacement now.",
    "none": "",
}
VISIT_REPLY = (
    "Your technician visit is booked for {date}. {technician} will come to {address} for the {service}{device}. "
    "They'll call you to agree the time. Ticket {ticket}."
)
SHIPPED_REPLY = (
    "Your replacement {part} is reserved and on its way to {address}. We'll let you know when it ships. "
    "Ticket {ticket}."
)
NOT_BOOKED_REPLY = (
    "Thanks! An agent will confirm your technician visit date with you here shortly. Ticket {ticket}."
)
EN_ROUTE_REPLY = (
    "{technician} is on the way for your {service} (ticket {ticket}). They'll call you if they need directions."
)
RESOLVED_REPLY = (
    "Hi {name}, ticket {ticket} ({title}) is now resolved. If anything still isn't right, just reply here and "
    "we'll pick it up."
)
DELETED_REPLY = (
    "Hi {name}, ticket {ticket} ({title}) has been closed. If you still need help with it, just message us here "
    "and we'll open a new one."
)
CLOSING_REPLY = (
    "Your {service} is done: {technician} has completed the visit, and ticket {ticket} is now resolved. "
    "If anything isn't right, just reply here and we'll pick it up."
)


# ---------- the database reads (and the claims) the chains need outside the MCP tools ----------

_BOOKING = text("""
SELECT t.id AS ticket_id, t.ticket_number, t.title, t.status AS ticket_status, t.customer_id,
       c.full_name, c.email, c.phone,
       pr.serial_number, pr.warranty_until,
       COALESCE(pr.warranty_until >= CAST(now() AT TIME ZONE 'Asia/Kolkata' AS date), FALSE) AS in_warranty,
       pm.id AS model_id, pm.name AS model_name, pm.category,
       a.id AS address_id, a.line1, a.line2, a.city, a.state, a.postal_code, a.location_url,
       s.code AS service_code, s.name AS service_name, s.part_type, s.requires_visit, s.required_skill
FROM tickets t
JOIN customers c                ON c.id = t.customer_id
LEFT JOIN products pr           ON pr.id = t.product_id
LEFT JOIN product_models pm     ON pm.id = pr.model_id
LEFT JOIN addresses a           ON a.id = CAST(:address AS uuid) AND a.customer_id = t.customer_id
LEFT JOIN service_catalog s     ON s.code = upper(:service)
WHERE t.id = CAST(:ticket AS uuid)
""")

_TRIED = text("""
SELECT step, result, notes FROM diagnostic_steps
WHERE ticket_id = CAST(:ticket AS uuid) AND result <> 'pending' ORDER BY position
""")

_JOB = text("""
SELECT j.id AS job_id, j.ticket_id, j.status, j.service_code, j.part_id, j.warehouse_id, j.scheduled_date,
       j.address_id,
       j.notes, j.technician_id, u.name AS technician_name, t.ticket_number, t.customer_id,
       c.full_name AS customer_name, sc.name AS service_name, p.sku AS part_sku, p.name AS part_name,
       w.name AS warehouse_name, pm.name AS model_name
FROM service_jobs j
JOIN tickets t              ON t.id = j.ticket_id
JOIN customers c            ON c.id = t.customer_id
JOIN staff_users u          ON u.id = j.technician_id
LEFT JOIN service_catalog sc ON sc.code = j.service_code
LEFT JOIN parts p           ON p.id = j.part_id
LEFT JOIN warehouses w      ON w.id = j.warehouse_id
LEFT JOIN products pr       ON pr.id = t.product_id
LEFT JOIN product_models pm ON pm.id = pr.model_id
WHERE j.id = CAST(:job AS uuid)
""")


_RESOLUTION = text("""
SELECT t.id AS ticket_id, t.ticket_number, t.title, t.status, t.resolved_at, t.customer_id,
       c.full_name, c.email, pm.name AS model_name, pr.serial_number
FROM tickets t
JOIN customers c            ON c.id = t.customer_id
LEFT JOIN products pr       ON pr.id = t.product_id
LEFT JOIN product_models pm ON pm.id = pr.model_id
WHERE t.id = CAST(:ticket AS uuid)
""")


class WorkflowData:
    """The database reads and the claims the chains need outside the MCP tools. Tests pass a fake."""

    async def invoice(self, payment_id: str) -> dict[str, Any] | None:
        return await load_invoice(payment_id=payment_id)

    async def customer_conversation(self, ticket_id: str, customer_id: str) -> dict[str, Any] | None:
        return await CommandData().customer_conversation(ticket_id, customer_id)

    async def claim_receipt(self, invoice: dict[str, Any]) -> bool:
        """True for exactly one caller per payment, across processes: a payment_confirmed timeline
        event, written under an advisory lock only if there isn't one yet."""
        payload = json.dumps({"payment_id": invoice["payment_id"], "invoice_number": invoice["invoice_number"],
                              "amount": invoice["total"], "utr": invoice["utr"],
                              "receipt_to": invoice["customer"]["email"]})
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                                      {"key": f"payment.paid:{invoice['payment_id']}"})
                claimed = (await session.execute(text("""
                    INSERT INTO ticket_events (ticket_id, type, payload, actor)
                    SELECT CAST(:t AS uuid), 'payment_confirmed', CAST(:payload AS jsonb), 'system'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ticket_events
                        WHERE ticket_id = CAST(:t AS uuid) AND type = 'payment_confirmed'
                          AND payload->>'payment_id' = :p)
                    RETURNING id
                """), {"t": invoice["ticket_id"], "p": invoice["payment_id"], "payload": payload})).scalar()
        return claimed is not None

    async def claim(self, ticket_id: str, type_: str, key: str, payload: dict[str, Any]) -> bool:
        """True for exactly one caller per (ticket, type, key), across processes: a timeline event of
        that type with payload.claim = key, written under an advisory lock only if there isn't one."""
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                                      {"key": f"{type_}:{key}"})
                claimed = (await session.execute(text("""
                    INSERT INTO ticket_events (ticket_id, type, payload, actor)
                    SELECT CAST(:t AS uuid), :type, CAST(:payload AS jsonb), 'system'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ticket_events
                        WHERE ticket_id = CAST(:t AS uuid) AND type = :type AND payload->>'claim' = :key)
                    RETURNING id
                """), {"t": ticket_id, "type": type_, "key": key,
                       "payload": json.dumps({**payload, "claim": key})})).scalar()
        return claimed is not None

    async def resolution(self, ticket_id: str) -> dict[str, Any] | None:
        """What the resolved notification needs: the ticket as it is now, its customer and device."""
        async with SessionLocal() as session:
            row = (await session.execute(_RESOLUTION, {"ticket": ticket_id})).mappings().one_or_none()
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()} if row else None

    async def payment_address(self, payment_id: str) -> str | None:
        async with SessionLocal() as session:
            value = (await session.execute(text(
                "SELECT service_address_id FROM payments WHERE id = CAST(:p AS uuid)"), {"p": payment_id})).scalar()
        return str(value) if value else None

    async def booking(self, ticket_id: str, service_code: str, address_id: str | None) -> dict[str, Any] | None:
        """Everything a booking needs: ticket, customer, device and warranty, address, service, what was tried."""
        async with SessionLocal() as session:
            row = (await session.execute(_BOOKING, {"ticket": ticket_id, "service": service_code,
                                                    "address": address_id})).mappings().one_or_none()
            tried = (await session.execute(_TRIED, {"ticket": ticket_id})).mappings().all()
        return booking_view(dict(row), [dict(t) for t in tried]) if row else None

    async def rejected_technicians(self, ticket_id: str) -> list[str]:
        """Every technician who rejected a job on this ticket: nobody is offered the same job twice."""
        async with SessionLocal() as session:
            rows = (await session.execute(text(
                "SELECT DISTINCT payload->>'technician_id' FROM ticket_events"
                " WHERE ticket_id = CAST(:t AS uuid) AND type = 'job_rejected'"), {"t": ticket_id})).scalars().all()
        return [str(r) for r in rows if r]

    async def job(self, job_id: str) -> dict[str, Any] | None:
        async with SessionLocal() as session:
            row = (await session.execute(_JOB, {"job": job_id})).mappings().one_or_none()
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()} if row else None


def booking_view(row: dict[str, Any], tried: list[dict[str, Any]]) -> dict[str, Any]:
    address = None
    if row.get("address_id"):
        address = {k: row.get(k) for k in ("line1", "line2", "city", "state", "postal_code", "location_url")}
        address["address_id"] = str(row["address_id"])
        address["text"] = address_text(address)
        address["maps_links"] = maps_links(address)
    service = None
    if row.get("service_code"):
        service = {"code": row["service_code"], "name": row["service_name"], "part_type": row["part_type"],
                   "requires_visit": bool(row["requires_visit"]), "required_skill": row["required_skill"]}
    return {
        "ticket_id": str(row["ticket_id"]), "ticket_number": row["ticket_number"], "title": row["title"],
        "ticket_status": row["ticket_status"], "customer_id": str(row["customer_id"]),
        "customer": {"full_name": row["full_name"], "email": row["email"], "phone": row["phone"]},
        "device": {"name": row["model_name"], "serial_number": row["serial_number"], "category": row["category"]}
        if row.get("serial_number") else None,
        "model_id": str(row["model_id"]) if row.get("model_id") else None,
        "in_warranty": bool(row["in_warranty"]),
        "warranty_until": row["warranty_until"].isoformat() if row.get("warranty_until") else None,
        "address": address, "service": service,
        "tried": [{"step": t["step"], "result": t["result"], "notes": t.get("notes")} for t in tried],
    }


# ---------- small helpers ----------


def booking_dates(now: datetime | None = None, days: int = BOOKING_DAYS) -> list[date]:
    """The next `days` working days (Monday to Saturday, IST) after today."""
    day = (now or datetime.now(UTC)).astimezone(IST).date()
    out: list[date] = []
    while len(out) < days:
        day += timedelta(days=1)
        if day.weekday() != 6:  # Sunday
            out.append(day)
    return out


def reassign_dates(scheduled: date | str, now: datetime | None = None, days: int = BOOKING_DAYS) -> list[date]:
    """A rejected job's dates: its own date, or the next working day if that has passed, then the working
    days after it, BOOKING_DAYS in all (Monday to Saturday, IST)."""
    scheduled = date.fromisoformat(scheduled) if isinstance(scheduled, str) else scheduled
    today = (now or datetime.now(UTC)).astimezone(IST).date()
    day = scheduled if scheduled >= today else booking_dates(now, 1)[0]
    out: list[date] = []
    while len(out) < days:
        if day.weekday() != 6:  # Sunday
            out.append(day)
        day += timedelta(days=1)
    return out


def date_display(day: date | str) -> str:
    """"Mon 5 Oct 2026". Built by hand: Windows strftime has no %-d."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{day:%a} {day.day} {day:%b %Y}"


def sentence(text_: str) -> str:
    """The first letter upper-cased and nothing else touched (str.capitalize lower-cases "INV-2026-00002")."""
    return text_[:1].upper() + text_[1:]


def first_name(name: str | None) -> str:
    return (name or "").split(" ")[0] or "Your technician"


@dataclass
class WorkflowResult:
    outcome: str                      # done | skipped
    reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    booking: "BookingOutcome | None" = None


@dataclass
class BookingOutcome:
    outcome: str                      # scheduled | shipping | not_booked | no_booking | skipped
    reason: str | None = None
    job: dict[str, Any] | None = None
    reservation: dict[str, Any] | None = None


class _Chain:
    """One run's tool gate plus its error list. A failed step is recorded and the rest still run."""

    def __init__(self, tools: CommandTools, label: str) -> None:
        self.tools = tools
        self.label = label
        self.errors: list[str] = []
        self.start = time.monotonic()

    async def step(self, name: str, **args: Any) -> Any:
        try:
            result = await self.tools.call(name, **args)
        except Exception as e:
            log.exception("%s %s: %s failed", self.tools.trigger, self.label, name)
            self.errors.append(f"{name}: {type(e).__name__}: {e}"[:200])
            return None
        if isinstance(result, dict) and result.get("ok") is False:
            self.errors.append(f"{name}: {result.get('error')}")
        return result

    def log(self, log_run: RunLogger | None) -> None:
        log_run_in_background(log_run or log_ai_run, AiRunRecord(
            role=ROLE, trigger=self.tools.trigger, ticket_id=self.tools.ticket_id, model=None, input_tokens=None,
            output_tokens=None, tool_calls=self.tools.records,
            latency_ms=round((time.monotonic() - self.start) * 1000), error="; ".join(self.errors)[:500] or None,
        ))


def _ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is not False


# ---------- the booking (§7.7, §7.8) ----------


async def book(
    chain: _Chain,
    data: WorkflowData,
    *,
    ticket_id: str,
    service_code: str,
    address_id: str | None,
    claim_key: str,
    authorization: str,
    free: bool,
    settings: Settings,
    now: datetime | None = None,
) -> BookingOutcome:
    """Reserve the part and book the visit (or the shipment) for a paid or free repair. Once per claim_key."""
    b = await data.booking(ticket_id, service_code, address_id)
    if b is None or b["service"] is None:
        return BookingOutcome("skipped", "no such ticket or service")
    service = b["service"]
    if not service["part_type"] and not service["requires_visit"]:
        return BookingOutcome("no_booking", f"{service['code']} needs no part and no visit")
    if not await data.claim(ticket_id, "job_requested", claim_key,
                            {"service_code": service["code"], "authorized_by": authorization}):
        return BookingOutcome("skipped", "already booked for this payment or request")

    ticket_number = b["ticket_number"]
    customer = b["customer"]
    conversation = await data.customer_conversation(ticket_id, b["customer_id"])
    reservation: dict[str, Any] | None = None
    part: dict[str, Any] | None = None

    async def fail(reason: str) -> BookingOutcome:
        """No job: give back anything reserved, tell the admins why and the customer what happens next."""
        if reservation is not None:
            await chain.step("inventory__release_part", part_id=reservation["part_id"],
                             warehouse_id=reservation["warehouse_id"], qty=1, ticket_id=ticket_id)
        where = b["address"]["text"] if b["address"] else "no service address"
        await chain.step("messaging__notify_staff", role="admin", type="booking_failed",
                         title=f"Couldn't book the {service['name'].lower()} for {ticket_number}",
                         body=f"{reason}. {customer['full_name'] or 'Customer'}, {customer['phone'] or 'no phone'}, "
                              f"{where}. {sentence(authorization)}.",
                         link=f"/tickets/{ticket_id}")
        if conversation is not None:
            await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                             text=NOT_BOOKED_REPLY.format(ticket=ticket_number))
        else:
            chain.errors.append("no customer conversation to reply on")
        await chain.step("tickets__update_status", ticket_id=ticket_id, status="in_progress",
                         note=f"No visit booked: {reason}. Admins were told; an agent will confirm the date.")
        return BookingOutcome("not_booked", reason)

    try:
        if b["address"] is None:
            return await fail("there is no service address on the booking")
        if free and not free_under_warranty(service["code"], b["in_warranty"]):
            return await fail(f"{service['code']} isn't free under warranty any more, so it needs an invoice")

        if service["part_type"]:
            found = await chain.step("inventory__find_compatible_part", model_id=b["model_id"] or "",
                                     part_type=service["part_type"])
            if not isinstance(found, dict) or not found.get("found"):
                return await fail(f"no {service['part_type']} part fits the {b['device'] and b['device']['name']}")
            part = found
            stock = max(found.get("stock") or [{}], key=lambda s: s.get("available", 0))
            if stock.get("available", 0) < 1:
                return await fail(f"{found['sku']} is out of stock")
            reserved = await chain.step("inventory__reserve_part", part_id=found["part_id"],
                                        warehouse_id=stock["warehouse_id"], qty=1, ticket_id=ticket_id)
            if not isinstance(reserved, dict) or not reserved.get("ok"):
                return await fail(f"{found['sku']} couldn't be reserved "
                                  f"({reserved.get('message') if isinstance(reserved, dict) else 'inventory down'})")
            reservation = {**reserved, "warehouse_name": stock.get("warehouse_name")}

        low_stock = reservation if reservation is not None and reservation.get("restock_due") else None
        if not service["requires_visit"]:
            outcome = await _ship(chain, b, part, reservation, conversation, authorization)
        else:
            outcome = await _visit(chain, b, part, reservation, conversation, authorization, settings, now, fail)
            if outcome.outcome == "scheduled":
                reservation = None  # it belongs to the job now
        if low_stock is not None and outcome.outcome in ("scheduled", "shipping"):
            # §7.8: this booking took the part to its threshold. Only once the booking stands: a failed
            # one gave the part back, so stock never really dropped.
            await _restock(chain, low_stock, part_id=low_stock["part_id"], warehouse_id=low_stock["warehouse_id"],
                           warehouse_name=low_stock.get("warehouse_name"), ticket_id=ticket_id,
                           ticket_number=ticket_number, cause=f"booking {ticket_number}", settings=settings,
                           every_drop=True)
        return outcome
    except Exception:
        # An unexpected failure must not leave the part held for a job that was never made.
        if reservation is not None:
            await chain.step("inventory__release_part", part_id=reservation["part_id"],
                             warehouse_id=reservation["warehouse_id"], qty=1, ticket_id=ticket_id)
        raise


async def _ship(chain: _Chain, b: dict[str, Any], part: dict[str, Any] | None, reservation: dict[str, Any] | None,
                conversation: dict[str, Any] | None, authorization: str) -> BookingOutcome:
    """A shipped service (charger, ear cushions): the part is reserved; admins send it."""
    customer, address = b["customer"], b["address"]
    label = f"{part['name']} ({part['sku']})" if part else b["service"]["name"]
    await chain.step("messaging__notify_staff", role="admin", type="ship_part",
                     title=f"Ship {part['sku'] if part else b['service']['code']} for {b['ticket_number']}",
                     body=f"{label} to {customer['full_name'] or 'the customer'}, {customer['phone'] or 'no phone'}, "
                          f"{address['text']}. Reserved at {reservation and reservation.get('warehouse_name')}. "
                          f"{sentence(authorization)}.",
                     link=f"/tickets/{b['ticket_id']}")
    if conversation is not None:
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=SHIPPED_REPLY.format(part=part["name"] if part else b["service"]["name"].lower(),
                                                   address=address["text"], ticket=b["ticket_number"]))
    await chain.step("tickets__update_status", ticket_id=b["ticket_id"], status="in_progress",
                     note=f"{label} reserved at {reservation and reservation.get('warehouse_name')}; "
                          f"admins asked to ship it to {address['text']} ({authorization}).")
    return BookingOutcome("shipping", reservation=reservation)


async def _visit(chain: _Chain, b: dict[str, Any], part: dict[str, Any] | None,
                 reservation: dict[str, Any] | None, conversation: dict[str, Any] | None, authorization: str,
                 settings: Settings, now: datetime | None, fail) -> BookingOutcome:
    service, address = b["service"], b["address"]
    job, technician, last_reason, error = await _book_technician(chain, b, part, booking_dates(now))
    if error is not None:
        return await fail(error)
    if job is None or technician is None:
        return await fail(f"no {service['required_skill']} technician in {address['city']} has room in the next "
                          f"{BOOKING_DAYS} working days ({last_reason})")
    await _announce_visit(chain, b, job, technician, part, reservation, conversation, authorization, settings)
    when = date_display(job["scheduled_date"])
    carried = f"; {part['sku']} reserved at {reservation and reservation.get('warehouse_name')}" if part else ""
    await chain.step("tickets__update_status", ticket_id=b["ticket_id"], status="scheduled",
                     note=f"Visit booked for {when} with {technician['name']}{carried} ({authorization}).")
    return BookingOutcome("scheduled", job=job, reservation=reservation)


async def _book_technician(chain: _Chain, b: dict[str, Any], part: dict[str, Any] | None, dates: list[date],
                           exclude: list[str] | None = None) -> tuple[dict | None, dict | None, str, str | None]:
    """The technician search and the create_job loop, shared by the booking and the reassignment after a
    rejection: each date in turn, the least-worked technician with room (minus `exclude`), skipping one who
    filled up meanwhile. (job, technician, last_reason, None), or (None, None, reason, error) when create_job
    refused for anything but a full day."""
    service, address = b["service"], b["address"]
    last_reason = "no technician found"
    for day in dates:
        for _ in range(TECHNICIAN_TRIES_PER_DAY):
            search = {"city": address["city"], "skill": service["required_skill"], "date": day.isoformat()}
            if exclude:
                search["exclude_technician_ids"] = list(exclude)
            found = await chain.step("dispatch__find_technician", **search)
            if not isinstance(found, dict) or not found.get("found"):
                if isinstance(found, dict) and found.get("reason"):
                    last_reason = found["reason"]
                break
            technician = found["technician"]
            created = await chain.step("dispatch__create_job", ticket_id=b["ticket_id"],
                                       technician_id=technician["technician_id"], address_id=address["address_id"],
                                       service_code=service["code"], part_id=part["part_id"] if part else None,
                                       date=day.isoformat())
            if isinstance(created, dict) and created.get("ok"):
                return created, technician, last_reason, None
            if not isinstance(created, dict) or created.get("error") != "technician_full":
                return None, None, last_reason, (f"the job couldn't be created "
                                                 f"({created.get('message') if isinstance(created, dict) else 'dispatch down'})")
    return None, None, last_reason, None


async def _announce_visit(chain: _Chain, b: dict[str, Any], job: dict[str, Any], technician: dict[str, Any],
                          part: dict[str, Any] | None, reservation: dict[str, Any] | None,
                          conversation: dict[str, Any] | None, authorization: str, settings: Settings, *,
                          email_customer: bool = True) -> None:
    """The technician's job_assigned email and notification, then the customer's visit_scheduled email and
    the reply on their channel (§7.7). A reassignment tells the customer on their channel only, or by email
    when there is no conversation, so they hear it exactly once."""
    service, address, customer = b["service"], b["address"], b["customer"]
    when = date_display(job["scheduled_date"])
    device = b["device"]
    billing = sentence(authorization)
    job_url = f"{settings.frontend_url.rstrip('/')}/jobs/{job['job_id']}"
    if technician.get("email"):
        await chain.step("messaging__send_email", to=technician["email"], template="job_assigned",
                         ticket_id=b["ticket_id"],
                         subject=f"New job {when}: {service['name']} for {customer['full_name']} ({b['ticket_number']})",
                         data={
                             "technician_name": technician["name"], "ticket_number": b["ticket_number"],
                             "job_url": job_url, "scheduled_date_display": when, "service_name": service["name"],
                             "customer": customer, "address_text": address["text"],
                             "maps_links": address["maps_links"], "device": device, "issue": b["title"],
                             "part": {"sku": part["sku"], "name": part["name"],
                                      "warehouse_name": reservation and reservation.get("warehouse_name")}
                             if part else None,
                             "tried": b["tried"], "billing": billing,
                         })
    await chain.step("messaging__notify_staff", user_id=technician["technician_id"], type="job_assigned",
                     title=f"New job on {when}: {service['name']}",
                     body=f"{customer['full_name']} · {address['text']} · {b['ticket_number']}",
                     link=f"/jobs/{job['job_id']}")
    if customer.get("email") and (email_customer or conversation is None):
        await chain.step("messaging__send_email", to=customer["email"], template="visit_scheduled",
                         ticket_id=b["ticket_id"], subject=f"Your technician visit on {when} ({b['ticket_number']})",
                         data={"customer_name": customer["full_name"], "ticket_number": b["ticket_number"],
                               "scheduled_date_display": when, "technician_first_name": first_name(technician["name"]),
                               "service_name": service["name"], "device": device, "address_text": address["text"]})
    if conversation is not None:
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=VISIT_REPLY.format(date=when, technician=first_name(technician["name"]),
                                                 address=address["text"], service=service["name"].lower(),
                                                 device=f" on your {device['name']}" if device else "",
                                                 ticket=b["ticket_number"]))
    else:
        chain.errors.append("no customer conversation to reply on")


# ---------- payment.paid (§7.6 step 6) ----------


async def on_payment_paid(
    event: Event,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> WorkflowResult:
    """The payment.paid chain: the receipt, then the booking. Safe to run any number of times for one payment."""
    data = data or WorkflowData()
    settings = settings or get_settings()
    payment_id = str(event.data.get("payment_id") or "")
    try:
        uuid.UUID(payment_id)
    except ValueError:
        log.warning("payment.paid without a payment id: %s", event.data)
        return WorkflowResult("skipped", "no payment id")

    # The event says paid; the database decides. A receipt for money that never arrived is the one
    # thing this chain must not do.
    invoice = await data.invoice(payment_id)
    if invoice is None or invoice["status"] != "paid":
        log.warning("payment.paid for %s, but it is %s; nothing done", payment_id, invoice and invoice["status"])
        return WorkflowResult("skipped", f"payment is {invoice['status'] if invoice else 'missing'}")

    tools = CommandTools(PAYMENT_PAID_TOOLS, call_tool, events, role=ROLE, trigger=PAYMENT_PAID,
                         ticket_id=invoice["ticket_id"])
    chain = _Chain(tools, invoice["invoice_number"])
    receipt = await data.claim_receipt(invoice)
    if receipt:
        await _receipt(chain, data, invoice)
    else:
        log.info("payment.paid for %s again: its receipt already went out", invoice["invoice_number"])
    booking = await book(
        chain, data, ticket_id=invoice["ticket_id"], service_code=invoice["service"]["code"],
        address_id=await data.payment_address(payment_id), claim_key=payment_id,
        authorization=f"paid, invoice {invoice['invoice_number']}", free=False, settings=settings, now=now)
    if not receipt and booking.outcome in ("skipped", "no_booking"):
        return WorkflowResult("skipped", "receipt already sent and nothing left to book", booking=booking)

    chain.log(log_run)
    log.info("payment.paid %s: receipt %s, booking %s (%d steps%s)", invoice["invoice_number"],
             "sent" if receipt else "already sent", booking.outcome, len(tools.records),
             f", {len(chain.errors)} problem(s)" if chain.errors else "")
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors, booking=booking)


async def _receipt(chain: _Chain, data: WorkflowData, invoice: dict[str, Any]) -> None:
    email = invoice["customer"]["email"]
    if email:
        await chain.step("messaging__send_email", to=email, template="payment_confirmed", data=invoice,
                         ticket_id=invoice["ticket_id"], attachments=["receipt_pdf"],
                         subject=f"Payment received: invoice {invoice['invoice_number']} ({invoice['ticket_number']})")
    conversation = await data.customer_conversation(invoice["ticket_id"], invoice["customer_id"])
    service = invoice.get("service") or {}
    kind = service.get("fulfilment") or "visit"
    if conversation is not None:
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=RECEIPT_REPLY.format(
                             total=invoice["total_display"], invoice=invoice["invoice_number"],
                             # An admin may confirm it from the bank statement, with no UTR from the customer.
                             utr=f" (UTR {invoice['utr']})" if invoice["utr"] else "", ticket=invoice["ticket_number"],
                             email_note=f"Your receipt is on its way to {email}. " if email else "",
                             next_step=RECEIPT_NEXT.get(kind, "")).strip())
    else:
        chain.errors.append("no customer conversation to reply on")
    await chain.step("tickets__update_status", ticket_id=invoice["ticket_id"], status="in_progress",
                     note=f"Payment {invoice['invoice_number']} received ({invoice['total_display']}, UPI)"
                          f" for {service.get('code') or 'the service'}.")


async def tell_customer_link_expired(
    expired: dict[str, Any],
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
) -> WorkflowResult:
    """A link the sweep just expired, unpaid: tell the customer on the channel they last wrote on (§15, never
    silence). One fixed message, no model; `expired` is {payment_id, ticket_id, customer_id, invoice_number,
    ticket_number}. The sweep expires a link once, so this runs once per link."""
    data = data or WorkflowData()
    tools = CommandTools(EXPIRED_TOOLS, call_tool, events, role=ROLE, trigger=PAYMENT_EXPIRED,
                         ticket_id=str(expired["ticket_id"]))
    chain = _Chain(tools, expired["invoice_number"])
    conversation = await data.customer_conversation(str(expired["ticket_id"]), str(expired["customer_id"]))
    if conversation is None:
        chain.errors.append("no customer conversation to reply on")
    else:
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=EXPIRED_REPLY.format(invoice=expired["invoice_number"], ticket=expired["ticket_number"]))
    chain.log(log_run)
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors)


# ---------- a free warranty repair (§7.6, §7.7) ----------

_tasks: set[asyncio.Task[Any]] = set()


def start_warranty_booking(**kwargs: Any) -> asyncio.Task[Any]:
    """Book a free warranty repair in the background, so intake answers the customer at once."""
    task = asyncio.create_task(_warranty_booking_logged(**kwargs), name="warranty-booking")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _warranty_booking_logged(**kwargs: Any) -> None:
    try:
        await on_warranty_booking(**kwargs)
    except Exception:
        log.exception("warranty booking failed for ticket %s", kwargs.get("ticket_id"))


async def drain(timeout: float = 15.0) -> int:
    """Wait for background bookings still running (shutdown and tests)."""
    pending = [t for t in _tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    return len(pending)


async def on_warranty_booking(
    *,
    ticket_id: str,
    service_code: str,
    address_id: str,
    request_key: str,
    staff_id: str,
    staff_name: str | None = None,
    command: str = "/payments",
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> WorkflowResult:
    """Book a free warranty repair once the customer's details are in, or at once from /schedule when
    they are on file. No invoice; authorized by the staff member who ran /payments or /schedule (the
    request's requested_by), never by customer text. The warranty is checked again here, in code,
    before anything is booked."""
    data = data or WorkflowData()
    tools = CommandTools(BOOKING_TOOLS, call_tool, events, role=ROLE, trigger=WARRANTY_REPAIR, ticket_id=ticket_id)
    chain = _Chain(tools, ticket_id)
    booking = await book(
        chain, data, ticket_id=ticket_id, service_code=service_code, address_id=address_id,
        claim_key=f"warranty:{request_key}",
        authorization=f"free under warranty, authorized by {staff_name or staff_id} with {command}",
        free=True, settings=settings or get_settings(), now=now)
    if booking.outcome in ("skipped", "no_booking"):
        return WorkflowResult("skipped", booking.reason, booking=booking)
    chain.log(log_run)
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors, booking=booking)


# ---------- job status changes and completion (§7.7 steps 4-5, §7.8) ----------


async def on_job_status_changed(
    event: Event,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
) -> WorkflowResult:
    """en_route tells the customer; cancelled releases the part and hands the ticket back to staff."""
    status = event.data.get("status")
    if status not in ("en_route", "cancelled"):
        return WorkflowResult("skipped", f"nothing to do for {status}")
    data = data or WorkflowData()
    job = await _current_job(data, event, status)
    if job is None:
        return WorkflowResult("skipped", "the job isn't in that status")
    tools = CommandTools(JOB_TOOLS, call_tool, events, role=ROLE, trigger=JOB_STATUS_CHANGED,
                         ticket_id=job["ticket_id"])
    chain = _Chain(tools, job["ticket_number"])
    if status == "en_route":
        conversation = await data.customer_conversation(job["ticket_id"], job["customer_id"])
        if conversation is None:
            return WorkflowResult("skipped", "no customer conversation")
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=EN_ROUTE_REPLY.format(technician=first_name(job["technician_name"]),
                                                    service=(job["service_name"] or job["service_code"]).lower(),
                                                    ticket=job["ticket_number"]))
    else:
        if not await data.claim(job["ticket_id"], "job_closed", f"{job['job_id']}:cancelled",
                                {"job_id": job["job_id"], "status": "cancelled"}):
            return WorkflowResult("skipped", "already handled")
        if job["part_id"] and job["warehouse_id"]:
            await chain.step("inventory__release_part", part_id=job["part_id"], warehouse_id=job["warehouse_id"],
                             qty=1, ticket_id=job["ticket_id"], job_id=job["job_id"])
        await chain.step("tickets__update_status", ticket_id=job["ticket_id"], status="in_progress",
                         note=f"Job for {date_display(job['scheduled_date'])} with {job['technician_name']} cancelled"
                              f"{': ' + job['notes'] if job.get('notes') else ''}."
                              f"{' ' + job['part_sku'] + ' released.' if job.get('part_sku') else ''}")
        await chain.step("messaging__notify_staff", role="admin", type="job_cancelled",
                         title=f"Job cancelled on {job['ticket_number']}",
                         body=f"{job['service_name']} for {job['customer_name']} on "
                              f"{date_display(job['scheduled_date'])} with {job['technician_name']}. "
                              "The ticket is back in progress; rebook or contact the customer.",
                         link=f"/tickets/{job['ticket_id']}")
    chain.log(log_run)
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors)


async def on_job_completed(
    event: Event,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
    settings: Settings | None = None,
) -> WorkflowResult:
    """Consume the part, the low-stock check and restock alert, resolve the ticket, close with the customer."""
    data = data or WorkflowData()
    settings = settings or get_settings()
    job = await _current_job(data, event, "completed")
    if job is None:
        return WorkflowResult("skipped", "the job isn't completed")
    if not await data.claim(job["ticket_id"], "job_closed", f"{job['job_id']}:completed",
                            {"job_id": job["job_id"], "status": "completed"}):
        return WorkflowResult("skipped", "already handled")
    tools = CommandTools(JOB_TOOLS, call_tool, events, role=ROLE, trigger=JOB_COMPLETED, ticket_id=job["ticket_id"])
    chain = _Chain(tools, job["ticket_number"])

    used = ""
    if job["part_id"] and job["warehouse_id"]:
        consumed = await chain.step("inventory__consume_part", part_id=job["part_id"],
                                    warehouse_id=job["warehouse_id"], qty=1, job_id=job["job_id"])
        if _ok(consumed) and isinstance(consumed, dict):
            used = f" {job['part_sku']} used ({consumed['available']} left available)."
            if consumed.get("restock_due"):
                await _restock(chain, consumed, part_id=job["part_id"], warehouse_id=job["warehouse_id"],
                               warehouse_name=job["warehouse_name"], ticket_id=job["ticket_id"],
                               ticket_number=job["ticket_number"], cause=f"job {job['ticket_number']}",
                               settings=settings)
    conversation = await data.customer_conversation(job["ticket_id"], job["customer_id"])
    # The closing chat message below is this chain's own, so the resolved notification only emails (§7.9).
    await chain.step("tickets__update_status", ticket_id=job["ticket_id"], status="resolved",
                     note=f"Job completed by {job['technician_name']}"
                          f"{': ' + job['notes'] if job.get('notes') else ''}.{used}",
                     customer_told=conversation is not None)
    if conversation is not None:
        await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                         text=CLOSING_REPLY.format(service=(job["service_name"] or job["service_code"]).lower(),
                                                   technician=first_name(job["technician_name"]),
                                                   ticket=job["ticket_number"]))
    else:
        chain.errors.append("no customer conversation to reply on")
    chain.log(log_run)
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors)


async def on_job_rejected(
    event: Event,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> WorkflowResult:
    """The assigned technician rejected the job: book another one, the part still held (§7.7).

    Claimed once per job with the cancel path's own claim (job_closed, "<job>:cancelled"), so an event
    replay does nothing and the cancellation workflow can never release this job's part as well. The
    search is the booking chain's, without everyone who rejected a job on this ticket.
    """
    data = data or WorkflowData()
    settings = settings or get_settings()
    job = await _current_job(data, event, "cancelled")
    if job is None:
        return WorkflowResult("skipped", "the job isn't closed")
    if not await data.claim(job["ticket_id"], "job_closed", f"{job['job_id']}:cancelled",
                            {"job_id": job["job_id"], "status": "rejected"}):
        return WorkflowResult("skipped", "already handled")
    tools = CommandTools(REJECT_TOOLS, call_tool, events, role=ROLE, trigger=JOB_REJECTED, ticket_id=job["ticket_id"])
    chain = _Chain(tools, job["ticket_number"])
    rejecter = job["technician_name"]
    reason = str(event.data.get("reason") or "no reason given")
    was = date_display(job["scheduled_date"])
    b = await data.booking(job["ticket_id"], job["service_code"], str(job.get("address_id") or "") or None)
    conversation = await data.customer_conversation(job["ticket_id"], job["customer_id"])
    part = {"part_id": job["part_id"], "sku": job["part_sku"], "name": job["part_name"]} if job["part_id"] else None
    held = {"warehouse_name": job["warehouse_name"]} if job["part_id"] else None
    service = (job["service_name"] or job["service_code"])

    new_job = technician = None
    why = "the booking details couldn't be read"
    if b is not None and b["service"] is not None and b["address"] is not None:
        new_job, technician, last_reason, error = await _book_technician(
            chain, b, part, reassign_dates(job["scheduled_date"], now),
            exclude=await data.rejected_technicians(job["ticket_id"]))
        why = error or (f"no other {b['service']['required_skill']} technician in {b['address']['city']} has room "
                        f"in the next {BOOKING_DAYS} working days ({last_reason})")

    if new_job is not None and technician is not None:
        when = date_display(new_job["scheduled_date"])
        await _announce_visit(chain, b, new_job, technician, part, held, conversation,
                              f"reassigned after {rejecter} rejected the job", settings, email_customer=False)
        await chain.step("tickets__update_status", ticket_id=job["ticket_id"], status="scheduled",
                         note=f"Job rejected by {rejecter} ({reason}); reassigned to {technician['name']} on {when}.")
        await chain.step("messaging__notify_staff", role="admin", type="job_rejected",
                         title=f"{rejecter} rejected a job on {job['ticket_number']}; {first_name(technician['name'])} has it",
                         body=f"{service} for {job['customer_name']}, was {was}: \"{reason}\". "
                              f"Reassigned to {technician['name']} on {when}.",
                         link=f"/tickets/{job['ticket_id']}")
        outcome = "reassigned"
    else:
        if job["part_id"] and job["warehouse_id"]:
            await chain.step("inventory__release_part", part_id=job["part_id"], warehouse_id=job["warehouse_id"],
                             qty=1, ticket_id=job["ticket_id"], job_id=job["job_id"])
        await chain.step("tickets__update_status", ticket_id=job["ticket_id"], status="in_progress",
                         note=f"Job rejected by {rejecter} ({reason}); not reassigned: {why}."
                              f"{' ' + job['part_sku'] + ' released.' if job.get('part_sku') else ''}")
        if conversation is not None:
            await chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                             text=NOT_BOOKED_REPLY.format(ticket=job["ticket_number"]))
        else:
            chain.errors.append("no customer conversation to reply on")
        await chain.step("messaging__notify_staff", role="admin", type="job_rejected",
                         title=f"{rejecter} rejected a job on {job['ticket_number']}; nobody else is free",
                         body=f"{service} for {job['customer_name']}, was {was}: \"{reason}\". {sentence(why)}. "
                              "Rebook or contact the customer.",
                         link=f"/tickets/{job['ticket_id']}")
        outcome = "not_reassigned"
    chain.log(log_run)
    return WorkflowResult("done", outcome, tool_calls=tools.records, errors=chain.errors)


async def _restock(chain: _Chain, stock: dict[str, Any], *, part_id: str, warehouse_id: str,
                   warehouse_name: str | None, ticket_id: str, ticket_number: str, cause: str, settings: Settings,
                   every_drop: bool = False) -> None:
    """§7.8: one open restock request per part, the stock.low event, the email, the admins.

    `stock` is what reserve_part or consume_part said. A consume alerts only when it files a new request;
    a booking that crossed the threshold (`every_drop`) alerts once per drop, so after stock went back
    above the threshold the next drop is reported again, on the request that is still open.
    """
    where = warehouse_name or "the warehouse"
    reason = (f"{stock['sku']} is down to {stock['available']} available at {where} "
              f"(threshold {stock['reorder_threshold']}) after {cause}")
    request = await chain.step("inventory__create_restock_request", part_id=part_id,
                               warehouse_id=warehouse_id, qty=stock["reorder_qty"], reason=reason)
    if not isinstance(request, dict) or not request.get("ok"):
        return
    if not request.get("created") and not every_drop:
        return
    # A new request re-read the stock; an open one only says its id and qty, so the rest comes from `stock`.
    alert = {"restock_request_id": request["restock_request_id"],
             **{key: request.get(key, stock.get(key))
                for key in ("sku", "name", "on_hand", "reserved", "available", "reorder_threshold")},
             "warehouse_name": request.get("warehouse_name") or where,
             "qty": request.get("qty") or stock.get("reorder_qty"), "reason": reason}
    to = settings.warehouse_alert_email.strip()
    if to:
        await chain.step("messaging__send_email", to=to, template="restock_alert", data=alert,
                         ticket_id=ticket_id,
                         subject=f"Restock {alert['sku']}: {alert['available']} left at {alert['warehouse_name']}")
    else:
        chain.errors.append("WAREHOUSE_ALERT_EMAIL is not set; restock alert not emailed")
    await chain.step("messaging__notify_staff", role="admin", type="stock_low",
                     title=f"Low stock: {alert['sku']} ({alert['available']} left)",
                     body=f"Restock request for {alert['qty']} × {alert['name']} at {alert['warehouse_name']}.",
                     link="/inventory")


# ---------- telling the customer a ticket is resolved or deleted (§7.9) ----------


def usable_address(email: str | None) -> bool:
    """An address worth writing to: there is one, and it isn't a no-reply or bounce address, which would
    only bounce back into the support inbox (app/channels/email_channel.py)."""
    from app.channels.email_channel import AUTOMATED_LOCAL_PART

    return bool(email and "@" in email and not AUTOMATED_LOCAL_PART.search(email.rpartition("@")[0]))


async def notify_customer(chain: _Chain, ticket: dict[str, Any], conversation: dict[str, Any] | None, *,
                          kind: str, chat_already: bool = False) -> dict[str, bool]:
    """The email and the chat message, at the same time. Returns which were queued.

    `ticket` carries ticket_number, title, full_name, email, model_name, serial_number. On the email
    channel the chat message is itself an email in the customer's thread, so no second one is sent.
    """
    email = ticket.get("email")
    address_ok = usable_address(email)
    channel = conversation["channel"] if conversation else None
    chat = conversation is not None and not chat_already and (channel != "email" or address_ok)
    mail = address_ok and channel != "email"
    name = first_name(ticket.get("full_name")) if ticket.get("full_name") else "there"
    title = ticket.get("title") or "your request"
    template = "ticket_resolved" if kind == "resolved" else "ticket_deleted"
    subject = (f"[{ticket['ticket_number']}] Your support request is resolved" if kind == "resolved"
               else f"[{ticket['ticket_number']}] Your support request has been closed")
    reply = (RESOLVED_REPLY if kind == "resolved" else DELETED_REPLY).format(
        name=name, ticket=ticket["ticket_number"], title=title)
    data = {"customer_name": name, "ticket_number": ticket["ticket_number"], "title": title,
            "device": ticket.get("model_name"), "serial": ticket.get("serial_number"),
            "channel": CHANNEL_NAMES.get(channel or "", None)}

    steps = []
    if chat:
        steps.append(chain.step("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                                text=reply))
    if mail:
        steps.append(chain.step("messaging__send_email", to=email, template=template, subject=subject, data=data,
                                ticket_id=ticket.get("ticket_id") if kind == "resolved" else None))
    results = await asyncio.gather(*steps)
    sent = iter(results)
    return {"chat": chat and _ok(next(sent)), "email": mail and _ok(next(sent))}


async def on_ticket_resolved(
    event: Event,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: WorkflowData | None = None,
    now: datetime | None = None,
) -> WorkflowResult:
    """A ticket became resolved, by any path: email the customer and message them on their channel (§7.9)."""
    if event.data.get("status") != "resolved":
        return WorkflowResult("skipped", "not a resolution")
    ticket_id = str(event.data.get("ticket_id") or "")
    try:
        uuid.UUID(ticket_id)
    except ValueError:
        return WorkflowResult("skipped", "no ticket")
    data = data or WorkflowData()
    ticket = await data.resolution(ticket_id)
    # The event says what happened; the database decides.
    if ticket is None or ticket["status"] != "resolved" or ticket.get("resolved_at") is None:
        return WorkflowResult("skipped", "the ticket isn't resolved")
    if (now or datetime.now(UTC)) - ticket["resolved_at"] > RESOLVED_RECENTLY:
        return WorkflowResult("skipped", "an earlier resolution")
    conversation = await data.customer_conversation(ticket_id, ticket["customer_id"])
    if not await data.claim(ticket_id, "resolution_notified", ticket["resolved_at"].isoformat(),
                            {"channel": conversation["channel"] if conversation else None}):
        return WorkflowResult("skipped", "already handled")

    tools = CommandTools(NOTIFY_TOOLS, call_tool, events, role=ROLE, trigger=TICKET_RESOLVED, ticket_id=ticket_id)
    chain = _Chain(tools, ticket["ticket_number"])
    await notify_customer(chain, ticket, conversation, kind="resolved",
                          chat_already=bool(event.data.get("customer_told")))
    chain.log(log_run)
    return WorkflowResult("done", tool_calls=tools.records, errors=chain.errors)


async def notify_ticket_deleted(
    ticket: dict[str, Any],
    conversation: dict[str, Any] | None,
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
) -> dict[str, bool]:
    """The ticket is gone (DELETE /api/tickets/{id} has committed): tell the customer by email and on their
    channel (§7.9). `ticket` and `conversation` were read before the delete. Never raises."""
    tools = CommandTools(NOTIFY_TOOLS, call_tool, events, role=ROLE, trigger=TICKET_DELETED, ticket_id=None)
    chain = _Chain(tools, ticket["ticket_number"])
    try:
        sent = await notify_customer(chain, ticket, conversation, kind="deleted")
    except Exception:
        log.exception("ticket.deleted %s: the customer couldn't be told", ticket.get("ticket_number"))
        sent = {"chat": False, "email": False}
    chain.log(log_run)
    return sent


async def _current_job(data: WorkflowData, event: Event, status: str) -> dict[str, Any] | None:
    """The job, re-read: the event says what happened, the database decides."""
    job_id = str(event.data.get("job_id") or "")
    try:
        uuid.UUID(job_id)
    except ValueError:
        return None
    job = await data.job(job_id)
    return job if job is not None and job["status"] == status else None


# ---------- wiring ----------


async def _payment_paid(event: Event) -> None:
    await on_payment_paid(event)


async def _job_status_changed(event: Event) -> None:
    await on_job_status_changed(event)


async def _job_completed(event: Event) -> None:
    await on_job_completed(event)


async def _job_rejected(event: Event) -> None:
    await on_job_rejected(event)


async def _ticket_updated(event: Event) -> None:
    if event.data.get("status") == "resolved":
        await on_ticket_resolved(event)


def register(events: EventBus | None = None) -> None:
    """Subscribe the workflows to their events (main.py's lifespan). Calling it twice is harmless:
    the bus keeps one copy of a handler."""
    target = events or bus
    target.on(PAYMENT_PAID, _payment_paid)
    target.on(JOB_STATUS_CHANGED, _job_status_changed)
    target.on(JOB_COMPLETED, _job_completed)
    target.on(JOB_REJECTED, _job_rejected)
    target.on(TICKET_UPDATED, _ticket_updated)