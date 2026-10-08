"""The outbox dispatcher (ARCHITECTURE.md §5.4, §6.1): delivers queued replies on their own channel.

messaging.send_reply writes a messages row and an outbox row. The dispatcher reads the pending
rows, looks up conversations.channel and external_thread_id, calls that channel's adapter and
marks the row sent, or failed after MAX_ATTEMPTS. The channel comes from the conversation,
never from the payload, so a model can't choose where a reply goes.

One tick at a time (a lock): the attempts bump is committed before the send, to release the row
lock while the network call runs, which leaves the row pending meanwhile; without the lock the
loop and a request path could claim the same row and the customer would get the reply twice.
Request paths pass their own conversation_id, so a customer never waits on somebody else's
queue; the loop drains everything every second and does the retries. Whether a reply went out
is read back from its row (outcome_of), never from whose call happened to send it.

Rows without a conversation (send_email) belong to the email channel and are left alone here.

On a laptop, a channel switched off in settings (ENABLE_*=false) has no adapter; with a
SimulatedSink its replies are logged and kept in memory instead, so POST /api/dev/simulate can
return them (§10). A channel that is switched on but not connected is never simulated: its
replies wait and are retried, then fail.
"""

import asyncio
import logging
import re
import uuid
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import Adapters, adapters, limit_idle_transaction
from app.core.db import DB_ERRORS, get_sessionmaker
from app.core.events import publish

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH = 20
SEND_TIMEOUT_SECONDS = 20.0
# Attempt n+1 waits until created_at + (2^n - 1) * RETRY_BASE_SECONDS: 0, 2, 6, 14, 30 s.
RETRY_BASE_SECONDS = 2
# A Telegram API error can carry the request URL, which holds the bot token.
BOT_TOKEN = re.compile(r"bot\d+:[\w-]+")


@dataclass(frozen=True)
class Outcome:
    outbox_id: uuid.UUID
    status: str  # pending, sent, failed
    attempts: int
    last_error: str | None


def _error_text(exc: BaseException) -> str:
    detail = str(exc) or "no detail"
    return BOT_TOKEN.sub("bot<token>", f"{type(exc).__name__}: {detail}")[:300]


