"""The app's side of the outbox: queue a staff reply, and deliver everything queued (§5.4, §6.1).

`messaging.send_reply` never touches Discord or Telegram. It writes a `messages` row and an
`outbox` row, and this loop -- running in the backend process, where the bot connections live --
looks up `conversations.channel` and calls that adapter's `send()`. The reply target comes from
the conversation, never from a model, which is what guarantees "reply on the same platform".

A channel with no adapter running (the ENABLE_* flags are only set on the demo host, §15) is
delivered to the simulated sink instead of failing, so `POST /api/dev/simulate` proves the whole
flow with no bots connected.

Failures are retried: `attempts` counts them, `last_error` keeps the reason, and a row that has
used up MAX_ATTEMPTS is marked `failed` and left alone. Each success publishes `message.sent` (§9).
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as text_sql

from app.channels.base import ChannelRegistry, registry as default_registry
from app.core.db import SessionLocal
from app.core.events import EventBus, bus

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
BATCH = 20
MAX_ATTEMPTS = 5
# Backoff before a failed row is claimed again, by attempt number.
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0, 30.0, 60.0)


@dataclass(frozen=True)
class Delivery:
    """One outbox row's outcome, as POST /api/dev/simulate returns it."""

    outbox_id: uuid.UUID
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    ticket_id: uuid.UUID | None
    channel: str
    thread_id: str
    text: str
    ok: bool
    simulated: bool                      # delivered to the sink because no adapter is running
    external_message_id: str | None = None
    error: str | None = None


# One round trip: claim a batch (bumping attempts so a crash can't retry forever) and bring the
# conversation's channel and thread back with it. SKIP LOCKED keeps two dispatchers off one row.
_CLAIM = text_sql("""
WITH claimed AS (
    UPDATE outbox SET attempts = attempts + 1
    WHERE id IN (
        SELECT id FROM outbox
        WHERE status = 'pending'
          AND attempts < :max_attempts
          AND NOT (id = ANY(CAST(:deferred AS uuid[])))
          AND (CAST(:conversation_id AS uuid) IS NULL
               OR conversation_id = CAST(:conversation_id AS uuid))
        ORDER BY created_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, conversation_id, message_id, payload, attempts
)
SELECT c.id              AS outbox_id,
       c.conversation_id AS conversation_id,
       c.message_id      AS message_id,
       c.payload         AS payload,
       c.attempts        AS attempts,
       conv.channel      AS conversation_channel,
       conv.external_thread_id AS external_thread_id,
       conv.ticket_id    AS ticket_id,
       cust.email        AS customer_email,
       tk.ticket_number  AS ticket_number
FROM claimed c
LEFT JOIN conversations conv ON conv.id = c.conversation_id
LEFT JOIN customers cust     ON cust.id = conv.customer_id
LEFT JOIN tickets tk         ON tk.id = conv.ticket_id
ORDER BY c.id
""")

_MARK_SENT = text_sql("""
UPDATE outbox SET status = 'sent', last_error = NULL WHERE id = ANY(CAST(:ids AS uuid[]))
""")

_STAMP_MESSAGES = text_sql("""
UPDATE messages m SET external_message_id = v.external_message_id
FROM (
    SELECT unnest(CAST(:ids AS uuid[])) AS id,
           unnest(CAST(:external_message_ids AS text[])) AS external_message_id
) v
WHERE m.id = v.id
""")

_MARK_FAILED = text_sql("""
UPDATE outbox
SET last_error = :error,
    status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'pending' END
WHERE id = :id
""")


