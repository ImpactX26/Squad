"""tickets MCP server (:8101), ARCHITECTURE.md §5.1.

Block 1 tools: create_ticket, get_ticket, add_message, update_status, update_summary,
set_diagnostic_plan. The other §5.1 tools (find_similar_tickets, add_followup, search_tickets,
count_tickets, record_diagnostic, record_diagnostic_results) come in later blocks.

Run: uv run python -m mcp_servers.tickets_server
"""

from typing import Any

import asyncpg
from mcp.server import MCPServer

from mcp_servers import serve
from mcp_servers.common import events
from mcp_servers.common.db import connection
from mcp_servers.common.results import dumps, fail, parse_uuid

PORT = 8101

# Vocabularies from db/schema.sql (§8.1).
CHANNELS = ("discord", "telegram", "email", "web")
MESSAGE_CHANNELS = (*CHANNELS, "internal")
CATEGORIES = ("hardware", "software", "unknown")
STATUSES = ("new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed")
PRIORITIES = ("low", "medium", "high", "urgent")
FLAGS = ("unverified_product", "ownership_mismatch", "out_of_warranty")
SENDER_TYPES = ("customer", "agent", "ai", "technician", "system")
SUGGESTED_BY = ("ai", "agent", "playbook")
CLOSED_STATUSES = ("resolved", "closed")

TIMELINE_LIMIT = 20

mcp = MCPServer("tickets", instructions="Support tickets: create, read, add messages, status, summary, diagnostic plan.")


def _check(value: str, allowed: tuple[str, ...], field: str) -> None:
    if value not in allowed:
        raise fail(f"bad_{field}", f"{field} must be one of: {', '.join(allowed)}", field=field, value=value)


async def _ticket_row(conn: asyncpg.Connection, ticket_id: str, *, lock: bool = False) -> asyncpg.Record:
    query = "SELECT id, ticket_number, status FROM tickets WHERE id = $1"
    row = await conn.fetchrow(query + (" FOR UPDATE" if lock else ""), parse_uuid(ticket_id, "ticket_id"))
    if row is None:
        raise fail("not_found", "no ticket with that id", ticket_id=ticket_id)
    return row


@mcp.tool(structured_output=False)
async def create_ticket(
    customer_id: str,
    product_id: str | None,
    category: str,
    issue_type: str,
    title: str,
    description: str,
    source_channel: str,
    conversation_id: str | None = None,
    flags: list[str] | None = None,
    priority: str | None = None,
) -> str:
    """Create a ticket for a customer's issue and return its ticket_number.

    product_id is null when the device is not verified. flags and priority are optional;
    unknown values are rejected and nothing is created. With conversation_id, the conversation and
    its messages not yet on a ticket are linked to this one.
    """
    _check(category, CATEGORIES, "category")
    _check(source_channel, CHANNELS, "source_channel")
    if priority is not None:
        _check(priority, PRIORITIES, "priority")
    flags = list(dict.fromkeys(flags or []))
    unknown = [f for f in flags if f not in FLAGS]
    if unknown:
        raise fail("bad_flags", f"flags must be among: {', '.join(FLAGS)}", unknown=unknown)
    if not title.strip() or not description.strip():
        raise fail("missing_text", "title and description must not be empty")
    customer = parse_uuid(customer_id, "customer_id")
    product = parse_uuid(product_id, "product_id") if product_id else None
    conversation = parse_uuid(conversation_id, "conversation_id") if conversation_id else None

    messages_attached = 0
    async with connection() as conn, conn.transaction():
        try:
            # Not built yet: embeddings. The embedding column stays NULL until Block 2 (§14.3).
            row = await conn.fetchrow(
                """
                INSERT INTO tickets (customer_id, product_id, category, issue_type, title, description,
                                     source_channel, flags, priority)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, ticket_number, status, priority, flags
                """,
                customer, product, category, issue_type.strip() or "other", title.strip(),
                description.strip(), source_channel, flags, priority or "medium",
            )
        except asyncpg.ForeignKeyViolationError as exc:
            field = "product_id" if "product" in (exc.constraint_name or "") else "customer_id"
            raise fail("not_found", f"no such {field.removesuffix('_id')}", field=field) from None
        if conversation is not None:
            linked = await conn.execute(
                "UPDATE conversations SET ticket_id = $1 WHERE id = $2 AND customer_id = $3",
                row["id"], conversation, customer,
            )
            if linked != "UPDATE 1":
                # Raising inside the transaction rolls the ticket back: nothing is created.
                raise fail("not_found", "no conversation with that id for this customer", field="conversation_id")
            # The messages that led here (the customer's first words, the serial asked for) are stored
            # before intake decides their ticket (§6.3): this is it. Messages already on a ticket stay.
            attached = await conn.execute(
                "UPDATE messages SET ticket_id = $1 WHERE conversation_id = $2 AND ticket_id IS NULL",
                row["id"], conversation,
            )
            messages_attached = int(attached.split()[-1])
        await conn.execute(
            "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1, 'created', $2, 'ai')",
            row["id"],
            {"source_channel": source_channel, "priority": row["priority"], "flags": row["flags"]},
        )

    result = {
        "ticket_id": row["id"],
        "ticket_number": row["ticket_number"],
        "status": row["status"],
        "priority": row["priority"],
        "flags": row["flags"],
        "messages_attached": messages_attached,
    }
    await events.publish(
        "ticket.created", {**result, "customer_id": customer, "source_channel": source_channel, "title": title}
    )
    return dumps(result)


