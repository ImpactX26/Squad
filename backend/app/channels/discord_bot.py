"""The Discord channel (ARCHITECTURE.md §6.2).

The Gateway websocket runs as an asyncio task inside FastAPI's lifespan, not `client.run()`,
which would install signal handlers and take over the loop.

The bot answers where the customer wrote, and never opens a thread (§6.2):

- **DMs.** The thread key is the DM channel id, and replies go straight back to it.
- **The support channel.** `#support` (DISCORD_SUPPORT_CHANNEL_ID) holds one conversation per
  person: the key is `<channel id>:<user id>`, so everything one customer writes there is one
  conversation (the serial they gave a minute ago is still known), and the reply @mentions them in
  the channel. A bot @mention at the start of a message is dropped before intake reads it.
- **A thread under the support channel** (one a person opened, or one from before) is its own
  conversation, keyed by the thread id; replies stay in it, and it is named after its ticket.

Opening a thread per message is what this used to do, and every new message then became a new
conversation that had forgotten the last one.

**Message Content Intent must be enabled** in the Developer Portal, or `message.content` arrives
empty and every message looks blank. The adapter checks for it at startup and says so plainly
rather than silently receiving nothing.

It starts only when ENABLE_DISCORD is true **and** DISCORD_BOT_TOKEN is set, so on any machine
but the demo host (§15) nothing connects and the dispatcher uses the simulated sink instead.
"""

import asyncio
import logging
import re
from typing import Any

from app.channels.base import Channel, InboundMessage
from app.channels.dispatcher import dispatcher
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# Discord rejects a message over 2000 characters outright.
MAX_MESSAGE_CHARS = 1900
THREAD_NAME_CHARS = 90
# How long to let the Gateway connect before giving up on it at startup.
READY_TIMEOUT_SECONDS = 30
# "<channel id>:<user id>": one person's conversation in the support channel itself.
SEPARATOR = ":"


def support_channel_key(channel_id: Any, user_id: Any) -> str:
    return f"{channel_id}{SEPARATOR}{user_id}"


