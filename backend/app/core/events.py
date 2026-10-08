"""In-process event bus (ARCHITECTURE.md §9).

Every event goes two ways: pushed to every connected /ws/staff client as {type, data, ts} (§14.3),
and handed to the workflow handlers subscribed to its type (§4.2), e.g. the payment.paid chain.

The two are deliberately different. A dashboard socket gets a bounded queue that drops its oldest
event when the client stalls, because a slow browser must never block a publisher. A workflow
handler is never dropped: each one runs as its own task, so a slow handler doesn't block the
publisher either, and a failing one is logged without touching the others.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)

# The §9 event names. Anything else is rejected at /internal/events.
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


class Event(BaseModel):
    type: EventType
    data: dict[str, Any]
    ts: datetime


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size
        self._handlers: dict[str, list[Handler]] = {}
        self._running: set[asyncio.Task[None]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ---------- workflow handlers (§4.2) ----------

    def on(self, type: EventType, handler: Handler) -> None:
        """Run `handler` for every event of this type. Registering the same handler twice is a no-op."""
        handlers = self._handlers.setdefault(type, [])
        if handler not in handlers:
            handlers.append(handler)

    def off(self, type: EventType, handler: Handler) -> None:
        handlers = self._handlers.get(type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def drain(self, timeout: float = 10.0) -> int:
        """Wait for the handler tasks still running. Returns how many there were (shutdown and tests)."""
        pending = [task for task in self._running if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        return len(pending)

    async def publish(self, type: EventType, data: dict[str, Any]) -> Event:
        event = Event(type=type, data=data, ts=datetime.now(UTC))
        for queue in list(self._subscribers):
            if queue.full():
                # A stalled client must never block publishers: drop its oldest event.
                queue.get_nowait()
                log.warning("event queue full for a /ws/staff client; dropped oldest event")
            queue.put_nowait(event)
        for handler in list(self._handlers.get(type, [])):
            task = asyncio.create_task(self._run(handler, event), name=f"on-{type}")
            self._running.add(task)  # keep a reference until it's done
            task.add_done_callback(self._running.discard)
        return event

    async def _run(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("handler %s for %s failed", getattr(handler, "__name__", handler), event.type)


bus = EventBus()