@mcp.tool(structured_output=False)
async def get_ticket(ticket_id: str | None = None, ticket_number: str | None = None) -> str:
    """Get one ticket by ticket_id or ticket_number: ticket, product, customer and the recent timeline."""
    if bool(ticket_id) == bool(ticket_number):
        raise fail("bad_arguments", "give exactly one of ticket_id or ticket_number")
    where, key = ("t.id = $1", parse_uuid(ticket_id, "ticket_id")) if ticket_id else (
        "t.ticket_number = $1", ticket_number.strip().upper())

    async with connection() as conn:
        t = await conn.fetchrow(
            f"""
            SELECT t.id, t.ticket_number, t.title, t.description, t.ai_summary, t.status, t.priority,
                   t.category, t.issue_type, t.source_channel, t.flags, t.duplicate_count,
                   t.assigned_agent_id, t.created_at, t.updated_at, t.resolved_at,
                   c.id AS customer_id, c.full_name, c.email, c.phone,
                   p.id AS product_id, p.serial_number, p.color, p.warranty_until,
                   m.model_number, m.name AS model_name, m.category AS model_category
            FROM tickets t
            JOIN customers c ON c.id = t.customer_id
            LEFT JOIN products p ON p.id = t.product_id
            LEFT JOIN product_models m ON m.id = p.model_id
            WHERE {where}
            """,
            key,
        )
        if t is None:
            raise fail("not_found", "no such ticket", ticket_id=ticket_id, ticket_number=ticket_number)
        timeline = await conn.fetch(
            """
            SELECT * FROM (
              SELECT 'message' AS kind, created_at AS at, sender_type, channel, body, is_internal_note,
                     NULL AS type, NULL::jsonb AS payload, NULL AS actor
              FROM messages WHERE ticket_id = $1
              UNION ALL
              SELECT 'event', created_at, NULL, NULL, NULL, NULL, type, payload, actor
              FROM ticket_events WHERE ticket_id = $1
            ) x ORDER BY at DESC LIMIT $2
            """,
            t["id"], TIMELINE_LIMIT,
        )

    product = None
    if t["product_id"] is not None:
        product = {
            "product_id": t["product_id"],
            "serial_number": t["serial_number"],
            "model_number": t["model_number"],
            "model_name": t["model_name"],
            "category": t["model_category"],
            "color": t["color"],
            "warranty_until": t["warranty_until"],
        }
    items = []
    for r in reversed(timeline):  # oldest first
        if r["kind"] == "message":
            items.append({"kind": "message", "at": r["at"], "sender_type": r["sender_type"], "channel": r["channel"],
                          "body": r["body"], "internal": r["is_internal_note"]})
        else:
            items.append({"kind": "event", "at": r["at"], "type": r["type"], "actor": r["actor"],
                          "payload": r["payload"]})
    return dumps({
        "ticket": {k: t[k] for k in (
            "id", "ticket_number", "title", "description", "ai_summary", "status", "priority", "category",
            "issue_type", "source_channel", "flags", "duplicate_count", "assigned_agent_id",
            "created_at", "updated_at", "resolved_at")},
        "customer": {"customer_id": t["customer_id"], "full_name": t["full_name"], "email": t["email"],
                     "phone": t["phone"]},
        "product": product,
        "timeline": items,
    })


