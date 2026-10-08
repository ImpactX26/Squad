"""GET /api/tickets: the inbox (ARCHITECTURE.md §10, §11.3). Agents and admins."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_roles
from app.core.db import get_session
from app.models import StaffUser
from app.schemas.tickets import Channel, Priority, RowCustomer, RowDevice, TicketCategory, TicketList, TicketRow, TicketStatus

router = APIRouter()

PRIORITY_ORDER = "CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"

ROW_SELECT = """
    SELECT t.id, t.ticket_number, t.title, t.status, t.priority, t.category, t.issue_type, t.source_channel,
           t.ai_summary, t.duplicate_count, t.flags, t.assigned_agent_id, t.created_at, t.updated_at,
           c.id AS customer_id, c.full_name AS customer_name,
           m.name AS model_name, m.category AS device_category, p.serial_number, p.color,
           ARRAY(
             SELECT ch FROM (
               SELECT t.source_channel AS ch, t.created_at AS at
               UNION ALL
               SELECT msg.channel, msg.created_at FROM messages msg
               WHERE msg.ticket_id = t.id AND msg.channel <> 'internal'
             ) seen GROUP BY ch ORDER BY min(at)
           ) AS channels
    FROM tickets t
    JOIN customers c ON c.id = t.customer_id
    LEFT JOIN products p ON p.id = t.product_id
    LEFT JOIN product_models m ON m.id = p.model_id
"""


@router.get("/api/tickets", response_model=TicketList, responses={401: {}, 403: {}})
async def list_tickets(
    status: TicketStatus | Literal["open"] | None = Query(None, description="A status, or open: not resolved or closed"),
    priority: Priority | None = None,
    channel: Channel | None = Query(None, description="Tickets whose conversation crossed this channel"),
    category: TicketCategory | None = None,
    assignee: uuid.UUID | Literal["me", "unassigned"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: StaffUser = Depends(require_roles("agent", "admin")),
    session: AsyncSession = Depends(get_session),
) -> TicketList:
    """The inbox, highest priority first, then the most recently updated."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if status == "open":
        where.append("t.status NOT IN ('resolved', 'closed')")
    elif status:
        where.append("t.status = :status")
        params["status"] = status
    if priority:
        where.append("t.priority = :priority")
        params["priority"] = priority
    if channel:
        where.append("(t.source_channel = :channel OR EXISTS ("
                     "SELECT 1 FROM messages msg WHERE msg.ticket_id = t.id AND msg.channel = :channel))")
        params["channel"] = channel
    if category:
        where.append("t.category = :category")
        params["category"] = category
    if assignee == "unassigned":
        where.append("t.assigned_agent_id IS NULL")
    elif assignee is not None:
        where.append("t.assigned_agent_id = :assignee")
        params["assignee"] = staff.id if assignee == "me" else assignee
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = await session.scalar(
        text(f"SELECT count(*) FROM tickets t {clause}"), params)
    rows = (await session.execute(
        text(f"{ROW_SELECT} {clause} ORDER BY {PRIORITY_ORDER}, t.updated_at DESC, t.ticket_number DESC "
             "LIMIT :limit OFFSET :offset"),
        {**params, "limit": limit, "offset": offset},
    )).mappings().all()
    return TicketList(tickets=[_row(r) for r in rows], total=total or 0, limit=limit, offset=offset)


def _row(r: Any) -> TicketRow:
    device = None
    if r["serial_number"] is not None:
        device = RowDevice(model_name=r["model_name"], serial_number=r["serial_number"], color=r["color"],
                           category=r["device_category"])
    return TicketRow(
        id=r["id"], ticket_number=r["ticket_number"], title=r["title"], status=r["status"], priority=r["priority"],
        category=r["category"], issue_type=r["issue_type"], source_channel=r["source_channel"],
        channels=list(r["channels"]), customer=RowCustomer(id=r["customer_id"], name=r["customer_name"]),
        device=device, ai_summary=r["ai_summary"], duplicate_count=r["duplicate_count"], flags=list(r["flags"]),
        assigned_agent_id=r["assigned_agent_id"], created_at=r["created_at"], updated_at=r["updated_at"],
    )
