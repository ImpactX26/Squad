from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

# The event names of ARCHITECTURE.md §9: nothing else is published.
EventType = Literal[
    "message.received",
    "message.sent",
    "ticket.created",
    "ticket.updated",
    "ticket.followup",
    "agent.tool_called",
    "payment.link_sent",
    "payment.utr_submitted",
    "payment.paid",
    "payment.failed",
    "job.assigned",
    "job.status_changed",
    "job.completed",
    "job.rejected",
    "stock.low",
    "notification.created",
]
EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))


class StaffEvent(BaseModel):
    """One event, as /ws/staff sends it: {type, data, ts}."""

    type: EventType
    data: dict[str, Any]
    ts: datetime


class InternalEventIn(BaseModel):
    """What an MCP server POSTs to /internal/events."""

    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
