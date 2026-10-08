"""Identity across channels: resolve or create the customer and conversation (ARCHITECTURE.md §6.3).

`customer_identities (channel, external_user_id)` maps each platform account to one customer, so
a complaint on Discord and a later email about the same device land on the same customer once the
identities are linked. Email and web give an email address immediately (the web widget's pre-chat
form, §6.2); Discord and Telegram customers get linked when they share one later.

This module also owns the inbound `messages` row and `conversations.context`, the slot-filling
state of the intake state machine (§7.1). Those are channel-layer writes, not MCP tools: the brain
reaches the database only through the hub (§4.1), and there is no §5 tool for either.

Database round trips cost ~250 ms against the pooler, so the happy path (both the identity and the
conversation already exist) is one query.
"""

import json
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundMessage
from app.core.db import SessionLocal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """Who sent the message, and which conversation it belongs to."""

    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    channel: str
    external_user_id: str            # the platform account: customer_identities' other half
    external_thread_id: str
    context: dict[str, Any]          # conversations.context: {"awaiting": "serial_number", ...}
    ticket_id: uuid.UUID | None      # the conversation's current ticket, when it has one
    customer_name: str | None
    customer_email: str | None
    created_customer: bool
    created_conversation: bool


# Both lookups in one round trip. The cross join on a one-row subquery keeps a result row even
# when neither the identity nor the conversation exists yet, so one query answers every case.
_LOOKUP = text("""
SELECT ci.customer_id    AS identity_customer_id,
       conv.id           AS conversation_id,
       conv.customer_id  AS conversation_customer_id,
       conv.context      AS context,
       conv.ticket_id    AS ticket_id,
       cust.full_name    AS full_name,
       cust.email        AS email
FROM (SELECT 1) AS one
LEFT JOIN customer_identities ci
       ON ci.channel = :channel AND ci.external_user_id = :external_user_id
LEFT JOIN conversations conv
       ON conv.channel = :channel AND conv.external_thread_id = :external_thread_id
LEFT JOIN customers cust
       ON cust.id = COALESCE(ci.customer_id, conv.customer_id)
""")


async def resolve(
    inbound: InboundMessage,
    *,
    email: str | None = None,
    full_name: str | None = None,
    session: AsyncSession | None = None,
) -> Identity:
    """The customer and conversation for this message, creating either on first contact.

    `email` and `full_name` come from the channels that know them up front: the web widget's
    pre-chat form and the email channel's From header (§6.2). When they are given and the customer
    record has no email yet, it is filled in -- that is how a Discord or Telegram account ends up
    on the same customer as their email (§6.3).
    """
    if session is not None:
        return await _resolve(session, inbound, email, full_name)
    async with SessionLocal() as own_session:
        return await _resolve(own_session, inbound, email, full_name)


async def _resolve(
    session: AsyncSession, inbound: InboundMessage, email: str | None, full_name: str | None
) -> Identity:
    found = (await session.execute(_LOOKUP, {
        "channel": inbound.channel,
        "external_user_id": inbound.external_user_id,
        "external_thread_id": inbound.external_thread_id,
    })).mappings().one()

    name = full_name or inbound.display_name
    customer_id = found["identity_customer_id"] or found["conversation_customer_id"]
    created_customer = False

    if customer_id is None:
        customer_id, created_customer = await _find_or_create_customer(session, email, name)
    if found["identity_customer_id"] is None:
        await _ensure_identity(session, inbound, customer_id)

    customer_name, customer_email = found["full_name"], found["email"]
    if created_customer:
        customer_name, customer_email = name, email
    elif email and not customer_email:
        # §6.3: the customer shared an email on a channel that did not have one.
        customer_email = await _fill_in_email(session, customer_id, email) or customer_email

    conversation_id = found["conversation_id"]
    created_conversation = conversation_id is None
    context: dict[str, Any] = {}
    ticket_id: uuid.UUID | None = None
    if created_conversation:
        conversation_id = await _ensure_conversation(session, inbound, customer_id)
    else:
        context = dict(found["context"] or {})
        ticket_id = found["ticket_id"]
        await session.execute(
            text("UPDATE conversations SET last_message_at = now() WHERE id = :id"),
            {"id": conversation_id})

    await session.commit()
    return Identity(
        customer_id=customer_id,
        conversation_id=conversation_id,
        channel=inbound.channel,
        external_user_id=inbound.external_user_id,
        external_thread_id=inbound.external_thread_id,
        context=context,
        ticket_id=ticket_id,
        customer_name=customer_name,
        customer_email=customer_email,
        created_customer=created_customer,
        created_conversation=created_conversation,
    )


