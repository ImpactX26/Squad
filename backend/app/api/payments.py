"""The payments page's API (ARCHITECTURE.md §7.6, §10, §11.2): every payment, by hand.

The automation stays the main path: payments.submit_utr from the pay page and the UPI verifier's
bank-alert match (app/payments/upi_verifier.py). This is the manual path beside it, and every write
goes through the same payments MCP tools (or, for a pasted bank SMS, the same reader and matcher),
never straight SQL, so there is one code path for each change.

    GET   /api/payments                 the list (agents and admins): status, needs review, text
    GET   /api/payments/{id}            the drawer: line items, timeline events, bank alerts
    POST  /api/payments                 an admin's "New payment request" (create_payment_request)
    PATCH /api/payments/{id}            {utr?, expires_in_minutes?, note}: correct_utr_manually /
                                        extend_payment
    POST  /api/payments/{id}/reject     reject_payment: a verifying payment becomes failed
    POST  /api/payments/{id}/cancel     cancel_payment: never a paid one (that is a refund)
    GET   /api/bank-alerts              every bank_alerts row and the payment it matched
    POST  /api/bank-alerts              an admin pastes a bank SMS: upi_verifier.ingest_manual

Writes are admin-only, each with a note saying what was checked, and the timeline names the admin.
Nothing is ever deleted: a financial record is cancelled. POST /api/payments/{id}/mark-paid stays
in app/api/tickets.py.
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain.commands import CommandData, CommandTools, send_payment_link
from app.brain.mcp_hub import McpToolError, hub
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.events import bus
from app.core.security import require_roles
from app.models import StaffUser
from app.payments.invoice import load_invoice
from app.payments.money import UTR, money_str
from app.payments.upi_verifier import ingest_manual, match_for_utr
from app.schemas.payments import (
    BankAlertCreate,
    BankAlertList,
    BankAlertOut,
    BankAlertResult,
    PaymentCreate,
    PaymentCreated,
    PaymentDetail,
    PaymentEvent,
    PaymentListResponse,
    PaymentNote,
    PaymentPatch,
    PaymentRow,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["payments"])

Staff = Annotated[StaffUser, Depends(require_roles("agent", "admin"))]
Admin = Annotated[StaffUser, Depends(require_roles("admin"))]
Session = Annotated[AsyncSession, Depends(get_session)]

# The link message and the invoice email of a "New payment request", through the same gate as /payments.
LINK_TOOLS: frozenset[str] = frozenset({"messaging__send_reply", "messaging__send_email"})
TRIGGER = "payments_page"
BANK_ALERT_LIMIT = 200

# The payments tools' refusals, as HTTP. Anything else is a 409 with the tool's own message.
_REFUSALS: dict[str, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid_id": status.HTTP_404_NOT_FOUND,
    "ticket_not_found": status.HTTP_404_NOT_FOUND,
    "note_required": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_utr": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_minutes": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "bad_status": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "unknown_service": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_admin": status.HTTP_403_FORBIDDEN,
    "staff_not_found": status.HTTP_403_FORBIDDEN,
}

_EVENTS = text("""
SELECT e.id, e.type, e.actor, e.payload, e.created_at, s.name AS actor_name
FROM ticket_events e LEFT JOIN staff_users s ON CAST(s.id AS text) = e.actor
WHERE e.ticket_id = CAST(:t AS uuid) AND e.payload->>'payment_id' = :p
ORDER BY e.created_at
""")

_ALERTS = """
SELECT a.id, a.sender, a.utr, a.amount, a.parsed_ok, a.reject_reason, a.matched_payment_id, a.received_at,
       a.processed_at, p.invoice_number AS matched_invoice_number, p.ticket_id AS matched_ticket_id,
       t.ticket_number AS matched_ticket_number, s.name AS added_by
