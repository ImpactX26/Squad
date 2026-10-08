"""tickets MCP server (ARCHITECTURE.md §5.1) — :8101.

Create, find, update, and search tickets. Loads fastembed (app.brain.embeddings) because
ticket embeddings power duplicate detection (§7.2) and hybrid search (§7.4).
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from app.brain.decide import extract_serial
from app.brain.embeddings import aembed_texts
from app.core.config import get_settings
from mcp_servers import run
from mcp_servers.common import db, events

log = logging.getLogger(__name__)
mcp = MCPServer(name="tickets", instructions=__doc__)

PRIORITIES = ["low", "medium", "high", "urgent"]
# The §8.1 tickets.flags vocabulary. create_ticket rejects anything else, so a typo
# in a flag name can never reach the agent-facing ticket.
TICKET_FLAGS = frozenset({"unverified_product", "ownership_mismatch", "out_of_warranty"})

_COLS = """t.id AS ticket_id, t.ticket_number, t.customer_id, t.product_id, t.source_channel,
       t.category, t.issue_type, t.title, t.description, t.ai_summary, t.status, t.priority,
       t.duplicate_count, t.flags, t.assigned_agent_id, t.created_at, t.updated_at, t.resolved_at,
       t.status NOT IN ('resolved','closed') AS open,
       c.full_name AS customer_name, m.name AS device, p.serial_number"""
# The readable names ride along with the ids, so a model never has only a UUID to show a person.
_FROM = """ FROM tickets t JOIN customers c ON c.id = t.customer_id
LEFT JOIN products p ON p.id = t.product_id LEFT JOIN product_models m ON m.id = p.model_id"""
_TICKET = f"SELECT {_COLS}{_FROM}"


def _select(extra_column: str = "") -> str:
    """The ticket SELECT, optionally with one scoring column. Keeps the extra column before FROM."""
    return f"SELECT {_COLS}{',' + extra_column if extra_column else ''}{_FROM}"


async def _embed(text: str) -> str:
    return db.to_vector_literal((await aembed_texts([text]))[0])


async def _event(type: str, ticket: dict[str, Any], **extra: Any) -> None:
    await events.publish(type, {
        "ticket_id": ticket["ticket_id"], "ticket_number": ticket["ticket_number"],
        "status": ticket.get("status"), "priority": ticket.get("priority"), **extra,
    })


async def _add_ticket_event(conn, ticket_id: str, type: str, actor: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1,$2,$3,$4)",
        ticket_id, type, payload, actor,
    )


@mcp.tool()
async def create_ticket(
    customer_id: str,
    category: Literal["hardware", "software", "unknown"],
    issue_type: str,
    title: str,
    description: str,
    source_channel: Literal["discord", "telegram", "email", "web"],
    product_id: str | None = None,
    conversation_id: str | None = None,
    flags: list[str] | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Create a ticket with its embedding and a ticket.created event (§7.1). Returns the ticket_number.

    `flags` are the §8.1 agent-facing flags: unverified_product (the serial was never confirmed,
    §7.1), ownership_mismatch (the unit is registered to someone else, §6.3), out_of_warranty.
    `priority` overrides the "medium" default, for intake's urgency (§4.4).
    """
    bad = sorted({f for f in (flags or []) if f not in TICKET_FLAGS})
    if bad:
        return {"ok": False, "error": f"unknown flags {bad}; allowed: {sorted(TICKET_FLAGS)}"}
    if priority is not None and priority not in PRIORITIES:
        return {"ok": False, "error": f"unknown priority {priority!r}; allowed: {PRIORITIES}"}
    vector = await _embed(f"{title}\n{description}")
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO tickets (customer_id, product_id, source_channel, category, issue_type,"
                " title, description, embedding, flags, priority)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9::text[],COALESCE($10,'medium'))"
                " RETURNING id AS ticket_id, ticket_number, status, priority, flags",
                customer_id, product_id, source_channel, category, issue_type, title, description, vector,
                sorted(set(flags or [])), priority,
            )
            ticket = db.row_to_dict(row)
            await _add_ticket_event(conn, ticket["ticket_id"], "created", "ai", {"source_channel": source_channel})
            if conversation_id:
                await conn.execute("UPDATE conversations SET ticket_id = $2 WHERE id = $1",
                                   conversation_id, ticket["ticket_id"])
    await _event("ticket.created", ticket, customer_id=customer_id, title=title,
                 source_channel=source_channel, flags=ticket.get("flags") or [])
    return ticket


