"""The stage backups (§10, §15): POST /api/dev/simulate and GET /api/dev/channels. Development only."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.tickets import Channel, Priority, TicketStatus


class SimulateIn(BaseModel):
    """A fake inbound message, handled as if the channel's adapter had received it."""

    channel: Channel
    text: str = Field(min_length=1, max_length=4000)
    external_user_id: str | None = Field(
        None, min_length=1, max_length=200,
        description="The sender's account on the channel (Telegram or Discord user id, email address, web session "
                    "id). Leave it out for a new simulated customer; send back the one a response gave to go on "
                    "with that conversation. A real chat id reaches that chat when its adapter runs here.")
    external_thread_id: str | None = Field(None, min_length=1, max_length=200,
                                           description="The conversation's thread; external_user_id when left out.")
    display_name: str | None = Field(None, max_length=80)
    email: EmailStr | None = Field(
        None, description="web: the pre-chat form's email, which links the customer. email: the sender's address "
                          "when external_user_id is left out.")
    subject: str | None = Field(None, max_length=200, description="email only")

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class SimulatedReply(BaseModel):
    """A reply intake queued for the simulated message."""

    message_id: uuid.UUID
    text: str
    status: Literal["pending", "sent", "failed"] | None  # its outbox row; null when it has none
    simulated: bool  # taken by this process's simulated sink, not sent on a real channel
    last_error: str | None
    created_at: datetime


class SimulatedTicket(BaseModel):
    id: uuid.UUID
    ticket_number: str
    title: str
    status: TicketStatus
    priority: Priority
    flags: list[str]


class SimulateOut(BaseModel):
    channel: Channel
    external_user_id: str  # send these back to go on with the same conversation
    external_thread_id: str
    # Where this channel's replies go in this process: its adapter, the simulated sink, or nowhere yet
    # (switched on but not connected: they wait in the outbox and are retried).
    delivery: Literal["adapter", "simulated", "none"]
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID  # the stored inbound message
    awaiting: str | None  # what intake waits for on this conversation, e.g. serial_number
    ticket: SimulatedTicket | None  # the conversation's ticket after intake
    replies: list[SimulatedReply]
    intake_error: str | None  # intake failed: the reply is the channel layer's fallback


class DevChannels(BaseModel):
    """Which channel adapters this process runs (§15 checklist)."""

    connected: list[Channel]  # running here: their replies go out for real
    enabled: list[Channel]  # switched on here (ENABLE_*; web is always on); enabled and not connected is still connecting
    simulated: list[Channel]  # switched off here: replies go to the simulated sink