class SimulatedSink:
    """Takes the replies of channels switched off in this process (development only)."""

    def __init__(self, channels: Iterable[str], keep: int = 200) -> None:
        self.channels = frozenset(channels)
        self._replies: deque[dict[str, Any]] = deque(maxlen=keep)

    async def send(self, channel: str, thread_id: str, text: str, meta: dict) -> str:
        external_id = f"simulated-{uuid.uuid4().hex[:12]}"
        log.info("simulated %s reply to %s: %s", channel, thread_id, text[:120])
        self._replies.append({"channel": channel, "thread_id": thread_id, "text": text,
                              "conversation_id": meta.get("conversation_id"), "message_id": meta.get("message_id"),
                              "external_message_id": external_id})
        return external_id

    def replies(self, conversation_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return [r for r in self._replies if r["conversation_id"] == str(conversation_id)]


class Dispatcher:
    def __init__(self, adapters: Adapters, sessionmaker: async_sessionmaker[AsyncSession],
                 sink: SimulatedSink | None = None) -> None:
        self._adapters = adapters
        self._sessionmaker = sessionmaker
        self.sink = sink
        self._tick = asyncio.Lock()

    async def _claim(self, conversation_id: uuid.UUID | None) -> list[Any]:
        async with self._sessionmaker() as session:
            await limit_idle_transaction(session)
            rows = (await session.execute(
                text(
                    """
                    SELECT o.id, o.message_id, o.payload, o.attempts + 1 AS attempt,
                           c.id AS conversation_id, c.channel, c.external_thread_id, c.ticket_id
                    FROM outbox o JOIN conversations c ON c.id = o.conversation_id
                    WHERE o.status = 'pending'
                      AND (CAST(:conversation AS uuid) IS NULL OR o.conversation_id = :conversation)
                      AND o.created_at + make_interval(secs => (power(2, o.attempts) - 1) * :base) <= now()
                    ORDER BY o.created_at
                    LIMIT :batch
                    FOR UPDATE OF o SKIP LOCKED
                    """
                ).columns(payload=JSONB),
                {"conversation": conversation_id, "base": RETRY_BASE_SECONDS, "batch": BATCH},
            )).all()
            if rows:
                await session.execute(text("UPDATE outbox SET attempts = attempts + 1 WHERE id = ANY(:ids)"),
                                      {"ids": [r.id for r in rows]})
            await session.commit()
        return rows

    async def _send(self, row: Any) -> str:
        payload = dict(row.payload)
        body = payload.pop("text", "")
        meta = {**payload, "outbox_id": str(row.id), "conversation_id": str(row.conversation_id),
                "message_id": str(row.message_id) if row.message_id else None}
        adapter = self._adapters.get(row.channel)
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            if adapter is not None:
                return await adapter.send(row.external_thread_id, body, meta)
            if self.sink is not None and row.channel in self.sink.channels:
                return await self.sink.send(row.channel, row.external_thread_id, body, meta)
        raise LookupError(f"no {row.channel} adapter is running in this process")

    async def _record(self, row: Any, external_id: str | None, error: str | None) -> None:
        async with self._sessionmaker() as session:
            await limit_idle_transaction(session)
            if error is None:
                await session.execute(text("UPDATE outbox SET status = 'sent', last_error = NULL WHERE id = :id"),
                                      {"id": row.id})
                if row.message_id is not None:
                    await session.execute(text("UPDATE messages SET external_message_id = :ext WHERE id = :id"),
                                          {"ext": external_id, "id": row.message_id})
            else:
                status = "failed" if row.attempt >= MAX_ATTEMPTS else "pending"
                await session.execute(text("UPDATE outbox SET status = :status, last_error = :error WHERE id = :id"),
                                      {"status": status, "error": error, "id": row.id})
            await session.commit()

    async def deliver_pending(self, conversation_id: uuid.UUID | None = None) -> int:
        """One tick: deliver what is due (only this conversation's rows when one is given).
        Returns how many rows were sent.
        """
        sent = 0
        async with self._tick:
            for row in await self._claim(conversation_id):
                try:
                    external_id = await self._send(row)
                except Exception as exc:  # a channel error must not stop the other replies
                    error = _error_text(exc)
                    log.warning("outbox %s on %s, attempt %d of %d: %s", row.id, row.channel, row.attempt,
                                MAX_ATTEMPTS, error)
                    await self._record(row, None, error)
                    continue
                await self._record(row, external_id, None)
                sent += 1
                publish("message.sent", {
                    "outbox_id": str(row.id),
                    "message_id": str(row.message_id) if row.message_id else None,
                    "conversation_id": str(row.conversation_id),
                    "ticket_id": str(row.ticket_id) if row.ticket_id else None,
                    "channel": row.channel,
                    "external_message_id": external_id,
                })
        return sent

    async def outcome_of(self, outbox_id: uuid.UUID) -> Outcome | None:
        async with self._sessionmaker() as session:
            await limit_idle_transaction(session)
            row = (await session.execute(text("SELECT status, attempts, last_error FROM outbox WHERE id = :id"),
                                         {"id": outbox_id})).first()
        return None if row is None else Outcome(outbox_id, row.status, row.attempts, row.last_error)

    async def run(self, stop: asyncio.Event, interval: float = 1.0) -> None:
        """The loop started in the lifespan: drain and retry every interval until stop is set.

        It stops between ticks, never in the middle of one: a tick cancelled inside its
        transaction can leave the pooler holding that transaction and its row locks.
        """
        failing = False
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
            if stop.is_set():
                return
            try:
                await self.deliver_pending()
                failing = False
            except (*DB_ERRORS, TimeoutError) as exc:
                if not failing:  # once per outage, not once a second
                    log.error("outbox dispatcher can't reach the database: %s", type(exc).__name__)
                failing = True
            except Exception:
                log.exception("outbox dispatcher tick failed")


@lru_cache
def get_dispatcher() -> Dispatcher:
    """The process's dispatcher. The lifespan gives it a sink in development (channels.lifespan)."""
    return Dispatcher(adapters, get_sessionmaker())