@mcp.tool()
async def find_similar_tickets(
    customer_id: str, text: str, product_id: str | None = None, limit: int = 3
) -> dict[str, Any]:
    """Open tickets for the same customer and product, ranked by cosine similarity (§7.2)."""
    vector = await _embed(text)
    rows = await db.fetch(
        _select(" 1 - (t.embedding <=> $1::vector) AS similarity") + """
        WHERE t.customer_id = $2::uuid
          AND ($3::uuid IS NULL OR t.product_id = $3::uuid)
          AND t.status NOT IN ('resolved','closed')
          AND t.embedding IS NOT NULL
          AND t.created_at > now() - make_interval(days => $4)
        ORDER BY t.embedding <=> $1::vector
        LIMIT $5""",
        vector, customer_id, product_id, get_settings().duplicate_lookback_days, limit,
    )
    return {"matches": db.rows_to_list(rows), "threshold": get_settings().duplicate_similarity_threshold}


@mcp.tool()
async def add_followup(
    ticket_id: str, message_id: str | None = None, channel: str | None = None
) -> dict[str, Any]:
    """Duplicate handling (§7.2): link the message, bump duplicate_count, raise priority every 2 follow-ups."""
    async with db.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT ticket_number, duplicate_count, priority FROM tickets WHERE id = $1 FOR UPDATE", ticket_id)
            if current is None:
                return {"ok": False, "error": "ticket not found", "ticket_id": ticket_id}
            count = current["duplicate_count"] + 1
            priority = current["priority"]
            raised = count % 2 == 0 and priority != "urgent"
            if raised:
                priority = PRIORITIES[min(PRIORITIES.index(priority) + 1, len(PRIORITIES) - 1)]
            row = await conn.fetchrow(
                "UPDATE tickets SET duplicate_count = $2, priority = $3, updated_at = now()"
                " WHERE id = $1 RETURNING id AS ticket_id, ticket_number, status, priority, duplicate_count",
                ticket_id, count, priority,
            )
            if message_id:
                await conn.execute("UPDATE messages SET ticket_id = $2 WHERE id = $1", message_id, ticket_id)
            note = f"Customer followed up via {channel}" if channel else "Customer followed up"
            await _add_ticket_event(conn, ticket_id, "followup", "customer", {"note": note, "channel": channel})
            if raised:
                await _add_ticket_event(conn, ticket_id, "priority_raised", "system", {"priority": priority})
    ticket = db.row_to_dict(row)
    await _event("ticket.followup", ticket, channel=channel, priority_raised=raised)
    return {"ok": True, **ticket, "priority_raised": raised}


@mcp.tool()
async def add_message(
    body: str,
    ticket_id: str | None = None,
    conversation_id: str | None = None,
    sender_type: Literal["customer", "agent", "ai", "technician", "system"] = "ai",
    body_original: str | None = None,
) -> dict[str, Any]:
    """Append a message to the ticket timeline (§5.1). The channel comes from the conversation."""
    if not body.strip():
        return {"ok": False, "error": "body is empty"}
    channel = "internal"
    if conversation_id:
        found = await db.fetchval("SELECT channel FROM conversations WHERE id = $1", conversation_id)
        if found is None:
            return {"ok": False, "error": "conversation not found", "conversation_id": conversation_id}
        channel = found
    row = await db.fetchrow(
        "INSERT INTO messages (conversation_id, ticket_id, sender_type, channel, body, body_original)"
        " VALUES ($1,$2,$3,$4,$5,$6) RETURNING id AS message_id, created_at",
        conversation_id, ticket_id, sender_type, channel, body, body_original,
    )
    return {"ok": True, **db.row_to_dict(row), "channel": channel}