async def _find_or_create_customer(
    session: AsyncSession, email: str | None, full_name: str | None
) -> tuple[uuid.UUID, bool]:
    """The customer with this email, or a new one. The flag says whether it was created."""
    if email:
        existing = (await session.execute(
            text("SELECT id FROM customers WHERE lower(email) = lower(:email)"),
            {"email": email})).scalar()
        if existing is not None:
            return existing, False
    # ON CONFLICT covers two first messages from the same new customer arriving at once:
    # customers.email is UNIQUE, so the loser of the race re-reads the winner's row.
    created = (await session.execute(text(
        "INSERT INTO customers (full_name, email) VALUES (:full_name, :email)"
        " ON CONFLICT (email) DO NOTHING RETURNING id"
    ), {"full_name": full_name, "email": email})).scalar()
    if created is not None:
        return created, True
    existing = (await session.execute(
        text("SELECT id FROM customers WHERE lower(email) = lower(:email)"), {"email": email})).scalar()
    return existing, False


async def _ensure_identity(session: AsyncSession, inbound: InboundMessage, customer_id: uuid.UUID) -> None:
    await session.execute(text(
        "INSERT INTO customer_identities (customer_id, channel, external_user_id, display_name)"
        " VALUES (:customer_id, :channel, :external_user_id, :display_name)"
        " ON CONFLICT (channel, external_user_id) DO NOTHING"
    ), {
        "customer_id": customer_id, "channel": inbound.channel,
        "external_user_id": inbound.external_user_id, "display_name": inbound.display_name,
    })


async def _fill_in_email(session: AsyncSession, customer_id: uuid.UUID, email: str) -> str | None:
    """Set this customer's email when they have none. Returns it, or None when it is taken."""
    savepoint = await session.begin_nested()
    try:
        filled = (await session.execute(text(
            "UPDATE customers SET email = :email WHERE id = :id AND email IS NULL RETURNING email"
        ), {"email": email, "id": customer_id})).scalar()
    except Exception as e:  # another customer already owns that address (customers.email is UNIQUE)
        await savepoint.rollback()
        log.info("email is already another customer's; leaving customer %s unchanged (%s)",
                 customer_id, type(e).__name__)
        return None
    await savepoint.commit()
    return filled


async def _ensure_conversation(
    session: AsyncSession, inbound: InboundMessage, customer_id: uuid.UUID
) -> uuid.UUID:
    created = (await session.execute(text(
        "INSERT INTO conversations (customer_id, channel, external_thread_id)"
        " VALUES (:customer_id, :channel, :external_thread_id)"
        " ON CONFLICT (channel, external_thread_id) DO NOTHING RETURNING id"
    ), {
        "customer_id": customer_id, "channel": inbound.channel,
        "external_thread_id": inbound.external_thread_id,
    })).scalar()
    if created is not None:
        return created
    return (await session.execute(text(
        "SELECT id FROM conversations WHERE channel = :channel"
        " AND external_thread_id = :external_thread_id"
    ), {"channel": inbound.channel, "external_thread_id": inbound.external_thread_id})).scalar()


# ---------- linking a channel account to a known customer (§6.3) ----------