class DiscordAdapter:
    """The `discord` ChannelAdapter (§6.1), wrapping one discord.py Client."""

    channel: Channel = "discord"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def enabled(cls, settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        return bool(settings.enable_discord and settings.discord_bot_token.strip())

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Connect the Gateway as a task and wait until it is ready (§6.2)."""
        import discord

        intents = discord.Intents.default()
        intents.message_content = True   # needs Message Content Intent in the portal
        intents.dm_messages = True
        self._client = discord.Client(intents=intents)
        self._client.event(self._wrap_on_message())

        ready = asyncio.Event()

        async def on_ready() -> None:
            ready.set()

        self._client.event(on_ready)

        # start(), never run(): run() installs signal handlers and runs its own loop.
        self._task = asyncio.create_task(
            self._client.start(self.settings.discord_bot_token), name="discord-gateway")
        done, _ = await asyncio.wait(
            [asyncio.create_task(ready.wait()), self._task],
            timeout=READY_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED,
        )
        if self._task in done:        # it exited instead of connecting: surface why
            await self._task
            raise RuntimeError("the Discord gateway stopped before it was ready")
        if not ready.is_set():
            raise TimeoutError(f"the Discord gateway was not ready within {READY_TIMEOUT_SECONDS}s")
        log.info("discord bot connected as %s", self._client.user)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - closing, nothing to recover
                pass
        self._client, self._task = None, None
        log.info("discord bot stopped")

    # ---------- outbound ----------

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        """Reply where the conversation is: the DM, the thread, or the support channel (§6.1, §6.2)."""
        channel_id, _, user_id = thread_id.partition(SEPARATOR)
        channel = await self._channel(channel_id)
        if user_id:
            # The support channel itself is shared, so the reply names whom it is for, and may
            # notify that one person only: never @everyone or a role, whatever the text says.
            import discord

            sent = await channel.send(
                f"<@{user_id}> {text}"[:MAX_MESSAGE_CHARS],
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                         users=[discord.Object(id=int(user_id))]))
            return str(sent.id)
        sent = await channel.send(text[:MAX_MESSAGE_CHARS])
        # A thread is named after its ticket the first time we know it, so #support stays readable.
        await self._name_thread(channel, meta)
        return str(sent.id)

    async def typing(self, thread_id: str) -> None:
        try:
            channel = await self._channel(thread_id.partition(SEPARATOR)[0])
            await channel.typing()
        except Exception as e:  # a typing indicator is never worth failing a message over
            log.info("discord typing indicator failed for %s: %s", thread_id, type(e).__name__)

    async def _channel(self, thread_id: str) -> Any:
        if self._client is None:
            raise RuntimeError("discord adapter is not running")
        found = self._client.get_channel(int(thread_id))
        if found is None:
            # Not in the cache: a DM channel or a thread this process hasn't seen yet.
            found = await self._client.fetch_channel(int(thread_id))
        return found

    async def _name_thread(self, channel: Any, meta: dict) -> None:
        ticket_number = meta.get("ticket_number")
        if not ticket_number or getattr(channel, "type", None) is None:
            return
        name = getattr(channel, "name", "") or ""
        if not hasattr(channel, "edit") or ticket_number in name or "thread" not in str(channel.type):
            return
        try:
            await channel.edit(name=f"{ticket_number} {name}"[:THREAD_NAME_CHARS])
        except Exception as e:
            log.info("could not rename discord thread %s: %s", channel.id, type(e).__name__)

    # ---------- inbound ----------

    def _wrap_on_message(self):
        async def on_message(message: Any) -> None:
            await self._on_message(message)

        return on_message

    async def _on_message(self, message: Any) -> None:
        """One Discord message -> the §7.1 intake pipeline."""
        import discord

        from app.brain.intake import handle_inbound

        if self._client is None or message.author.bot or message.author == self._client.user:
            return
        # "@ServiceMesh my battery…": the mention is how they got our attention, not part of the problem.
        mention = re.compile(rf"<@!?{getattr(self._client.user, 'id', 0)}>")
        body = mention.sub(" ", message.content or "").strip()
        if not body:
            # Almost always Message Content Intent being off in the Developer Portal (§13.1).
            log.warning("discord message %s had no content; is Message Content Intent enabled?",
                        message.id)
            return

        support_channel_id = (self.settings.discord_support_channel_id or "").strip()
        is_dm = isinstance(message.channel, discord.DMChannel)
        key = str(message.channel.id)

        if not is_dm:
            # The support channel itself, or a thread whose parent it is. (This used to ask whether the
            # thread's own id was the support channel's, so every message inside a thread was ignored.)
            parent_id = str(getattr(message.channel, "parent_id", "") or "")
            if not support_channel_id or support_channel_id not in (str(message.channel.id), parent_id):
                return  # not our support channel, and not a thread under it
            if str(message.channel.id) == support_channel_id:
                # §6.2: in the channel itself, one conversation per person, answered in place.
                key = support_channel_key(message.channel.id, message.author.id)

        inbound = InboundMessage(
            channel="discord",
            external_user_id=str(message.author.id),
            external_thread_id=key,
            display_name=getattr(message.author, "display_name", None) or str(message.author),
            text=body,
            attachments=[{"url": a.url, "filename": a.filename} for a in message.attachments],
            external_message_id=str(message.id),
            raw_meta={"guild_id": str(message.guild.id) if message.guild else None, "dm": is_dm},
        )
        await self.typing(key)
        try:
            result = await handle_inbound(inbound)
        except Exception:
            log.exception("discord intake failed for conversation %s", key)
            return
        # The reply is on the outbox; deliver it now rather than on the next dispatcher tick.
        await dispatcher.deliver_pending(conversation_id=result.conversation_id)