@mcp.tool()
async def update_status(
    ticket_id: str,
    status: Literal["new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed"],
    note: str | None = None,
    customer_told: bool = False,
) -> dict[str, Any]:
    """Change a ticket's status and record it on the timeline (§5.1).

    `customer_told`: the caller has already sent the customer its own closing chat message (/close, a
    completed job), so the resolved notification (§7.9) sends only the email, not a second message.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE tickets SET status = $2, updated_at = now(),"
                " resolved_at = CASE WHEN $2 IN ('resolved','closed') THEN now() ELSE resolved_at END"
                " WHERE id = $1 RETURNING id AS ticket_id, ticket_number, status, priority",
                ticket_id, status,
            )
            if row is None:
                return {"ok": False, "error": "ticket not found", "ticket_id": ticket_id}
            await _add_ticket_event(conn, ticket_id, "status_changed", "ai", {"status": status, "note": note})
    ticket = db.row_to_dict(row)
    await _event("ticket.updated", ticket, note=note, customer_told=customer_told)
    return {"ok": True, **ticket}


@mcp.tool()
async def get_ticket(ticket_id: str | None = None, ticket_number: str | None = None) -> dict[str, Any]:
    """One ticket with its product, customer, and recent timeline. Give ticket_id or ticket_number."""
    if not ticket_id and not ticket_number:
        return {"found": False, "error": "give ticket_id or ticket_number"}
    row = await db.fetchrow(
        _TICKET + " WHERE ($1::uuid IS NULL OR t.id = $1::uuid)"
                  " AND ($2::text IS NULL OR t.ticket_number = $2::text)",
        ticket_id, ticket_number,
    )
    if row is None:
        return {"found": False, "ticket_id": ticket_id, "ticket_number": ticket_number}
    ticket = db.row_to_dict(row)
    tid = ticket["ticket_id"]
    customer = await db.fetchrow(
        "SELECT id AS customer_id, full_name, email, phone FROM customers WHERE id = $1", ticket["customer_id"])
    product = None
    if ticket["product_id"]:
        product = await db.fetchrow(
            "SELECT p.id AS product_id, p.serial_number, p.color, p.warranty_until,"
            " m.model_number, m.name AS model_name, m.category"
            " FROM products p JOIN product_models m ON m.id = p.model_id WHERE p.id = $1",
            ticket["product_id"],
        )
    messages = await db.fetch(
        "SELECT id AS message_id, sender_type, channel, body, created_at FROM messages"
        " WHERE ticket_id = $1 ORDER BY created_at DESC LIMIT 20", tid)
    timeline = await db.fetch(
        "SELECT id AS event_id, type, payload, actor, created_at FROM ticket_events"
        " WHERE ticket_id = $1 ORDER BY created_at DESC LIMIT 20", tid)
    steps = await db.fetch(
        "SELECT id AS step_id, position, step, suggested_by, result, notes FROM diagnostic_steps"
        " WHERE ticket_id = $1 ORDER BY position", tid)
    return {
        "found": True, **ticket,
        "customer": db.row_to_dict(customer),
        "product": db.row_to_dict(product),
        "messages": list(reversed(db.rows_to_list(messages))),
        "events": list(reversed(db.rows_to_list(timeline))),
        "diagnostic_steps": db.rows_to_list(steps),
    }


@mcp.tool()
async def search_tickets(
    query: str = "", filters: dict[str, Any] | None = None, limit: int = 20
) -> dict[str, Any]:
    """Hybrid search: SQL filters + full-text ts_rank + pgvector similarity, merged by RRF (§7.4).

    filters keys: status, priority, issue_type, category, source_channel, customer_id,
    assigned_agent_id, created_after, created_before, min_duplicate_count, open_only.
    """
    filters = filters or {}
    applied, ignored_values = _normal_filters(filters)
    where, args = _filter_sql(applied)
    # Said back, so a model that invented a filter learns it did nothing.
    ignored = sorted(k for k in filters if k not in FILTER_KEYS)
    extra: dict[str, Any] = {"ignored_filters": ignored} if ignored else {}
    if ignored_values:
        extra["ignored_values"] = ignored_values
    limit = max(1, min(limit, 50))
    pool_size = limit * 3
    n = len(args)

    if serial := extract_serial(query):
        # A serial in the query names one device: its tickets, newest first (the words around it don't rank).
        rows = await db.fetch(
            _TICKET + f" WHERE {where} AND p.serial_number = ${n + 1} ORDER BY t.updated_at DESC LIMIT ${n + 2}",
            *args, serial, limit)
        return {"query": query, "filters": filters, "serial_number": serial, "results": _brief(db.rows_to_list(rows)),
                "ranking": "serial", **extra}

    if not query.strip():
        rows = await db.fetch(
            _TICKET + f" WHERE {where} ORDER BY t.updated_at DESC LIMIT ${n + 1}", *args, limit)
        return {"query": query, "filters": filters, "results": _brief(db.rows_to_list(rows)), "ranking": "recency",
                **extra}

    text_rows = await db.fetch(
        _select(f" ts_rank(t.search_tsv, plainto_tsquery('english', ${n + 1})) AS text_score")
        + f" WHERE {where} AND t.search_tsv @@ plainto_tsquery('english', ${n + 1})"
        f" ORDER BY text_score DESC LIMIT ${n + 2}",
        *args, query, pool_size,
    )
    vector = await _embed(query)
    vector_rows = await db.fetch(
        _select(f" 1 - (t.embedding <=> ${n + 1}::vector) AS similarity")
        + f" WHERE {where} AND t.embedding IS NOT NULL"
        f" ORDER BY t.embedding <=> ${n + 1}::vector LIMIT ${n + 2}",
        *args, vector, pool_size,
    )
    merged = _reciprocal_rank_fusion(db.rows_to_list(text_rows), db.rows_to_list(vector_rows))
    return {"query": query, "filters": filters, "results": _brief(merged[:limit]), "ranking": "rrf", **extra}


# What a search row carries: enough to list and pick a ticket. Every tool round re-sends earlier results,
# and Groq's free tier allows ~8K tokens a minute, so the long text and the internal ids stay in get_ticket.
BRIEF_DROPPED = ("description", "ai_summary", "customer_id", "product_id", "assigned_agent_id", "resolved_at",
                 "category", "text_score")


def _brief(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search rows without the long text and internal ids (§4.5); get_ticket has it all."""
    for row in rows:
        for key in BRIEF_DROPPED:
            row.pop(key, None)
        if not row.get("flags"):
            row.pop("flags", None)
        if not row.get("duplicate_count"):
            row.pop("duplicate_count", None)
        for key in ("created_at", "updated_at"):
            if isinstance(row.get(key), str):
                row[key] = row[key][:10]  # the date is what a list shows
        for key in ("similarity", "rrf_score"):
            if isinstance(row.get(key), float):
                row[key] = round(row[key], 3)
    return rows


