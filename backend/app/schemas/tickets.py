import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# Vocabularies of db/schema.sql (§8.1).
Channel = Literal["discord", "telegram", "email", "web"]
TicketStatus = Literal["new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed"]
Priority = Literal["low", "medium", "high", "urgent"]
TicketCategory = Literal["hardware", "software", "unknown"]


class RowCustomer(BaseModel):
    id: uuid.UUID
    name: str | None


class RowDevice(BaseModel):
    model_name: str
    serial_number: str
    color: str | None
    category: str  # laptop | desktop | headphones | accessory: the row's icon


class TicketRow(BaseModel):
    """One inbox row (§11.3)."""

    id: uuid.UUID
    ticket_number: str
    title: str
    status: TicketStatus
    priority: Priority
    category: TicketCategory
    issue_type: str
    source_channel: Channel
    channels: list[Channel]  # every channel the conversation crossed, first one first
    customer: RowCustomer
    device: RowDevice | None  # null while the device is unverified
    ai_summary: str | None
    duplicate_count: int  # the "+N" of merged follow-ups
    flags: list[str]
    assigned_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class TicketList(BaseModel):
    tickets: list[TicketRow]
    total: int
    limit: int
    offset: int
