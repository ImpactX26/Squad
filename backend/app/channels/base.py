"""The channel adapter interface every channel implements (ARCHITECTURE.md §6.1).

The brain never knows or cares where a message came from:

    inbound:  adapter -> InboundMessage -> identity.resolve() -> intake pipeline
    outbound: messaging.send_reply(conversation_id) -> outbox row -> dispatcher looks up
              conversations.channel -> that adapter's send()

That outbound path is what guarantees "reply on the same platform": the reply target is the
conversation's own channel, never chosen by a model.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

log = logging.getLogger(__name__)

Channel = Literal["discord", "telegram", "email", "web"]
# A channel as a customer reads it ("reply to you on Discord"), for emails and messages (§7.1, §7.9).
CHANNEL_NAMES: dict[str, str] = {"discord": "Discord", "telegram": "Telegram", "web": "the website chat",
                                 "email": "email"}
CHANNELS: tuple[Channel, ...] = ("discord", "telegram", "email", "web")


@runtime_checkable
class ChannelAdapter(Protocol):
    """§6.1. `send` returns the external message id; `typing` shows the platform's indicator."""

    channel: Channel

    async def start(self) -> None: ...                                        # connect / begin polling
    async def send(self, thread_id: str, text: str, meta: dict) -> str: ...   # returns external message id
    async def typing(self, thread_id: str) -> None: ...


@dataclass
class InboundMessage:
    """One message arriving from any channel (§6.1)."""

    channel: str
    external_user_id: str       # discord user id, telegram user id, email address, web session id
    external_thread_id: str     # DM channel / thread id, telegram chat id, email thread root Message-ID, web session id
    display_name: str | None
    text: str
    attachments: list[dict] = field(default_factory=list)
    external_message_id: str = ""
    raw_meta: dict = field(default_factory=dict)  # subject, In-Reply-To, etc.


# ---------- registry ----------


class ChannelRegistry:
    """The adapters running in this process, by channel.

    Only the demo host sets the ENABLE_* flags (§15), so on any other machine most channels have
    no adapter. The dispatcher sends those replies to the simulated sink instead of failing, which
    is what makes POST /api/dev/simulate a working demo backup with no bots connected.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        self.sink = SimulatedSink()

    def register(self, adapter: ChannelAdapter) -> None:
        self._adapters[adapter.channel] = adapter
        log.info("channel %s registered", adapter.channel)

    def unregister(self, channel: str) -> None:
        self._adapters.pop(channel, None)

    def get(self, channel: str) -> ChannelAdapter | None:
        return self._adapters.get(channel)

    def adapter_for(self, channel: str) -> tuple[ChannelAdapter, bool]:
        """The adapter for this channel, or the sink. The flag is True when the sink was used."""
        adapter = self._adapters.get(channel)
        return (adapter, False) if adapter is not None else (self.sink, True)

    def running(self) -> list[str]:
        return sorted(self._adapters)

    def clear(self) -> None:
        self._adapters.clear()
        self.sink.clear()


@dataclass(frozen=True)
class SinkDelivery:
    channel: str
    thread_id: str
    text: str
    meta: dict[str, Any]
    external_message_id: str
    at: datetime


class SimulatedSink:
    """Stands in for any channel whose adapter isn't running (§10 POST /api/dev/simulate).

    Every delivery is logged and kept in a short ring buffer, so the simulate endpoint can return
    the reply the customer would have seen on the real platform.
    """

    channel: Channel = "web"  # satisfies the protocol; the real channel is on each delivery
    MAX_KEPT = 200

    def __init__(self) -> None:
        self.deliveries: deque[SinkDelivery] = deque(maxlen=self.MAX_KEPT)
        self._counter = 0

    async def start(self) -> None:
        return None

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        self._counter += 1
        channel = str(meta.get("channel") or "unknown")
        external_message_id = f"sink-{channel}-{self._counter}"
        self.deliveries.append(SinkDelivery(
            channel=channel, thread_id=thread_id, text=text, meta=dict(meta),
            external_message_id=external_message_id, at=datetime.now(UTC),
        ))
        log.info("simulated sink <- %s thread=%s: %s", channel, thread_id, text.replace("\n", " ")[:200])
        return external_message_id

    async def typing(self, thread_id: str) -> None:
        return None

    def since(self, marker: int) -> list[SinkDelivery]:
        """Deliveries recorded after `marker`, which is a previous value of `self.count`."""
        taken = self.count - marker
        return list(self.deliveries)[-taken:] if taken > 0 else []

    @property
    def count(self) -> int:
        return self._counter

    def clear(self) -> None:
        self.deliveries.clear()


# The process-wide registry; main.py's lifespan registers the adapters it starts.
registry = ChannelRegistry()