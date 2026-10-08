"""The channel adapter interface and the inbound message (ARCHITECTURE.md §6.1).

Every channel implements ChannelAdapter, so the brain never knows or cares where a message came
from. Inbound: adapter → InboundMessage → identity.resolve() → intake. Outbound: send_reply →
outbox row → the dispatcher looks up conversations.channel → that adapter's send(). A model never
chooses the channel.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, get_args

Channel = Literal["discord", "telegram", "email", "web"]
CHANNELS: frozenset[str] = frozenset(get_args(Channel))


class ChannelAdapter(Protocol):
    channel: Channel

    async def start(self) -> None: ...  # connect / begin polling

    async def send(self, thread_id: str, text: str, meta: dict) -> str: ...  # returns external message id

    async def typing(self, thread_id: str) -> None: ...


@dataclass
class InboundMessage:
    channel: str
    external_user_id: str  # discord user id, telegram user id, email address, web session id
    external_thread_id: str  # DM channel / thread id, telegram chat id, email thread root Message-ID, web session id
    display_name: str | None
    text: str
    attachments: list[dict]
    external_message_id: str
    raw_meta: dict  # subject, In-Reply-To, etc.; web: the pre-chat name and email


class Adapters:
    """The adapters running in this process, one per channel. The dispatcher sends through them."""

    def __init__(self) -> None:
        self._by_channel: dict[str, ChannelAdapter] = {}

    def add(self, adapter: ChannelAdapter) -> None:
        if adapter.channel not in CHANNELS:
            raise ValueError(f"unknown channel {adapter.channel!r} (ARCHITECTURE.md §6.1)")
        if adapter.channel in self._by_channel:
            raise ValueError(f"a {adapter.channel} adapter is already running")
        self._by_channel[adapter.channel] = adapter

    def remove(self, channel: str) -> None:
        self._by_channel.pop(channel, None)

    def get(self, channel: str) -> ChannelAdapter | None:
        return self._by_channel.get(channel)

    def connected(self) -> list[str]:
        return sorted(self._by_channel)


adapters = Adapters()