FROM bank_alerts a
LEFT JOIN payments p     ON p.id = a.matched_payment_id
LEFT JOIN tickets t      ON t.id = p.ticket_id
LEFT JOIN staff_users s  ON a.sender = 'admin:' || CAST(s.id AS text) AND a.gmail_message_id LIKE 'manual-%'
"""


async def _tool(session: AsyncSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One payments tool call. The login check left a transaction open; it isn't held across the call."""
    await session.rollback()
    try:
        result = await hub.call_tool(name, args)
    except McpToolError as e:
        log.error("%s unavailable: %s", name, e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payments are unavailable right now. Try again in a minute.") from e
    if not isinstance(result, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payments gave an unexpected answer. Try again in a minute.")
    if result.get("ok") is False:
        error = str(result.get("error") or "refused")
        raise HTTPException(status_code=_REFUSALS.get(error, status.HTTP_409_CONFLICT),
                            detail=str(result.get("message") or error))
    return result


def _row(p: dict[str, Any]) -> PaymentRow:
    verified_by = p.get("verified_by")
    label = "bank alert" if verified_by == "bank_alert" else (p.get("verified_by_name") or verified_by)
    return PaymentRow(**{key: p.get(key) for key in PaymentRow.model_fields if key != "verified_by_label"},
                      verified_by_label=label)


def _alert(row: Any) -> BankAlertOut:
    data = dict(row)
    data["amount"] = money_str(data["amount"]) if data["amount"] is not None else None
    return BankAlertOut(**data)


async def _one(session: AsyncSession, payment_id: uuid.UUID) -> dict[str, Any]:
    listed = await _tool(session, "payments__list_payments", {"payment_id": str(payment_id), "limit": 1})
    if not listed.get("payments"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such payment.")
    return listed["payments"][0]


# ---------- reads (agents and admins) ----------


@router.get("/payments", response_model=PaymentListResponse, responses={503: {"description": "Payments unavailable"}})
async def list_payments(
    user: Staff,
    session: Session,
    status_: Annotated[str | None, Query(alias="status")] = None,
    needs_review: bool | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaymentListResponse:
    """Every payment, newest first (payments.list_payments)."""
    services = (await session.execute(text("SELECT code, name FROM service_catalog ORDER BY name"))).mappings().all()
    listed = await _tool(session, "payments__list_payments", {
        "status": status_ or None, "needs_review": needs_review, "query": q, "limit": limit, "offset": offset})
    return PaymentListResponse(payments=[_row(p) for p in listed["payments"]], total=listed["total"],
                               limit=listed["limit"], offset=listed["offset"], services=[dict(s) for s in services])


@router.get("/payments/{payment_id}", response_model=PaymentDetail, responses={404: {"description": "No such payment"}})
async def get_payment(payment_id: uuid.UUID, user: Staff, session: Session) -> PaymentDetail:
    """The drawer: the payment, its line items, its timeline events, and the bank alerts for it or its UTR."""
    p = await _one(session, payment_id)
    events = (await session.execute(_EVENTS, {"t": p["ticket_id"], "p": p["payment_id"]})).mappings().all()
    alerts = (await session.execute(text(
        _ALERTS + " WHERE a.matched_payment_id = CAST(:p AS uuid) OR (CAST(:utr AS text) IS NOT NULL AND a.utr = :utr)"
                  " ORDER BY a.received_at DESC"), {"p": p["payment_id"], "utr": p.get("utr")})).mappings().all()
    return PaymentDetail(
        **_row(p).model_dump(),
        line_items=[{"kind": i.get("kind", ""), "label": i.get("label", ""), "amount": str(i.get("amount", ""))}
                    for i in p.get("line_items") or []],
        events=[PaymentEvent(**dict(e)) for e in events],
        bank_alerts=[_alert(a) for a in alerts],
    )


@router.get("/bank-alerts", response_model=BankAlertList)
async def list_bank_alerts(user: Staff, session: Session,
                           limit: Annotated[int, Query(ge=1, le=BANK_ALERT_LIMIT)] = BANK_ALERT_LIMIT) -> BankAlertList:
    """Every bank_alerts row, newest first: parsed UTR, amount, reject_reason and the payment it matched."""
    rows = (await session.execute(text(_ALERTS + " ORDER BY a.received_at DESC LIMIT :n"), {"n": limit})).mappings()
    return BankAlertList(alerts=[_alert(row) for row in rows])


# ---------- an admin's writes, each with a note ----------


@router.post("/payments", response_model=PaymentCreated, status_code=status.HTTP_201_CREATED, responses={
    404: {"description": "No such ticket"}, 409: {"description": "No address on file, an open payment, free, ..."},
    422: {"description": "No note, or an unknown service"}, 503: {"description": "Payments unavailable"}})
async def create_payment(body: PaymentCreate, admin: Admin, session: Session,
                         settings: Annotated[Settings, Depends(get_settings)]) -> PaymentCreated:
    """"New payment request": payments.create_payment_request for the ticket's customer and the address on
    file, the amount computed in code (§5.5); then the link on the customer's channel and the invoice by
    email, the same words /payments sends."""
    ticket = (await session.execute(text(
        "SELECT t.id, t.customer_id, t.ticket_number, c.full_name FROM tickets t"
        " JOIN customers c ON c.id = t.customer_id"
        " WHERE t.id = CAST(:id AS uuid) OR upper(t.ticket_number) = upper(:number)"),
        {"id": str(body.ticket_id) if body.ticket_id else None,
         "number": (body.ticket_number or "").strip() or None})).mappings().one_or_none()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such ticket.")
    address = (await session.execute(text(
        "SELECT id FROM addresses WHERE customer_id = :c ORDER BY is_default DESC, created_at DESC LIMIT 1"),
        {"c": ticket["customer_id"]})).scalar()
    if address is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=(
            f"{ticket['full_name'] or 'This customer'} has no address on file. Run /payments on "
            f"{ticket['ticket_number']} first: it asks the customer for their details and sends the link."))
    if not settings.upi_configured:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="UPI_ID and UPI_PAYEE_NAME are not set, so a payment link can't be paid.")
    created = await _tool(session, "payments__create_payment_request", {
        "ticket_id": str(ticket["id"]), "customer_id": str(ticket["customer_id"]),
        "service_code": body.service_code.strip(), "address_id": str(address),
        "staff_user_id": str(admin.id), "note": body.note})

    told: str | None = None
    emailed: str | None = None
    invoice = await load_invoice(payment_id=created["payment_id"])
    if invoice is not None:
        conversation = await CommandData().customer_conversation(str(ticket["id"]), str(ticket["customer_id"]))
        tools = CommandTools(LINK_TOOLS, events=bus, trigger=TRIGGER, ticket_id=str(ticket["id"]))
        try:
            sent = await send_payment_link(tools, str(conversation["conversation_id"]) if conversation else None, invoice)
            told = conversation["channel"] if sent and conversation else None
            emailed = invoice["customer"]["email"]
        except Exception:  # the invoice exists either way; the admin sees that the customer wasn't told
            log.exception("payment %s created, but the link couldn't be sent", created.get("invoice_number"))
    return PaymentCreated(payment=_row(await _one(session, uuid.UUID(created["payment_id"]))),
                          told_customer_on=told, emailed_to=emailed)


@router.patch("/payments/{payment_id}", response_model=PaymentRow, responses={
    404: {"description": "No such payment"}, 409: {"description": "Paid, cancelled, used UTR, out of attempts, ..."},
    422: {"description": "No note, nothing to change, or not a 12-digit UTR"}})
async def patch_payment(payment_id: uuid.UUID, body: PaymentPatch, admin: Admin, session: Session) -> PaymentRow:
    """Extend the link (extend_payment) and/or correct the UTR on the customer's behalf
    (correct_utr_manually, the same five attempts), then match the new UTR at once."""
    if body.utr is None and body.expires_in_minutes is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Give a corrected UTR or a new expiry.")
    utr = body.utr.strip() if body.utr is not None else None
    if utr is not None and not UTR.fullmatch(utr):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A UTR is exactly 12 digits.")
    staff = str(admin.id)
    if body.expires_in_minutes is not None:
        await _tool(session, "payments__extend_payment", {
            "payment_id": str(payment_id), "staff_user_id": staff, "expires_in_minutes": body.expires_in_minutes,
            "note": body.note})
    if utr is not None:
        await _tool(session, "payments__correct_utr_manually", {
            "payment_id": str(payment_id), "staff_user_id": staff, "utr": utr, "note": body.note})
        try:  # the bank alert may already be in (§7.6); the verifier's sweep retries if this fails
            await match_for_utr(utr)
        except Exception:
            log.exception("immediate match for a corrected UTR failed; the verifier will retry it")
    return _row(await _one(session, payment_id))


@router.post("/payments/{payment_id}/reject", response_model=PaymentRow, responses={
    404: {"description": "No such payment"}, 409: {"description": "Not verifying"}, 422: {"description": "No note"}})
async def reject_payment(payment_id: uuid.UUID, body: PaymentNote, admin: Admin, session: Session) -> PaymentRow:
    """A verifying payment the bank statement doesn't show: failed, with the admin's reason."""
    await _tool(session, "payments__reject_payment",
                {"payment_id": str(payment_id), "staff_user_id": str(admin.id), "reason": body.note})
    return _row(await _one(session, payment_id))


@router.post("/payments/{payment_id}/cancel", response_model=PaymentRow, responses={
    404: {"description": "No such payment"}, 409: {"description": "Paid, refunded, or already closed"},
    422: {"description": "No note"}})
async def cancel_payment(payment_id: uuid.UUID, body: PaymentNote, admin: Admin, session: Session) -> PaymentRow:
    """Cancel a payment that isn't paid (cancel_payment). A financial record is cancelled, never deleted."""
    result = await _tool(session, "payments__cancel_payment",
                         {"payment_id": str(payment_id), "staff_user_id": str(admin.id), "note": body.note})
    if result.get("already_closed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This payment is already {result['status']}.")
    return _row(await _one(session, payment_id))


@router.post("/bank-alerts", response_model=BankAlertResult, status_code=status.HTTP_201_CREATED, responses={
    422: {"description": "No note, or no SMS"}})
async def add_bank_alert(body: BankAlertCreate, admin: Admin, session: Session) -> BankAlertResult:
    """"Add bank SMS": the pasted SMS runs through the same reader and matcher as a forwarded mail. Sender,
    SPF and the secret aren't checked (the admin is signed in); a match pays as this admin's verification."""
    admin_id, admin_name = str(admin.id), admin.name  # read before the rollback expires them
    await session.rollback()
    source = " ".join(part for part in (
        f"from {body.sender.strip()}" if body.sender and body.sender.strip() else "",
        f"(subject {body.subject.strip()})" if body.subject and body.subject.strip() else "") if part)
    note = f"Bank SMS{' ' + source if source else ''} added by {admin_name}: {' '.join(body.note.split())}"
    result = await ingest_manual(body.sms, staff_id=admin_id, note=note, events=bus)
    match = result.match
    return BankAlertResult(
        bank_alert_id=uuid.UUID(result.alert_id) if result.alert_id else None, parsed_ok=result.verdict.ok,
        reject_reason=result.verdict.reason, utr=result.verdict.utr,
        amount=money_str(result.verdict.amount) if result.verdict.amount is not None else None,
        match=match.status if match else None,
        payment_id=uuid.UUID(match.payment_id) if match and match.payment_id else None,
        invoice_number=match.invoice_number if match else None,
    )