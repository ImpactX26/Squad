"""Request and response models for the ticket endpoints (ARCHITECTURE.md §10, §11.2).

These are the source of the frontend's types: `make types` regenerates
web/src/lib/api-types.ts from the OpenAPI schema this produces.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.jobs import JobOut

TicketStatus = Literal[
    "new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed"
]
TicketPriority = Literal["low", "medium", "high", "urgent"]
TicketCategory = Literal["hardware", "software", "unknown"]
SourceChannel = Literal["discord", "telegram", "email", "web"]
ProductCategory = Literal["laptop", "desktop", "headphones", "accessory"]

# Highest first: the inbox sorts by priority, then by most recently updated (§10).
PRIORITY_ORDER: list[str] = ["urgent", "high", "medium", "low"]


class TicketCustomer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str | None
    email: str | None


class TicketProduct(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    serial_number: str
    model_number: str
    model_name: str
    # §11.3 names the device as "Aurora 14, Silver" behind a category glyph, and the ticket page
    # rail shows the warranty beside the serial. All three come from the joins already made.
    category: ProductCategory
    color: str | None
    warranty_until: date | None


class TicketAgent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class TicketListItem(BaseModel):
    """One row of the inbox list pane (§11.2)."""

    id: uuid.UUID
    ticket_number: str
    title: str
    ai_summary: str | None
    status: TicketStatus
    priority: TicketPriority
    category: TicketCategory
    issue_type: str
    source_channel: SourceChannel
    channels: list[SourceChannel] = Field(
        description="Every platform the conversation has touched, source channel first (§6.3, §11.3)")
    flags: list[str]
    duplicate_count: int
    created_at: datetime
    updated_at: datetime
    customer: TicketCustomer | None
    product: TicketProduct | None
    assigned_agent: TicketAgent | None


class TicketListResponse(BaseModel):
    tickets: list[TicketListItem]
    total: int = Field(description="Matching tickets before limit and offset")
    limit: int
    offset: int


# ---------- the ticket page (§10, §11.2) ----------


class DiagnosticStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    step: str
    suggested_by: Literal["ai", "agent", "playbook"]
    result: Literal["pending", "worked", "failed", "skipped"]
    notes: str | None
    updated_at: datetime


class TicketPaymentOut(BaseModel):
    """The ticket's latest payment, for the right rail (§7.6)."""

    id: uuid.UUID
    service_code: str
    amount: float
    currency: str
    status: str
    public_token: str
    expires_at: datetime
    paid_at: datetime | None
    invoice_number: str | None = None
    utr: str | None = None
    utr_submitted_at: datetime | None = None
    verified_by: str | None = Field(default=None, description="'bank_alert', or the admin who marked it paid")
    verified_at: datetime | None = None
    needs_review: bool = Field(default=False, description="Staff should check it: the UTR waited too long for its bank alert, or the alert's amount differed (§7.6)")


class MarkPaidRequest(BaseModel):
    note: str = Field(min_length=3, max_length=500,
                      description="What the admin checked, e.g. 'UTR seen on the bank statement'")


class MarkPaidResponse(BaseModel):
    """The payment after an admin marked it paid (§5.5 mark_paid_manually)."""

    payment_id: uuid.UUID
    ticket_id: uuid.UUID
    invoice_number: str | None
    status: str
    paid_at: datetime | None
    verified_by: str | None = Field(description="The admin's staff user id")


class TicketDetail(TicketListItem):
    """The full ticket behind /tickets/[id] (§11.2)."""

    description: str
    resolved_at: datetime | None
    diagnostic_steps: list[DiagnosticStepOut]
    payment: TicketPaymentOut | None
    job: JobOut | None = Field(description="The ticket's latest technician job (§7.7)")


class TimelineEntry(BaseModel):
    """One row of the unified timeline: a message or a ticket event (§10)."""

    kind: Literal["message", "event"]
    id: uuid.UUID
    at: datetime
    channel: str | None = Field(default=None, description="Which platform it came from or went to")
    # message
    sender_type: Literal["customer", "agent", "ai", "technician", "system"] | None = None
    sender_name: str | None = None
    body: str | None = None
    body_original: str | None = Field(default=None, description="The agent's note before polish")
    is_internal_note: bool = False
    external_message_id: str | None = None
    # event
    type: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    ticket_id: uuid.UUID
    ticket_number: str
    channels: list[str] = Field(description="Every channel this ticket has been touched on (§6.3)")
    entries: list[TimelineEntry]


class TicketPatch(BaseModel):
    """Only the three fields §10 allows; anything omitted is left alone."""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_agent_id: uuid.UUID | None = None
    unassign: bool = Field(default=False, description="Clear the assignee (assigned_agent_id is ignored)")
    note: str | None = Field(default=None, max_length=500, description="Recorded on the timeline")


class PolishRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class PolishResponse(BaseModel):
    polished: str = Field(description="The rewrite, or the original text when it could not be polished")
    original: str
    was_polished: bool = Field(description="False on a model outage: the text came back untouched")
    reason: str | None = None
    warning: str | None = Field(
        default=None,
        description="Polished, but it mentions something the note did not. Show it to the agent.")
    model: str | None = None
    latency_ms: int


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    original: str | None = Field(default=None, max_length=4000,
                                 description="The agent's note before polish (§7.3 step 4)")
    internal_note: bool = Field(default=False, description="Stored on the ticket, never sent")


class SendMessageResponse(BaseModel):
    message_id: uuid.UUID
    ticket_id: uuid.UUID
    channel: str
    queued: bool = Field(description="False for an internal note, which is never delivered")
    delivered: bool
    simulated: bool = Field(default=False, description="No adapter was running; the sink took it")
    error: str | None = None


class DiagnosticPatch(BaseModel):
    result: Literal["pending", "worked", "failed", "skipped"]
    notes: str | None = Field(default=None, max_length=1000)