GROUP_BY_COLUMNS = ("status", "priority", "issue_type", "category", "source_channel")


@mcp.tool()
async def count_tickets(filters: dict[str, Any] | None = None, group_by: str | None = None) -> dict[str, Any]:
    """How many tickets match: the total, open vs resolved/closed, and per value of one column (count and open). Read-only.

    filters: the search_tickets keys. group_by: status, priority, issue_type, category, or source_channel.
    """
    filters = filters or {}
    if group_by is not None and group_by not in GROUP_BY_COLUMNS:
        return {"ok": False, "error": "bad_group_by", "message": f"group_by must be one of {', '.join(GROUP_BY_COLUMNS)}"}
    applied, ignored_values = _normal_filters(filters)
    where, args = _filter_sql(applied)
    ignored = sorted(k for k in filters if k not in FILTER_KEYS)
    row = await db.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE t.status NOT IN ('resolved','closed')) AS open"
        f" FROM tickets t WHERE {where}", *args)
    out: dict[str, Any] = {"ok": True, "filters": filters, "total": row["total"], "open": row["open"],
                           "resolved_or_closed": row["total"] - row["open"]}
    if group_by:
        # The column comes from GROUP_BY_COLUMNS above, never from the caller's text.
        # Each group says how many are open too, so "open, by issue type" is right whatever filters were sent.
        groups = await db.fetch(
            f"SELECT t.{group_by} AS value, count(*) AS count,"
            " count(*) FILTER (WHERE t.status NOT IN ('resolved','closed')) AS open"
            f" FROM tickets t WHERE {where} GROUP BY 1 ORDER BY 2 DESC, 1", *args)
        out |= {"group_by": group_by, "groups": db.rows_to_list(groups)}
    if ignored:
        out["ignored_filters"] = ignored
    if ignored_values:
        out["ignored_values"] = ignored_values
    return out