class OutboxDispatcher:
    """Delivers queued replies. main.py's lifespan starts one; tests drive it a tick at a time."""

    def __init__(
        self,
        registry: ChannelRegistry | None = None,
        *,
        events: EventBus | None = None,
        poll_seconds: float = POLL_SECONDS,
        batch: int = BATCH,
        max_attempts: int = MAX_ATTEMPTS,
        retry_backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.registry = registry or default_registry
        self.events = events or bus
        self.poll_seconds = poll_seconds
        self.batch = batch
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff
        self._task: asyncio.Task[None] | None = None
        self._retry_after: dict[uuid.UUID, float] = {}
        # One tick at a time. _claim() commits the attempts bump to release the row lock before
        # the send (holding a pooler connection across a network call would be worse), which
        # leaves the row 'pending' until _record() runs. Without this lock, a second caller in
        # that window claims and delivers the same row again -- the customer gets the reply
        # twice. All channel delivery is in this one process by design (§5.4), so one
        # in-process lock closes the window; SKIP LOCKED still guards simultaneous claims.
        self._tick = asyncio.Lock()

    # ---------- lifecycle ----------

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="outbox-dispatcher")
            log.info("outbox dispatcher started (every %.1fs)", self.poll_seconds)
        return self._task

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            log.info("outbox dispatcher stopped")

    async def run(self) -> None:
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A database blip must not kill the loop; the rows are still pending.
                log.exception("outbox dispatcher tick failed")
            await asyncio.sleep(self.poll_seconds)

    # ---------- one tick ----------

    async def deliver_pending(self, *, conversation_id: uuid.UUID | None = None) -> list[Delivery]:
        """Claim the pending rows, deliver them concurrently, record what happened.

        `conversation_id` narrows it to one conversation. The request paths (web chat, telegram,
        /api/dev/simulate) pass the conversation they just handled, so a customer waiting on
        their own reply never waits on somebody else's queue; the loop passes nothing and drains
        everything, including retries.

        Serialised: the loop and the request paths both call this, and overlapping ticks would
        deliver one row twice.
        """
        async with self._tick:
            return await self._tick_once(conversation_id)

    async def _tick_once(self, conversation_id: uuid.UUID | None = None) -> list[Delivery]:
        rows = await self._claim(conversation_id)
        if not rows:
            return []
        deliveries = await asyncio.gather(*(self._deliver(row) for row in rows))
        await self._record(deliveries)
        for delivery in deliveries:
            if delivery.ok:
                await self.events.publish("message.sent", {
                    "outbox_id": str(delivery.outbox_id),
                    "message_id": str(delivery.message_id) if delivery.message_id else None,
                    "conversation_id": str(delivery.conversation_id) if delivery.conversation_id else None,
                    "ticket_id": str(delivery.ticket_id) if delivery.ticket_id else None,
                    "channel": delivery.channel,
                    "text": delivery.text,
                    "external_message_id": delivery.external_message_id,
                    "simulated": delivery.simulated,
                })
        return deliveries

    async def _claim(self, conversation_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        now = time.monotonic()
        deferred = [str(row_id) for row_id, until in self._retry_after.items() if until > now]
        for row_id in [row_id for row_id, until in self._retry_after.items() if until <= now]:
            self._retry_after.pop(row_id, None)
        async with SessionLocal() as session:
            rows = (await session.execute(_CLAIM, {
                "max_attempts": self.max_attempts, "batch": self.batch, "deferred": deferred,
                "conversation_id": str(conversation_id) if conversation_id else None,
            })).mappings().all()
            await session.commit()
        return [dict(row) for row in rows]

    async def _deliver(self, row: dict[str, Any]) -> Delivery:
        payload = dict(row["payload"] or {})
        channel = row["conversation_channel"] or payload.get("channel") or "web"
        thread_id = str(row["external_thread_id"] or payload.get("thread_id") or payload.get("to") or "")
        body = _body(payload)
        adapter, simulated = self.registry.adapter_for(channel)
        base = {
            "outbox_id": row["outbox_id"],
            "conversation_id": row["conversation_id"],
            "message_id": row["message_id"],
            "ticket_id": row["ticket_id"],
            "channel": channel,
            "thread_id": thread_id,
            "text": body,
            "simulated": simulated,
        }
        try:
            external_message_id = await adapter.send(thread_id, body, _meta(row, payload, channel))
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:500]
            log.warning("outbox %s to %s failed (attempt %d/%d): %s",
                        row["outbox_id"], channel, row["attempts"], self.max_attempts, reason)
            backoff = self.retry_backoff[min(row["attempts"], len(self.retry_backoff)) - 1]
            self._retry_after[row["outbox_id"]] = time.monotonic() + backoff
            return Delivery(**base, ok=False, error=reason)
        return Delivery(**base, ok=True, external_message_id=str(external_message_id or ""))

    async def _record(self, deliveries: list[Delivery]) -> None:
        sent = [d for d in deliveries if d.ok]
        failed = [d for d in deliveries if not d.ok]
        async with SessionLocal() as session:
            if sent:
                await session.execute(_MARK_SENT, {"ids": [str(d.outbox_id) for d in sent]})
                stamped = [d for d in sent if d.message_id and d.external_message_id]
                if stamped:
                    await session.execute(_STAMP_MESSAGES, {
                        "ids": [str(d.message_id) for d in stamped],
                        "external_message_ids": [d.external_message_id for d in stamped],
                    })
            for delivery in failed:
                await session.execute(_MARK_FAILED, {
                    "error": delivery.error, "id": str(delivery.outbox_id),
                    "max_attempts": self.max_attempts,
                })
            await session.commit()


def _meta(row: dict[str, Any], payload: dict[str, Any], channel: str) -> dict[str, Any]:
    """What `adapter.send` is told about this delivery, beyond the text.

    The payload from the MCP tool, plus the two things only the conversation knows: who the
    customer is (the email channel has to address the mail, §6.2) and which ticket this is
    (Discord names the ticket's thread after it, §6.2). An explicit `to` in the payload wins,
    because messaging.send_email sets it for mail that has no channel conversation (§5.4).
    """
    return {
        **payload,
        "channel": channel,
        "to": payload.get("to") or row.get("customer_email"),
        "ticket_number": payload.get("ticket_number") or row.get("ticket_number"),
    }


