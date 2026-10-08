"""From an adapter to intake (ARCHITECTURE.md §6.1, §15): typing while intake runs, never silence.

Every adapter hands its InboundMessage to run_intake(). It shows the typing indicator until intake
is done, and if intake fails for any reason it sends FALLBACK_REPLY_NO_TICKET: intake itself turns
a model outage into the §15 reply, so this is for everything else.

The fallback goes the usual way, messaging.send_reply → outbox → this conversation's channel.
identity.resolve() is safe to call again for the same message (it is stored once), which gives the
conversation even when intake failed after resolving it. Only when that way is down too (database
or MCP servers unreachable) is the fallback sent straight to the thread the message came from.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.channels import identity
from app.channels.base import ChannelAdapter, InboundMessage
from app.channels.dispatcher import get_dispatcher, simulated_threads

log = logging.getLogger(__name__)

FALLBACK_REPLY_NO_TICKET = "Thanks, we've received your message. An agent will follow up shortly."
TYPING_EVERY_SECONDS = 4.0  # Telegram shows "typing…" for 5 seconds per call
FALLBACK_TOOLS = frozenset({"messaging__send_reply"})

IntakeHandler = Callable[[InboundMessage], Awaitable[None]]


class IntakeNotBuilt(RuntimeError):
    pass


def _intake() -> IntakeHandler:
    """P1's app.brain.intake.handle_inbound, looked up per message."""
    from app.brain import intake

    handler = getattr(intake, "handle_inbound", None)
    if handler is None:
        raise IntakeNotBuilt("app.brain.intake.handle_inbound is not built yet")
    return handler


async def _keep_typing(adapter: ChannelAdapter, thread_id: str) -> None:
    while True:
        try:
            await adapter.typing(thread_id)
        except Exception as exc:  # the indicator is cosmetic; intake goes on
            log.debug("typing on %s failed: %s", adapter.channel, type(exc).__name__)
        await asyncio.sleep(TYPING_EVERY_SECONDS)


async def send_fallback(adapter: ChannelAdapter, inbound: InboundMessage) -> str:
    """Tell the customer we have their message. Returns how it went out: outbox or direct."""
    try:
        from app.brain.mcp_hub import get_hub

        resolved = await identity.resolve(inbound)
        result = await get_hub().call_tool(
            "messaging__send_reply",
            {"conversation_id": str(resolved.conversation_id), "text": FALLBACK_REPLY_NO_TICKET},
            allowed=FALLBACK_TOOLS,
        )
        if result.ok:
            await get_dispatcher().deliver_pending(resolved.conversation_id)
            return "outbox"
        log.error("fallback send_reply refused: %s", result.error)
    except Exception as exc:
        log.error("fallback through the outbox failed (%s); sending it directly", type(exc).__name__)
    await adapter.send(inbound.external_thread_id, FALLBACK_REPLY_NO_TICKET, {"kind": "fallback"})
    return "direct"


async def run_intake(adapter: ChannelAdapter, inbound: InboundMessage, intake: IntakeHandler | None = None) -> None:
    """Run intake for one inbound message with the typing indicator on; never raises."""
    if inbound.raw_meta.get("simulated"):  # POST /api/dev/simulate: its replies may go to the sink
        simulated_threads.add(inbound.channel, inbound.external_thread_id)
    typing = asyncio.create_task(_keep_typing(adapter, inbound.external_thread_id))
    try:
        await (intake or _intake())(inbound)
    except Exception as exc:
        if isinstance(exc, IntakeNotBuilt):
            log.error("%s: sending the fallback reply on %s", exc, inbound.channel)
        else:
            log.exception("intake failed for a %s message; sending the fallback reply", inbound.channel)
        try:
            await send_fallback(adapter, inbound)
        except Exception:
            log.exception("could not send the fallback reply on %s", inbound.channel)
    finally:
        typing.cancel()
        with suppress(asyncio.CancelledError):
            await typing