async def is_anonymous_customer(customer_id: uuid.UUID) -> bool:
    """True when this customer record is only a placeholder for one channel account.

    No email and no tickets: it was created because somebody wrote in from a platform that
    gives no address. Such a record can safely be folded into a customer we can identify.
    A customer with an email or any history is a real person and is never merged.
    """
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT c.email IS NULL                                        AS no_email,
                   (SELECT count(*) FROM tickets t WHERE t.customer_id = c.id) AS tickets,
                   (SELECT count(*) FROM customer_identities i WHERE i.customer_id = c.id) AS identities
            FROM customers c WHERE c.id = :id
        """), {"id": customer_id})).mappings().one_or_none()
    if row is None:
        return False
    return bool(row["no_email"]) and row["tickets"] == 0 and row["identities"] <= 1


async def customer_email(customer_id: uuid.UUID) -> str | None:
    """A customer's email on file, or None. What a chat account's typed email must match to be linked to
    the owner of the serial it quoted (§6.3)."""
    async with SessionLocal() as session:
        return (await session.execute(
            text("SELECT email FROM customers WHERE id = :id"), {"id": customer_id})).scalar()


async def adopt_customer(identity: Identity, owner_customer_id: uuid.UUID) -> Identity:
    """Move this channel account and its conversation onto a customer we already know (§6.3).

    This is how a Discord or Telegram account with no email gets linked: the serial they quoted is
    registered to a customer, and the email they gave is that customer's, so that is who they are. The placeholder customer that
    was created for the account is deleted once nothing points at it, and from then on their
    tickets show one timeline across every channel.

    Only ever called after is_anonymous_customer() agrees and the emails match (intake._settle_ownership),
    so nobody is merged into another customer on the strength of a serial number alone.
    """
    if str(owner_customer_id) == str(identity.customer_id):
        return identity
    async with SessionLocal() as session:
        await session.execute(text(
            "UPDATE customer_identities SET customer_id = :owner"
            " WHERE channel = :channel AND external_user_id = :external_user_id"
        ), {"owner": owner_customer_id, "channel": identity.channel,
            "external_user_id": identity.external_user_id})
        await session.execute(text(
            "UPDATE conversations SET customer_id = :owner WHERE id = :id"),
            {"owner": owner_customer_id, "id": identity.conversation_id})
        # Harmless when something still references it; the placeholder is then just unused.
        await session.execute(text("""
            DELETE FROM customers c WHERE c.id = :placeholder
              AND c.email IS NULL
              AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.customer_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM conversations v WHERE v.customer_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM customer_identities i WHERE i.customer_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM products p WHERE p.customer_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM addresses a WHERE a.customer_id = c.id)
        """), {"placeholder": identity.customer_id})
        owner = (await session.execute(text(
            "SELECT full_name, email FROM customers WHERE id = :id"), {"id": owner_customer_id})).mappings().one()
        await session.commit()

    log.info("linked %s account to customer %s by registered serial (§6.3)",
             identity.channel, owner_customer_id)
    return replace(
        identity,
        customer_id=owner_customer_id,
        customer_name=owner["full_name"],
        customer_email=owner["email"],
    )


# ---------- the inbound message row ----------


async def record_inbound(identity: Identity, inbound: InboundMessage) -> uuid.UUID:
    """Store the customer's message and return its id (for §7.2's add_followup(message_id)).

    ticket_id stays null here, even when the conversation already has a ticket. Which ticket this
    message belongs to is decided after intake runs: link_message() attaches it to a ticket intake
    just created, and §7.2's add_followup() attaches it to the ticket it is a follow-up on.
    Guessing from the conversation would file a second, unrelated issue under the first ticket.
    """
    async with SessionLocal() as session:
        message_id = (await session.execute(text(
            "INSERT INTO messages (conversation_id, ticket_id, sender_type, channel, body,"
            " attachments, external_message_id)"
            " VALUES (:conversation_id, :ticket_id, 'customer', :channel, :body,"
            " CAST(:attachments AS jsonb), :external_message_id) RETURNING id"
        ), {
            "conversation_id": identity.conversation_id,
            "ticket_id": None,
            "channel": identity.channel,
            "body": inbound.text,
            "attachments": json.dumps(inbound.attachments, default=str),
            "external_message_id": inbound.external_message_id or None,
        })).scalar()
        await session.commit()
        return message_id


async def link_message(message_id: uuid.UUID, ticket_id: uuid.UUID) -> None:
    """Attach an already-stored message to the ticket intake just created for it."""
    async with SessionLocal() as session:
        await session.execute(text(
            "UPDATE messages SET ticket_id = :ticket_id WHERE id = :id AND ticket_id IS NULL"
        ), {"ticket_id": ticket_id, "id": message_id})
        await session.commit()


# ---------- conversations.context: the intake state machine's slot-filling state (§7.1) ----------


class ContextStore:
    """Reads and writes conversations.context. Intake takes one, so tests can fake it."""

    async def get(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        async with SessionLocal() as session:
            found = (await session.execute(
                text("SELECT context FROM conversations WHERE id = :id"),
                {"id": conversation_id})).scalar()
            return dict(found or {})

    async def fill_in_email(self, customer_id: uuid.UUID, email: str) -> str | None:
        """Give a customer with no email this one, typed in a chat (§6.3, §7.1).

        Returns it, or None when they already have one or another customer does: an existing email is
        never changed from a chat message, because the email channel finds customers by it.
        """
        async with SessionLocal() as session:
            filled = await _fill_in_email(session, customer_id, email)
            await session.commit()
            return filled

    async def set(self, conversation_id: uuid.UUID, context: dict[str, Any]) -> None:
        async with SessionLocal() as session:
            await session.execute(
                text("UPDATE conversations SET context = CAST(:context AS jsonb) WHERE id = :id"),
                {"context": json.dumps(context, default=str), "id": conversation_id})
            await session.commit()