def _body(payload: dict[str, Any]) -> str:
    """The text an adapter sends. Emails (§5.4) carry a subject and a template, not a reply body."""
    if payload.get("kind") == "email":
        return f"[{payload.get('template', 'email')}] {payload.get('subject', '')}".strip()
    return str(payload.get("text") or "")


# ---------- what became of a queued reply ----------


@dataclass(frozen=True)
class Outcome:
    """The settled state of one outbox row, whoever delivered it."""

    ok: bool
    attempts: int
    simulated: bool
    external_message_id: str | None = None
    error: str | None = None


async def outcome_of(outbox_id: uuid.UUID, *, sink_marker: int | None = None) -> Outcome:
    """Read an outbox row back after a delivery attempt.

    Asking "did *my* deliver_pending() call send it?" gives the wrong answer whenever the
    background loop got there first, which at a one-second tick is often. The row itself is the
    truth. `sink_marker` is a `registry.sink.count` taken beforehand, which tells us whether the
    sink was the one that took it.
    """
    async with SessionLocal() as session:
        row = (await session.execute(text_sql(
            "SELECT o.status, o.attempts, o.last_error, o.payload->>'text' AS body,"
            " o.payload->>'channel' AS channel, o.payload->>'thread_id' AS thread_id,"
            " m.external_message_id"
            " FROM outbox o LEFT JOIN messages m ON m.id = o.message_id WHERE o.id = :id"
        ), {"id": outbox_id})).mappings().one_or_none()
    if row is None:
        return Outcome(ok=False, attempts=0, simulated=False, error="the outbox row is gone")
    simulated = False
    if sink_marker is not None:
        simulated = any(
            d.channel == row["channel"] and d.thread_id == (row["thread_id"] or "")
            and d.text == (row["body"] or "")
            for d in default_registry.sink.since(sink_marker)
        )
    return Outcome(
        ok=row["status"] == "sent",
        attempts=row["attempts"],
        simulated=simulated,
        external_message_id=row["external_message_id"],
        error=row["last_error"],
    )


# ---------- queueing a staff reply ----------


async def queue_staff_reply(
    ticket_id: uuid.UUID,
    text: str,
    *,
    sender_staff_id: uuid.UUID,
    body_original: str | None = None,
    internal_note: bool = False,
) -> dict[str, Any] | None:
    """Store an agent's reply and queue it for the customer's own channel (§7.3 step 4).

    `messaging.send_reply` is how the *brain* talks to customers (§5.4), and it writes the message
    as `ai`. A human agent's reply is a different row -- sender_type `agent`, their staff id, and
    `body_original` holding the note they wrote before Polish -- so the app writes it here, on the
    same outbox the dispatcher already drains.

    An internal note is stored on the ticket and never queued. Returns None when the ticket has no
    conversation to reply on (nothing to deliver to), or the row ids otherwise.
    """
    async with SessionLocal() as session:
        conversation = (await session.execute(text_sql("""
            SELECT c.id, c.channel, c.external_thread_id
            FROM conversations c
            WHERE c.ticket_id = :ticket_id
            ORDER BY c.last_message_at DESC
            LIMIT 1
        """), {"ticket_id": ticket_id})).mappings().one_or_none()
        if conversation is None and not internal_note:
            return None

        channel = conversation["channel"] if conversation else "internal"
        message_id = (await session.execute(text_sql(
            "INSERT INTO messages (conversation_id, ticket_id, sender_type, sender_staff_id,"
            " channel, body, body_original, is_internal_note)"
            " VALUES (:conversation_id, :ticket_id, 'agent', :staff_id, :channel, :body,"
            " :body_original, :internal_note) RETURNING id"
        ), {
            "conversation_id": conversation["id"] if conversation else None,
            "ticket_id": ticket_id,
            "staff_id": sender_staff_id,
            "channel": "internal" if internal_note else channel,
            "body": text,
            "body_original": body_original,
            "internal_note": internal_note,
        })).scalar()

        outbox_id = None
        if not internal_note:
            outbox_id = (await session.execute(text_sql(
                "INSERT INTO outbox (conversation_id, message_id, payload)"
                " VALUES (:conversation_id, :message_id, CAST(:payload AS jsonb)) RETURNING id"
            ), {
                "conversation_id": conversation["id"],
                "message_id": message_id,
                "payload": json.dumps({
                    "kind": "reply", "channel": channel,
                    "thread_id": conversation["external_thread_id"], "text": text,
                }),
            })).scalar()
            await session.execute(text_sql(
                "UPDATE conversations SET last_message_at = now() WHERE id = :id"),
                {"id": conversation["id"]})
        await session.commit()

    return {
        "message_id": message_id,
        "outbox_id": outbox_id,
        "conversation_id": conversation["id"] if conversation else None,
        "channel": "internal" if internal_note else channel,
    }


# The process-wide dispatcher; main.py's lifespan starts it.
dispatcher = OutboxDispatcher()