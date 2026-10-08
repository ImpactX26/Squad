"""Ticket endpoints (ARCHITECTURE.md §10).

The inbox list, and everything the ticket page reads and writes: the full ticket, the unified
timeline across channels, status/priority/assignee, the Writer's polish preview, the composer,
the diagnostics checklist, and an admin's "mark as paid" on the payment card. Search lives in
app/api/search.py, slash commands and suggestions in app/api/commands.py.
"""

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.jobs import ticket_job
from app.brain.mcp_hub import McpToolError, hub
from app.brain.workflows import WorkflowData, notify_ticket_deleted
from app.brain.writer import polish
from app.channels.base import registry
from app.channels.dispatcher import dispatcher, outcome_of, queue_staff_reply
from app.core.db import get_session
from app.core.events import bus
from app.core.security import CurrentUser, require_roles
from app.models import StaffUser
from app.schemas.tickets import (
    PRIORITY_ORDER,
    DiagnosticPatch,
    DiagnosticStepOut,
    MarkPaidRequest,
    MarkPaidResponse,
    PolishRequest,
    PolishResponse,
    SendMessageRequest,
    SendMessageResponse,
    SourceChannel,
    TicketAgent,
    TicketCategory,
    TicketCustomer,
    TicketDetail,
    TicketListItem,
    TicketListResponse,
    TicketPatch,
    TicketPaymentOut,
    TicketPriority,
    TicketProduct,
    TicketStatus,
    TimelineEntry,
    TimelineResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["tickets"])

MAX_LIMIT = 100

# What one inbox row needs (_item below): the ticket, its customer, device and agent.
_ITEM_COLUMNS = """
       t.id, t.ticket_number, t.title, t.ai_summary, t.status, t.priority, t.category,
       t.issue_type, t.source_channel, t.flags, t.duplicate_count, t.created_at, t.updated_at,
       cust.id AS customer_id, cust.full_name AS customer_name, cust.email AS customer_email,
       prod.id AS product_id, prod.serial_number AS product_serial,
       pm.model_number AS product_model_number, pm.name AS product_model_name,
       (SELECT array_agg(DISTINCT m.channel) FROM messages m
        WHERE m.ticket_id = t.id AND m.channel <> 'internal') AS channels,
       pm.category AS product_category, prod.color AS product_color,
       prod.warranty_until AS product_warranty_until,
       agent.id AS agent_id, agent.name AS agent_name"""
_ITEM_FROM = """
FROM tickets t
LEFT JOIN customers cust      ON cust.id = t.customer_id
LEFT JOIN products prod       ON prod.id = t.product_id
LEFT JOIN product_models pm   ON pm.id = prod.model_id
LEFT JOIN staff_users agent   ON agent.id = t.assigned_agent_id
"""

# One round trip: the page of rows and the unpaged total together, via a window function.
_LIST = text(f"""
SELECT {_ITEM_COLUMNS},
       count(*) OVER () AS total
{_ITEM_FROM}
WHERE (CAST(:status AS text[])   IS NULL OR t.status = ANY(CAST(:status AS text[])))
  AND (CAST(:priority AS text[]) IS NULL OR t.priority = ANY(CAST(:priority AS text[])))
  AND (CAST(:channel AS text[])  IS NULL OR t.source_channel = ANY(CAST(:channel AS text[])))
  AND (CAST(:category AS text[]) IS NULL OR t.category = ANY(CAST(:category AS text[])))
  AND (NOT :unassigned OR t.assigned_agent_id IS NULL)
  AND (CAST(:assignee AS uuid) IS NULL OR t.assigned_agent_id = CAST(:assignee AS uuid))
  AND (NOT :open_only OR t.status NOT IN ('resolved', 'closed'))
ORDER BY array_position(CAST(:priority_order AS text[]), t.priority), t.updated_at DESC
LIMIT :limit OFFSET :offset
""")