@mcp.tool(structured_output=False)
async def add_message(
    ticket_id: str, conversation_id: str | None, sender_type: str, body: str, body_original: str | None = None
) -> str:
    """Append a message to a ticket's timeline. The channel is the conversation's (internal when there is none)."""
    _check(sender_type, SENDER_TYPES, "sender_type")
    if not body.strip():
        raise fail("missing_text", "body must not be empty")
    conversation = parse_uuid(conversation_id, "conversation_id") if conversation_id else None

    async with connection() as conn, conn.transaction():
        ticket = await _ticket_row(conn, ticket_id)
        channel = "internal"
        if conversation is not None:
            channel = await conn.fetchval("SELECT channel FROM conversations WHERE id = $1", conversation)
            if channel is None:
                raise fail("not_found", "no conversation with that id", field="conversation_id")
        message = await conn.fetchrow(
            """
            INSERT INTO messages (conversation_id, ticket_id, sender_type, channel, body, body_original)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at
            """,
            conversation, ticket["id"], sender_type, channel, body, body_original,
        )
        await conn.execute("UPDATE tickets SET updated_at = now() WHERE id = $1", ticket["id"])

    result = {"message_id": message["id"], "ticket_number": ticket["ticket_number"], "channel": channel,
              "created_at": message["created_at"]}
    await events.publish("ticket.updated", {"ticket_id": ticket["id"], "ticket_number": ticket["ticket_number"],
                                            "reason": "message_added", "message_id": message["id"]})
    return dumps(result)


@mcp.tool(structured_output=False)
async def update_status(ticket_id: str, status: str, note: str | None = None) -> str:
    """Change a ticket's status, with an optional note on the timeline."""
    _check(status, STATUSES, "status")

    async with connection() as conn, conn.transaction():
        ticket = await _ticket_row(conn, ticket_id, lock=True)
        previous = ticket["status"]
        if previous == status:
            return dumps({"ticket_number": ticket["ticket_number"], "status": status, "changed": False})
        await conn.execute(
            """
            UPDATE tickets SET status = $2, updated_at = now(),
                   resolved_at = CASE WHEN $2 = ANY($3::text[]) THEN coalesce(resolved_at, now()) ELSE NULL END
            WHERE id = $1
            """,
            ticket["id"], status, list(CLOSED_STATUSES),
        )
        payload: dict[str, Any] = {"from": previous, "to": status}
        if note:
            payload["note"] = note
        await conn.execute(
            "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1, 'status_changed', $2, 'system')",
            ticket["id"], payload,
        )

    await events.publish("ticket.updated", {"ticket_id": ticket["id"], "ticket_number": ticket["ticket_number"],
                                            "reason": "status_changed", "from": previous, "to": status})
    return dumps({"ticket_number": ticket["ticket_number"], "status": status, "previous": previous, "changed": True})


@mcp.tool(structured_output=False)
async def update_summary(ticket_id: str, summary: str) -> str:
    """Replace a ticket's AI summary."""
    if not summary.strip():
        raise fail("missing_text", "summary must not be empty")

    async with connection() as conn:
        ticket = await _ticket_row(conn, ticket_id)
        await conn.execute(
            "UPDATE tickets SET ai_summary = $2, updated_at = now() WHERE id = $1", ticket["id"], summary.strip()
        )

    await events.publish("ticket.updated", {"ticket_id": ticket["id"], "ticket_number": ticket["ticket_number"],
                                            "reason": "summary_updated"})
    return dumps({"ticket_number": ticket["ticket_number"], "updated": True})


@mcp.tool(structured_output=False)
async def set_diagnostic_plan(ticket_id: str, steps: list[str], suggested_by: str = "ai") -> str:
    """Add a ticket's diagnostic plan, in order. Steps already on the ticket are skipped."""
    _check(suggested_by, SUGGESTED_BY, "suggested_by")

    async with connection() as conn, conn.transaction():
        # The row lock keeps two concurrent plans from taking the same positions.
        ticket = await _ticket_row(conn, ticket_id, lock=True)
        existing = await conn.fetch(
            "SELECT step, position FROM diagnostic_steps WHERE ticket_id = $1", ticket["id"]
        )
        seen = {r["step"].strip().casefold() for r in existing}
        position = max((r["position"] for r in existing), default=0)
        added, skipped = [], []
        for step in steps:
            text = step.strip()
            if not text:
                continue
            if text.casefold() in seen:
                skipped.append(text)
                continue
            seen.add(text.casefold())
            position += 1
            step_id = await conn.fetchval(
                """
                INSERT INTO diagnostic_steps (ticket_id, position, step, suggested_by)
                VALUES ($1, $2, $3, $4) RETURNING id
                """,
                ticket["id"], position, text, suggested_by,
            )
            added.append({"step_id": step_id, "position": position, "step": text})

    if added:
        await events.publish("ticket.updated", {"ticket_id": ticket["id"], "ticket_number": ticket["ticket_number"],
                                                "reason": "diagnostic_plan_set"})
    return dumps({"ticket_number": ticket["ticket_number"], "added": added, "skipped": skipped})


if __name__ == "__main__":
    serve(mcp, PORT)