FILTER_KEYS = frozenset({"status", "open_only", "priority", "issue_type", "category", "source_channel", "customer_id",
                         "assigned_agent_id", "created_after", "created_before", "min_duplicate_count"})
RRF_K = 60  # standard reciprocal-rank-fusion damping: score = sum(1 / (k + rank))


def _reciprocal_rank_fusion(*ranked_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    best: dict[str, dict[str, Any]] = {}
    matched: dict[str, list[str]] = {}
    for name, rows in zip(("full-text", "similarity"), ranked_lists):
        for rank, row in enumerate(rows, start=1):
            tid = row["ticket_id"]
            scores[tid] = scores.get(tid, 0.0) + 1.0 / (RRF_K + rank)
            matched.setdefault(tid, []).append(name)
            best[tid] = {**best.get(tid, {}), **row}
    out = []
    for tid, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        out.append({**best[tid], "rrf_score": round(score, 6), "matched_by": matched[tid]})
    return out


TICKET_STATUSES = ("new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed")


def _normal_filters(filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Status words a model writes but the schema doesn't have: "open" means open_only; any other
    unknown status is dropped and said back (ignored_values), never matched as zero tickets."""
    if not filters.get("status"):
        return filters, {}
    wanted = [s.strip().lower().replace(" ", "_") for s in _as_list(filters["status"])]
    out = {k: v for k, v in filters.items() if k != "status"}
    if "open" in wanted:
        out["open_only"] = True
    if known := [s for s in wanted if s in TICKET_STATUSES]:
        out["status"] = known
    unknown = [s for s in wanted if s not in TICKET_STATUSES and s != "open"]
    return out, ({"status": unknown} if unknown else {})


def _filter_sql(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    """WHERE clause and positional args for the §7.4 filter keys. Unknown keys are ignored."""
    clauses: list[str] = ["TRUE"]
    args: list[Any] = []

    def add(sql: str, value: Any) -> None:
        args.append(value)
        clauses.append(sql.format(n=len(args)))

    if filters.get("status"):
        add("t.status = ANY(${n}::text[])", _as_list(filters["status"]))
    if filters.get("open_only"):
        clauses.append("t.status NOT IN ('resolved','closed')")
    if filters.get("priority"):
        add("t.priority = ANY(${n}::text[])", _as_list(filters["priority"]))
    if filters.get("issue_type"):
        add("t.issue_type = ANY(${n}::text[])", _as_list(filters["issue_type"]))
    if filters.get("category"):
        add("t.category = ANY(${n}::text[])", _as_list(filters["category"]))
    if filters.get("source_channel"):
        add("t.source_channel = ANY(${n}::text[])", _as_list(filters["source_channel"]))
    if filters.get("customer_id"):
        add("t.customer_id = ${n}::uuid", str(filters["customer_id"]))
    if filters.get("assigned_agent_id"):
        add("t.assigned_agent_id = ${n}::uuid", str(filters["assigned_agent_id"]))
    # asyncpg binds a timestamptz only from a datetime, never from an ISO string.
    if (after := _when(filters.get("created_after"))) is not None:
        add("t.created_at >= ${n}::timestamptz", after)
    if (before := _when(filters.get("created_before"))) is not None:
        add("t.created_at <= ${n}::timestamptz", before)
    if filters.get("min_duplicate_count") is not None:
        add("t.duplicate_count >= ${n}::int", int(filters["min_duplicate_count"]))
    return " AND ".join(clauses), args


def _as_list(value: Any) -> list[str]:
    return [str(value)] if isinstance(value, str) else [str(v) for v in value]


def _when(value: Any) -> datetime | None:
    """An ISO date or datetime as an aware datetime (UTC when it has no zone); None when absent or invalid."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        log.warning("search_tickets: ignoring the unreadable date %r", str(value)[:40])
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@mcp.tool()
async def record_diagnostic(
    ticket_id: str,
    step: str,
    result: Literal["pending", "worked", "failed", "skipped"] = "pending",
    notes: str | None = None,
) -> dict[str, Any]:
    """Record what was tried and whether it worked (§5.1).

    Updates the matching existing step when there is one (playbook steps are inserted first,
    §7.1), otherwise appends a new agent-suggested step.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id FROM diagnostic_steps WHERE ticket_id = $1 AND lower(step) = lower($2) LIMIT 1",
                ticket_id, step)
            if existing:
                row = await conn.fetchrow(
                    "UPDATE diagnostic_steps SET result = $2, notes = COALESCE($3, notes), updated_at = now()"
                    " WHERE id = $1 RETURNING id AS step_id, position, step, result, notes, suggested_by",
                    existing["id"], result, notes)
            else:
                position = await conn.fetchval(
                    "SELECT COALESCE(max(position), 0) + 1 FROM diagnostic_steps WHERE ticket_id = $1", ticket_id)
                row = await conn.fetchrow(
                    "INSERT INTO diagnostic_steps (ticket_id, position, step, suggested_by, result, notes)"
                    " VALUES ($1,$2,$3,'agent',$4,$5)"
                    " RETURNING id AS step_id, position, step, result, notes, suggested_by",
                    ticket_id, position, step, result, notes)
    return {"ok": True, **db.row_to_dict(row)}


@mcp.tool()
async def set_diagnostic_plan(
    ticket_id: str,
    steps: list[str],
    suggested_by: Literal["ai", "playbook"] = "ai",
) -> dict[str, Any]:
    """Store the first diagnostic plan for a ticket, in order, in one call (§7.1).

    record_diagnostic is for one step's outcome ("what worked / what didn't"); this writes the
    whole opening plan the brain wrote from the playbook. Steps already on the ticket are skipped
    (compared case-insensitively) and the rest are appended after the highest position, so running
    intake twice on one ticket doesn't duplicate the plan.
    """
    wanted = [text for step in steps if (text := step.strip())]
    if not wanted:
        return {"ok": False, "error": "steps is empty"}

    async with db.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM tickets WHERE id = $1", ticket_id) is None:
                return {"ok": False, "error": "ticket not found", "ticket_id": ticket_id}
            existing = await conn.fetch(
                "SELECT step, position FROM diagnostic_steps WHERE ticket_id = $1 FOR UPDATE", ticket_id)
            already = {row["step"].strip().lower() for row in existing}
            position = max((row["position"] for row in existing), default=0)
            new_steps: list[tuple[int, str]] = []
            for text in wanted:
                if text.lower() in already:
                    continue
                already.add(text.lower())
                position += 1
                new_steps.append((position, text))
            if new_steps:
                await conn.executemany(
                    "INSERT INTO diagnostic_steps (ticket_id, position, step, suggested_by)"
                    " VALUES ($1,$2,$3,$4)",
                    [(ticket_id, pos, text, suggested_by) for pos, text in new_steps],
                )
            rows = await conn.fetch(
                "SELECT id AS step_id, position, step, suggested_by, result FROM diagnostic_steps"
                " WHERE ticket_id = $1 ORDER BY position", ticket_id)
    return {
        "ok": True, "ticket_id": ticket_id, "added": len(new_steps),
        "skipped": len(wanted) - len(new_steps), "diagnostic_steps": db.rows_to_list(rows),
    }


DIAGNOSTIC_RESULTS = ("worked", "failed", "skipped")
MAX_RESULT_NOTE = 200


@mcp.tool()
async def record_diagnostic_results(ticket_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """The customer's answer to /diagnose-send (§7.1 diagnostic_feedback): several steps' outcomes at once.

    `results` is [{step_id, result (worked | failed | skipped), notes}]. A step that isn't this ticket's,
    or a result outside those three, is ignored and returned in `ignored`. suggested_by is unchanged.
    One diagnostic_results_recorded timeline event (actor customer); a ticket awaiting the customer goes
    back to in_progress; ticket.updated lets the open checklist tick itself.
    """
    wanted: dict[str, tuple[str, str | None]] = {}
    ignored: list[dict[str, Any]] = []
    for item in results or []:
        step_id, result = str((item or {}).get("step_id") or ""), str((item or {}).get("result") or "")
        if result not in DIAGNOSTIC_RESULTS or not step_id or step_id in wanted:
            ignored.append({"step_id": step_id, "result": result, "why": "bad or repeated result"})
            continue
        notes = " ".join(str(item.get("notes") or "").split())[:MAX_RESULT_NOTE] or None
        wanted[step_id] = (result, notes)

    async with db.acquire() as conn:
        async with conn.transaction():
            ticket = await conn.fetchrow(
                "SELECT id, ticket_number, status, priority FROM tickets WHERE id = $1 FOR UPDATE", ticket_id)
            if ticket is None:
                return {"ok": False, "error": "ticket not found", "ticket_id": ticket_id}
            steps = {str(row["id"]): row for row in await conn.fetch(
                "SELECT id, position, step FROM diagnostic_steps WHERE ticket_id = $1", ticket_id)}
            recorded: list[dict[str, Any]] = []
            for step_id, (result, notes) in wanted.items():
                if step_id not in steps:
                    ignored.append({"step_id": step_id, "result": result, "why": "not a step of this ticket"})
                    continue
                row = await conn.fetchrow(
                    "UPDATE diagnostic_steps SET result = $2, notes = COALESCE($3, notes), updated_at = now()"
                    " WHERE id = $1 RETURNING id AS step_id, position, step, result, notes, suggested_by",
                    steps[step_id]["id"], result, notes)
                recorded.append(db.row_to_dict(row))
            if not recorded:
                return {"ok": False, "error": "nothing to record", "ignored": ignored}
            await _add_ticket_event(conn, ticket_id, "diagnostic_results_recorded", "customer", {
                "results": [{k: r[k] for k in ("step_id", "position", "step", "result", "notes")} for r in recorded],
                "note": "; ".join(f"{r['position']}. {r['step']}: {r['result']}" for r in recorded)[:500]})
            status = ticket["status"]
            if status == "awaiting_customer":
                status = "in_progress"
                await conn.execute("UPDATE tickets SET status = 'in_progress', updated_at = now() WHERE id = $1",
                                   ticket_id)
                await _add_ticket_event(conn, ticket_id, "status_changed", "customer", {
                    "status": "in_progress", "note": "The customer answered the diagnostic steps"})
            else:
                await conn.execute("UPDATE tickets SET updated_at = now() WHERE id = $1", ticket_id)
    await _event("ticket.updated", {"ticket_id": str(ticket["id"]), "ticket_number": ticket["ticket_number"],
                                    "status": status, "priority": ticket["priority"]},
                 diagnostics=[r["step_id"] for r in recorded])
    return {"ok": True, "ticket_id": str(ticket["id"]), "status": status, "recorded": recorded, "ignored": ignored}


@mcp.tool()
async def update_summary(ticket_id: str, summary: str) -> dict[str, Any]:
    """Refresh the AI summary, and the embedding that search and duplicate detection use."""
    row = await db.fetchrow("SELECT title, description FROM tickets WHERE id = $1", ticket_id)
    if row is None:
        return {"ok": False, "error": "ticket not found", "ticket_id": ticket_id}
    vector = await _embed(f"{row['title']}\n{row['description']}\n{summary}")
    updated = await db.fetchrow(
        "UPDATE tickets SET ai_summary = $2, embedding = $3::vector, updated_at = now()"
        " WHERE id = $1 RETURNING id AS ticket_id, ticket_number, status, priority",
        ticket_id, summary, vector,
    )
    ticket = db.row_to_dict(updated)
    await _event("ticket.updated", ticket, ai_summary=summary)
    return {"ok": True, **ticket}


if __name__ == "__main__":
    run(mcp, "tickets")