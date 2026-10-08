"""In-process async event bus (ARCHITECTURE.md §9).

Every event is (1) handed to the handlers subscribed to its type (workflows) and (2) offered to
every listener (each /ws/staff connection). publish() never waits for either and never raises
because of them: a failing handler is logged, and a listener that falls too far behind is told
to reconnect. One bus per process, which is why the API runs a single uvicorn worker (§2).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.schemas.events import EVENT_TYPES, StaffEvent

log = logging.getLogger(__name__)

Handler = Callable[[StaffEvent], Awaitable[None]]


class ListenerOverflow(Exception):
    """The listener missed events because its queue was full: reconnect and refetch."""


class Listener:
    def __init__(self, max_queue: int) -> None:
        self._queue: asyncio.Queue[StaffEvent | None] = asyncio.Queue(maxsize=max_queue)
        self._overflowed = False

    def offer(self, event: StaffEvent) -> None:
        if self._overflowed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop what is queued and leave one marker, so get() wakes up and reports it.
            self._overflowed = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)

    async def get(self) -> StaffEvent:
        event = await self._queue.get()
        if event is None:
            raise ListenerOverflow
        return event


class EventBus:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._handlers: dict[str, list[Handler]] = {}
        self._listeners: set[Listener] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: str, handler: Handler) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r} (ARCHITECTURE.md §9)")
        self._handlers.setdefault(event_type, []).append(handler)

    @contextmanager
    def listen(self) -> Iterator[Listener]:
        listener = Listener(self._max_queue)
        self._listeners.add(listener)
        try:
            yield listener
        finally:
            self._listeners.discard(listener)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def publish(self, event_type: str, data: dict[str, Any]) -> StaffEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r} (ARCHITECTURE.md §9)")
        event = StaffEvent(type=event_type, data=data, ts=datetime.now(UTC))
        for handler in self._handlers.get(event_type, []):
            task = asyncio.create_task(self._run(handler, event))
            self._tasks.add(task)  # keep a reference until it finishes
            task.add_done_callback(self._tasks.discard)
        for listener in list(self._listeners):
            listener.offer(event)
        return event

    async def drain(self) -> None:
        """Wait for the handlers already started (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @staticmethod
    async def _run(handler: Handler, event: StaffEvent) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("event handler %s failed on %s", getattr(handler, "__qualname__", handler), event.type)


bus = EventBus()
publish = bus.publish
