"""Identity across channels (ARCHITECTURE.md §6.3): who wrote, and in which conversation.

resolve() finds or creates the customer and the conversation for an inbound message and stores the
message, with ticket_id null: which ticket it belongs to is decided after intake runs. The channel
layer owns that messages row and conversations.context, intake's slot-filling state (§7.1); there
is no MCP tool for either, so intake reads the context from resolve() and writes it with
set_context().

Accounts are linked by email only for now: email and web give an address at once. A Telegram or
Discord account starts as a placeholder customer (no email); linking it by serial is Block 2.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import CHANNELS, InboundMessage
from app.core.db import get_sessionmaker
from app.core.events import publish


@dataclass(frozen=True)
class Resolved:
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID  # the stored inbound message (ticket_id null until intake decides)
    channel: str
    external_thread_id: str
    ticket_id: uuid.UUID | None  # the conversation's current ticket, if it has one
    context: dict[str, Any]  # conversations.context: {} or {"awaiting": "serial_number", ...}
    customer_name: str | None
    customer_email: str | None
    new_customer: bool
    new_conversation: bool
    # The same external message was stored before (a channel redelivered it): nothing new was
    # written and no event published. Intake must not run again for it.
    repeat: bool


def normalize_email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if "@" in value else None


def _email_of(inbound: InboundMessage) -> str | None:
    if inbound.channel == "email":
        return normalize_email(inbound.external_user_id)
    if inbound.channel == "web":
        return normalize_email(inbound.raw_meta.get("email"))
    return None


def _check(inbound: InboundMessage) -> None:
    if inbound.channel not in CHANNELS:
        raise ValueError(f"unknown channel {inbound.channel!r} (ARCHITECTURE.md §6.1)")
    if not inbound.external_user_id.strip() or not inbound.external_thread_id.strip():
        raise ValueError("external_user_id and external_thread_id must not be empty")


async def _lock(session: AsyncSession, key: str) -> None:
    # Held until the transaction ends: two messages from one new account at the same moment
    # wait for each other instead of creating two customers.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def _customer_by_email(session: AsyncSession, email: str, name: str | None) -> tuple[uuid.UUID, bool]:
    await _lock(session, f"customer-email:{email}")
    found = await session.scalar(text("SELECT id FROM customers WHERE lower(email) = :email"), {"email": email})
    if found is not None:
        return found, False
    # An existing customer's name is never changed from an unverified message or form.
    created = await session.scalar(
        text("INSERT INTO customers (full_name, email) VALUES (:name, :email) RETURNING id"),
        {"name": name, "email": email},
    )
    return created, True


async def _customer_for(session: AsyncSession, channel: str, external_user_id: str, display_name: str | None,
                        email: str | None) -> tuple[uuid.UUID, bool]:
    """The customer behind a channel account, created (with the account) when it is new."""
    await _lock(session, f"identity:{channel}:{external_user_id}")
    found = await session.scalar(
        text("SELECT customer_id FROM customer_identities WHERE channel = :channel AND external_user_id = :user"),
        {"channel": channel, "user": external_user_id},
    )
    if found is not None:
        return found, False
    if email is not None:
        customer_id, new_customer = await _customer_by_email(session, email, display_name)
    else:
        customer_id = await session.scalar(
            text("INSERT INTO customers (full_name) VALUES (:name) RETURNING id"), {"name": display_name}
        )
        new_customer = True
    await session.execute(
        text("""INSERT INTO customer_identities (customer_id, channel, external_user_id, display_name)
                VALUES (:customer, :channel, :user, :name)"""),
        {"customer": customer_id, "channel": channel, "user": external_user_id, "name": display_name},
    )
    return customer_id, new_customer


async def _conversation(session: AsyncSession, customer_id: uuid.UUID, channel: str, thread: str):
    statement = text(
        """
        INSERT INTO conversations (customer_id, channel, external_thread_id)
        VALUES (:customer, :channel, :thread)
        ON CONFLICT (channel, external_thread_id) DO UPDATE SET last_message_at = now()
        RETURNING id, ticket_id, context, (xmax = 0) AS inserted
        """
    ).columns(context=JSONB)
    return (await session.execute(statement, {"customer": customer_id, "channel": channel, "thread": thread})).one()


async def _resolve(session: AsyncSession, inbound: InboundMessage) -> Resolved:
    customer_id, new_customer = await _customer_for(
        session, inbound.channel, inbound.external_user_id, inbound.display_name, _email_of(inbound)
    )
    conversation = await _conversation(session, customer_id, inbound.channel, inbound.external_thread_id)

    message_id, repeat = None, False
    if inbound.external_message_id:
        message_id = await session.scalar(
            text("""SELECT id FROM messages WHERE conversation_id = :conv AND sender_type = 'customer'
                    AND external_message_id = :ext LIMIT 1"""),
            {"conv": conversation.id, "ext": inbound.external_message_id},
        )
        repeat = message_id is not None
    if message_id is None:
        message_id = await session.scalar(
            text(
                """INSERT INTO messages (conversation_id, sender_type, channel, body, attachments, external_message_id)
                   VALUES (:conv, 'customer', :channel, :body, :attachments, :ext) RETURNING id"""
            ).bindparams(bindparam("attachments", type_=JSONB)),
            {"conv": conversation.id, "channel": inbound.channel, "body": inbound.text,
             "attachments": inbound.attachments, "ext": inbound.external_message_id or None},
        )
    customer = (await session.execute(
        text("SELECT full_name, email FROM customers WHERE id = :id"), {"id": customer_id})).one()
    await session.commit()
    return Resolved(
        customer_id=customer_id,
        conversation_id=conversation.id,
        message_id=message_id,
        channel=inbound.channel,
        external_thread_id=inbound.external_thread_id,
        ticket_id=conversation.ticket_id,
        context=dict(conversation.context or {}),
        customer_name=customer.full_name,
        customer_email=customer.email,
        new_customer=new_customer,
        new_conversation=conversation.inserted,
        repeat=repeat,
    )


async def resolve(inbound: InboundMessage, session: AsyncSession | None = None) -> Resolved:
    """Find or create the customer and conversation for an inbound message, and store the message.

    Publishes message.received for a message seen for the first time.
    """
    _check(inbound)
    if session is None:
        async with get_sessionmaker()() as own:
            resolved = await _resolve(own, inbound)
    else:
        resolved = await _resolve(session, inbound)
    if not resolved.repeat:
        publish("message.received", {
            "message_id": str(resolved.message_id),
            "conversation_id": str(resolved.conversation_id),
            "customer_id": str(resolved.customer_id),
            "ticket_id": str(resolved.ticket_id) if resolved.ticket_id else None,
            "channel": resolved.channel,
            "body": inbound.text,
        })
    return resolved


async def set_context(conversation_id: uuid.UUID, context: dict[str, Any], session: AsyncSession | None = None) -> None:
    """Replace a conversation's context (intake's slot-filling state). {} clears it."""
    json.dumps(context)  # refuse what can't be stored before touching the database
    statement = text("UPDATE conversations SET context = :context WHERE id = :id RETURNING id").bindparams(
        bindparam("context", type_=JSONB)
    )
    if session is None:
        async with get_sessionmaker()() as own:
            updated = await own.scalar(statement, {"context": context, "id": conversation_id})
            await own.commit()
    else:
        updated = await session.scalar(statement, {"context": context, "id": conversation_id})
        await session.commit()
    if updated is None:
        raise LookupError(f"no conversation {conversation_id}")