@router.get("/tickets", response_model=TicketListResponse)
async def list_tickets(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[list[TicketStatus] | None, Query(description="Repeat to match any")] = None,
    priority: Annotated[list[TicketPriority] | None, Query()] = None,
    channel: Annotated[list[SourceChannel] | None, Query()] = None,
    category: Annotated[list[TicketCategory] | None, Query()] = None,
    assignee: Annotated[str | None, Query(description='A staff user id, "me", or "unassigned"')] = None,
    open_only: Annotated[bool, Query(description="Hide resolved and closed")] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketListResponse:
    """The inbox: the §10 filters, sorted by priority then most recently updated."""
    assignee_id, unassigned = _assignee(assignee, user.id)
    rows = (await session.execute(_LIST, {
        "status": list(status) if status else None,
        "priority": list(priority) if priority else None,
        "channel": list(channel) if channel else None,
        "category": list(category) if category else None,
        "assignee": str(assignee_id) if assignee_id else None,
        "unassigned": unassigned,
        "open_only": open_only,
        "priority_order": PRIORITY_ORDER,
        "limit": limit,
        "offset": offset,
    })).mappings().all()
    return TicketListResponse(
        tickets=[_item(row) for row in rows],
        total=int(rows[0]["total"]) if rows else 0,
        limit=limit,
        offset=offset,
    )


_BY_IDS = text(f"SELECT {_ITEM_COLUMNS} {_ITEM_FROM} WHERE t.id = ANY(CAST(:ids AS uuid[]))")


async def load_items(session: AsyncSession, ids: list[str]) -> dict[str, TicketListItem]:
    """Inbox rows for these tickets, by id (search results reuse the inbox's ticket cards, §7.4)."""
    if not ids:
        return {}
    rows = (await session.execute(_BY_IDS, {"ids": ids})).mappings().all()
    return {str(row["id"]): _item(row) for row in rows}


def _assignee(assignee: str | None, me: uuid.UUID) -> tuple[uuid.UUID | None, bool]:
    """`assignee` as (staff id, unassigned-only). An unparseable id matches nothing but me."""
    if not assignee:
        return None, False
    if assignee == "unassigned":
        return None, True
    if assignee == "me":
        return me, False
    try:
        return uuid.UUID(assignee), False
    except ValueError:
        log.info("ignoring unparseable assignee filter %r", assignee[:60])
        return None, False


def _channels(source: str, seen: list[str] | None) -> list[str]:
    """The source channel first, then every other platform a message on this ticket used."""
    return [source, *sorted(c for c in (seen or []) if c != source)]


def _item(row: Any) -> TicketListItem:
    return TicketListItem(
        id=row["id"],
        ticket_number=row["ticket_number"],
        title=row["title"],
        ai_summary=row["ai_summary"],
        status=row["status"],
        priority=row["priority"],
        category=row["category"],
        issue_type=row["issue_type"],
        source_channel=row["source_channel"],
        channels=_channels(row["source_channel"], row["channels"]),
        flags=list(row["flags"] or []),
        duplicate_count=row["duplicate_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        customer=TicketCustomer(
            id=row["customer_id"], full_name=row["customer_name"], email=row["customer_email"],
        ) if row["customer_id"] else None,
        product=TicketProduct(
            id=row["product_id"], serial_number=row["product_serial"],
            model_number=row["product_model_number"], model_name=row["product_model_name"],
            category=row["product_category"], color=row["product_color"],
            warranty_until=row["product_warranty_until"],
        ) if row["product_id"] else None,
        assigned_agent=TicketAgent(
            id=row["agent_id"], name=row["agent_name"],
        ) if row["agent_id"] else None,
    )


# ---------- the ticket page (§10) ----------

_DETAIL = text("""
SELECT t.id, t.ticket_number, t.title, t.description, t.ai_summary, t.status, t.priority,
       t.category, t.issue_type, t.source_channel, t.flags, t.duplicate_count,
       t.created_at, t.updated_at, t.resolved_at,
       cust.id AS customer_id, cust.full_name AS customer_name, cust.email AS customer_email,
       prod.id AS product_id, prod.serial_number AS product_serial,
       pm.model_number AS product_model_number, pm.name AS product_model_name,
       (SELECT array_agg(DISTINCT m.channel) FROM messages m
        WHERE m.ticket_id = t.id AND m.channel <> 'internal') AS channels,
       pm.category AS product_category, prod.color AS product_color,
       prod.warranty_until AS product_warranty_until,
       agent.id AS agent_id, agent.name AS agent_name
FROM tickets t
LEFT JOIN customers cust    ON cust.id = t.customer_id
LEFT JOIN products prod     ON prod.id = t.product_id
LEFT JOIN product_models pm ON pm.id = prod.model_id
LEFT JOIN staff_users agent ON agent.id = t.assigned_agent_id
WHERE t.id = :ticket_id
""")

_STEPS = text("""
SELECT id, position, step, suggested_by, result, notes, updated_at
FROM diagnostic_steps WHERE ticket_id = :ticket_id ORDER BY position
""")

# The live payment, if any (§7.6). The job comes from app/api/jobs.py (§7.7).
_PAYMENT = text("""
SELECT id, service_code, amount, currency, status, public_token, expires_at, paid_at,
       invoice_number, utr, utr_submitted_at, verified_by, verified_at, needs_review
FROM payments WHERE ticket_id = :ticket_id ORDER BY created_at DESC LIMIT 1
""")

# Messages and events in one list. Each message carries the channel it arrived on or went out by,
# which is what makes the timeline read as one conversation across platforms (§6.3).
_TIMELINE = text("""
SELECT 'message' AS kind, m.id, m.created_at AS at, m.channel, m.sender_type,
       u.name AS sender_name, m.body, m.body_original, m.is_internal_note,
       m.external_message_id, NULL AS type, NULL AS actor, CAST('{}' AS jsonb) AS payload
FROM messages m LEFT JOIN staff_users u ON u.id = m.sender_staff_id
WHERE m.ticket_id = :ticket_id
UNION ALL
SELECT 'event' AS kind, e.id, e.created_at AS at, NULL AS channel, NULL AS sender_type,
       NULL AS sender_name, NULL AS body, NULL AS body_original, FALSE AS is_internal_note,
       NULL AS external_message_id, e.type, e.actor, e.payload
FROM ticket_events e
WHERE e.ticket_id = :ticket_id
ORDER BY at, kind
""")


async def _load(session: AsyncSession, ticket_id: uuid.UUID) -> Any:
    row = (await session.execute(_DETAIL, {"ticket_id": ticket_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return row


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(
    ticket_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketDetail:
    """Ticket, customer, device, summary, diagnostics, payment and job (§10)."""
    row = await _load(session, ticket_id)
    steps = (await session.execute(_STEPS, {"ticket_id": ticket_id})).mappings().all()
    payment = (await session.execute(_PAYMENT, {"ticket_id": ticket_id})).mappings().one_or_none()
    job = await ticket_job(session, ticket_id)
    return TicketDetail(
        **_item(row).model_dump(),
        description=row["description"],
        resolved_at=row["resolved_at"],
        diagnostic_steps=[DiagnosticStepOut.model_validate(dict(step)) for step in steps],
        payment=TicketPaymentOut(**dict(payment)) if payment else None,
        job=job,
    )


@router.get("/tickets/{ticket_id}/timeline", response_model=TimelineResponse)
async def get_timeline(
    ticket_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TimelineResponse:
    """Messages and events merged, across every channel the ticket has been touched on (§10)."""
    row = await _load(session, ticket_id)
    rows = (await session.execute(_TIMELINE, {"ticket_id": ticket_id})).mappings().all()
    entries = [
        TimelineEntry(
            kind=r["kind"], id=r["id"], at=r["at"], channel=r["channel"],
            sender_type=r["sender_type"], sender_name=r["sender_name"], body=r["body"],
            body_original=r["body_original"], is_internal_note=r["is_internal_note"],
            external_message_id=r["external_message_id"],
            type=r["type"], actor=r["actor"], payload=r["payload"] or {},
        )
        for r in rows
    ]
    channels = sorted({e.channel for e in entries if e.channel and e.channel != "internal"})
    return TimelineResponse(
        ticket_id=row["id"], ticket_number=row["ticket_number"],
        channels=channels, entries=entries,
    )


@router.patch("/tickets/{ticket_id}", response_model=TicketDetail)
async def patch_ticket(
    ticket_id: uuid.UUID,
    body: TicketPatch,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketDetail:
    """Status, priority or assignee (§10). Every change is recorded on the timeline."""
    await _load(session, ticket_id)
    changes: dict[str, Any] = {}
    if body.status is not None:
        changes["status"] = body.status
    if body.priority is not None:
        changes["priority"] = body.priority
    if body.unassign:
        changes["assigned_agent_id"] = None
    elif body.assigned_agent_id is not None:
        changes["assigned_agent_id"] = body.assigned_agent_id
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Give status, priority, assigned_agent_id or unassign")

    assignments = ", ".join(f"{column} = :{column}" for column in changes)
    # Keep resolved_at in step with status, the way tickets.update_status does (§5.1).
    resolved = (", resolved_at = CASE WHEN :status IN ('resolved','closed') THEN now()"
                " ELSE resolved_at END") if "status" in changes else ""
    await session.execute(
        text(f"UPDATE tickets SET {assignments}, updated_at = now(){resolved} WHERE id = :ticket_id"),
        {**changes, "ticket_id": ticket_id})
    await session.execute(text(
        "INSERT INTO ticket_events (ticket_id, type, payload, actor)"
        " VALUES (:ticket_id, :type, CAST(:payload AS jsonb), :actor)"
    ), {
        "ticket_id": ticket_id,
        "type": "status_changed" if "status" in changes else "updated",
        "payload": json.dumps({
            **{k: (str(v) if v is not None else None) for k, v in changes.items()},
            "note": body.note,
        }),
        "actor": str(user.id),
    })
    await session.commit()

    await bus.publish("ticket.updated", {
        "ticket_id": str(ticket_id), "by": str(user.id),
        **{k: (str(v) if v is not None else None) for k, v in changes.items()},
    })
    return await get_ticket(ticket_id, user, session)


@router.post("/tickets/{ticket_id}/polish", response_model=PolishResponse)
async def polish_reply(
    ticket_id: uuid.UUID,
    body: PolishRequest,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PolishResponse:
    """The Writer's preview (§7.3). Never fails: a model outage returns the agent's own text."""
    row = await _load(session, ticket_id)
    context = f"{row['product_model_name'] or 'device'}: {row['title']}"
    result = await polish(body.text, ticket_id=ticket_id, context=context)
    return PolishResponse(
        polished=result.text, original=result.original, was_polished=result.polished,
        reason=result.reason, model=result.model, latency_ms=result.latency_ms,
        warning=result.warning,
    )


@router.post("/tickets/{ticket_id}/messages", response_model=SendMessageResponse,
             status_code=status.HTTP_201_CREATED)
async def send_message(
    ticket_id: uuid.UUID,
    body: SendMessageRequest,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SendMessageResponse:
    """Send the agent's reply on the customer's own channel, or store an internal note (§7.3)."""
    await _load(session, ticket_id)
    queued = await queue_staff_reply(
        ticket_id, body.text, sender_staff_id=user.id,
        body_original=body.original, internal_note=body.internal_note,
    )
    if queued is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ticket has no customer conversation to reply on.")

    if body.internal_note:
        return SendMessageResponse(
            message_id=queued["message_id"], ticket_id=ticket_id, channel="internal",
            queued=False, delivered=False)

    # Deliver now rather than on the dispatcher's next tick, so the agent sees it land. The
    # answer comes from the outbox row, not from this call: the background loop may well have
    # delivered it first, and the agent still needs to be told it went out.
    sink_marker = registry.sink.count
    await dispatcher.deliver_pending(conversation_id=queued["conversation_id"])
    outcome = await outcome_of(queued["outbox_id"], sink_marker=sink_marker)
    return SendMessageResponse(
        message_id=queued["message_id"],
        ticket_id=ticket_id,
        channel=queued["channel"],
        queued=True,
        delivered=outcome.ok,
        simulated=outcome.simulated,
        error=outcome.error,
    )


@router.patch("/tickets/{ticket_id}/diagnostics/{step_id}", response_model=DiagnosticStepOut)
async def patch_diagnostic(
    ticket_id: uuid.UUID,
    step_id: uuid.UUID,
    body: DiagnosticPatch,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DiagnosticStepOut:
    """Mark a step worked, failed or skipped (§10): the "what was tried" record (§5.1)."""
    row = (await session.execute(text(
        "UPDATE diagnostic_steps SET result = :result,"
        " notes = COALESCE(:notes, notes), updated_at = now()"
        " WHERE id = :step_id AND ticket_id = :ticket_id"
        " RETURNING id, position, step, suggested_by, result, notes, updated_at"
    ), {"result": body.result, "notes": body.notes, "step_id": step_id,
        "ticket_id": ticket_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such diagnostic step on this ticket")
    await session.execute(text("UPDATE tickets SET updated_at = now() WHERE id = :id"),
                          {"id": ticket_id})
    await session.commit()
    await bus.publish("ticket.updated", {
        "ticket_id": str(ticket_id), "diagnostic_step_id": str(step_id),
        "result": body.result, "by": str(user.id),
    })
    return DiagnosticStepOut.model_validate(dict(row))


# ---------- an admin's "mark as paid" (§5.5, §7.6) ----------

# payments.mark_paid_manually's refusals, as HTTP. The role is checked here first, so not_admin
# should never come back; it is mapped anyway rather than trusted not to.
_MARK_PAID_REFUSALS: dict[str, tuple[int, str]] = {
    "not_found": (status.HTTP_404_NOT_FOUND, "No such payment."),
    "invalid_id": (status.HTTP_404_NOT_FOUND, "No such payment."),
    "already_paid": (status.HTTP_409_CONFLICT, "This payment is already paid."),
    "cancelled": (status.HTTP_409_CONFLICT, "This payment was cancelled, so it can't be marked paid."),
    "refunded": (status.HTTP_409_CONFLICT, "This payment was refunded, so it can't be marked paid."),
    "note_required": (status.HTTP_422_UNPROCESSABLE_CONTENT,
                      "Say what you checked, e.g. 'UTR seen on the bank statement'."),
    "not_admin": (status.HTTP_403_FORBIDDEN, "Only an admin can mark a payment paid."),
    "staff_not_found": (status.HTTP_403_FORBIDDEN, "Only an admin can mark a payment paid."),
}


@router.post("/payments/{payment_id}/mark-paid", response_model=MarkPaidResponse, responses={
    403: {"description": "Not an admin"}, 404: {"description": "No such payment"},
    409: {"description": "Already paid, cancelled or refunded"}, 422: {"description": "No note"},
    503: {"description": "Payments unavailable"}})
async def mark_payment_paid(
    payment_id: uuid.UUID,
    body: MarkPaidRequest,
    admin: Annotated[StaffUser, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MarkPaidResponse:
    """The admin backup when a bank alert never arrives (§7.6): payments.mark_paid_manually, with the
    signed-in admin's id and their note. Emits payment.paid, so the receipt workflow runs as usual."""
    admin_id = str(admin.id)
    # The login check left a transaction open on this session; don't hold it across the tool call.
    await session.rollback()
    try:
        result = await hub.call_tool("payments__mark_paid_manually", {
            "payment_id": str(payment_id), "staff_user_id": admin_id, "note": body.note})
    except McpToolError as e:
        log.error("mark_paid_manually unavailable: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payments are unavailable right now. Try again in a minute.") from e
    if not isinstance(result, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payments gave an unexpected answer. Try again in a minute.")
    error = "already_paid" if result.get("ok") and result.get("already_paid") else \
        None if result.get("ok") else str(result.get("error") or "unknown")
    if error is not None:
        code, detail = _MARK_PAID_REFUSALS.get(
            error, (status.HTTP_409_CONFLICT, str(result.get("message") or "That payment can't be marked paid.")))
        raise HTTPException(status_code=code, detail=detail)
    return MarkPaidResponse(
        payment_id=result["payment_id"], ticket_id=result["ticket_id"],
        invoice_number=result.get("invoice_number"), status=result["status"],
        paid_at=result.get("paid_at"), verified_by=result.get("verified_by"),
    )


# ---------- deleting a ticket (§10) ----------

# Settled first, so a delete never strands stock or money: an open job has its part reserved, and a
# payment being verified may have money on the way. FOR UPDATE also holds back a workflow that is about
# to attach a job or a message (an insert that references the ticket waits for this lock, then fails).
_DELETE_CHECK = text("""
SELECT t.ticket_number,
       EXISTS (SELECT 1 FROM service_jobs j WHERE j.ticket_id = t.id
               AND j.status IN ('assigned', 'accepted', 'en_route', 'on_site')) AS open_job,
       EXISTS (SELECT 1 FROM payments p WHERE p.ticket_id = t.id AND p.status = 'verifying') AS verifying
FROM tickets t WHERE t.id = CAST(:id AS uuid)
FOR UPDATE OF t
""")

# Children before parents. The ticket takes its messages (and their outbox rows), payments (and the
# bank alerts matched to them), closed jobs, AI runs and the notifications that link to it;
# ticket_events and diagnostic_steps go by ON DELETE CASCADE. Stock movements stay, unlinked, so the
# inventory history still adds up. A conversation is unlinked and its slot-filling state cleared, and
# deleted only when nothing is left in it (unless it is the one the customer is told on, §7.9).
# Customers, products and addresses are never touched.
_DELETE_STEPS: tuple[tuple[str, str], ...] = (
    ("notifications", "DELETE FROM notifications WHERE link LIKE '%' || :id || '%'"),
    ("outbox", "DELETE FROM outbox WHERE payload->>'ticket_id' = :id"
               " OR message_id IN (SELECT id FROM messages WHERE ticket_id = CAST(:id AS uuid))"),
    ("messages", "DELETE FROM messages WHERE ticket_id = CAST(:id AS uuid)"),
    ("conversations_unlinked", "UPDATE conversations SET ticket_id = NULL, context = '{}'::jsonb"
                               " WHERE ticket_id = CAST(:id AS uuid)"),
    ("inventory_movements_unlinked", "UPDATE inventory_movements SET ticket_id = NULL, job_id = NULL"
                                     " WHERE ticket_id = CAST(:id AS uuid) OR job_id IN"
                                     " (SELECT id FROM service_jobs WHERE ticket_id = CAST(:id AS uuid))"),
    ("service_jobs", "DELETE FROM service_jobs WHERE ticket_id = CAST(:id AS uuid)"),
    ("bank_alerts", "DELETE FROM bank_alerts WHERE matched_payment_id IN"
                    " (SELECT id FROM payments WHERE ticket_id = CAST(:id AS uuid))"),
    ("payments", "DELETE FROM payments WHERE ticket_id = CAST(:id AS uuid)"),
    ("ai_runs", "DELETE FROM ai_runs WHERE ticket_id = CAST(:id AS uuid)"),
    ("tickets", "DELETE FROM tickets WHERE id = CAST(:id AS uuid)"),
)
# The conversations the ticket left empty. Run after the ticket's messages are gone.
_EMPTY_CONVERSATIONS = text("""
DELETE FROM conversations c
WHERE c.id = ANY(CAST(:ids AS uuid[]))
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
  AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.conversation_id = c.id)
""")


class TicketNotDeletable(Exception):
    """Why this ticket can't be deleted yet; the message is for the agent."""


async def delete_ticket_rows(session: AsyncSession, ticket_id: uuid.UUID, *,
                             keep_conversation: str | None = None) -> dict[str, Any] | None:
    """Delete one ticket and what belongs only to it, in the caller's transaction (not committed).

    Returns {ticket_number, rows: {what: count}}, or None when there is no such ticket.
    Raises TicketNotDeletable while a job is open or a payment is being verified.
    `keep_conversation` survives even when left empty: the customer is about to be told on it.
    """
    params = {"id": str(ticket_id)}
    check = (await session.execute(_DELETE_CHECK, params)).mappings().first()
    if check is None:
        return None
    if check["open_job"]:
        raise TicketNotDeletable(
            f"{check['ticket_number']} has an open technician job with a part reserved. Cancel the job first.")
    if check["verifying"]:
        raise TicketNotDeletable(
            f"{check['ticket_number']} has a payment being verified. Mark it paid or reject it first.")
    conversations = [row[0] for row in (await session.execute(text(
        "SELECT id FROM conversations WHERE ticket_id = CAST(:id AS uuid)"
        " OR id IN (SELECT conversation_id FROM messages WHERE ticket_id = CAST(:id AS uuid))"), params)).all()]
    rows: dict[str, int] = {}
    for name, sql in _DELETE_STEPS:
        rows[name] = (await session.execute(text(sql), params)).rowcount
    rows["conversations_deleted"] = (await session.execute(
        _EMPTY_CONVERSATIONS, {"ids": [str(c) for c in conversations if str(c) != str(keep_conversation)]})).rowcount
    return {"ticket_number": check["ticket_number"], "rows": rows}


@router.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT, responses={
    403: {"description": "Not an agent or admin"}, 404: {"description": "No such ticket"},
    409: {"description": "An open job or a payment being verified"}})
async def delete_ticket(
    ticket_id: uuid.UUID,
    staff: Annotated[StaffUser, Depends(require_roles("agent", "admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete a ticket for good, with its timeline, messages, payments and closed jobs (§10).

    Agents and admins. Refused (409) while a technician job is open or a payment is being verified.
    Once it's gone, the customer is told by email and on their own channel (§7.9).
    """
    staff_id, staff_name = str(staff.id), staff.name
    data = WorkflowData()
    # Read before the delete: afterwards there is no ticket to say what it was or whom to tell.
    ticket = await data.resolution(str(ticket_id))
    conversation = await data.customer_conversation(str(ticket_id), ticket["customer_id"]) if ticket else None
    try:
        deleted = await delete_ticket_rows(
            session, ticket_id, keep_conversation=str(conversation["conversation_id"]) if conversation else None)
    except TicketNotDeletable as e:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    if deleted is None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such ticket.")
    await session.commit()
    log.info("ticket %s deleted by %s (%s): %s", deleted["ticket_number"], staff_name, staff_id,
             json.dumps(deleted["rows"]))
    if ticket is not None:
        told = await notify_ticket_deleted(ticket, conversation)
        log.info("ticket %s: customer told by chat %s, by email %s", deleted["ticket_number"], told["chat"],
                 told["email"])
    # The inbox and any open copy of the ticket page refetch on this; the page then shows "removed".
    await bus.publish("ticket.updated", {"ticket_id": str(ticket_id), "reason": "ticket_deleted",
                                         "ticket_number": deleted["ticket_number"]})