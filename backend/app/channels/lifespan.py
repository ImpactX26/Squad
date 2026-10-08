"""What the API process runs beside its routes: the channel adapters and the outbox dispatcher
(ARCHITECTURE.md §3, §6). app/main.py passes channels_lifespan to FastAPI.

Adapters connect in the background, retrying while the network or the platform is down, so the
API starts (and the dashboard works) whatever Telegram is doing. An adapter is registered, and so
receives replies, only once it is connected.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from telegram.error import InvalidToken

from app.channels.base import CHANNELS, ChannelAdapter, adapters
from app.channels.dispatcher import SEND_TIMEOUT_SECONDS, SimulatedSink, get_dispatcher
from app.channels.inbound import run_intake
from app.channels.web_chat import mask_chat_sessions, web_adapter
from app.core.config import Settings

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = SEND_TIMEOUT_SECONDS + 5
RETRY_START_SECONDS = 15.0


def enabled_channels(settings: Settings) -> set[str]:
    """Channels this process connects to. The web chat is part of the API, so it is always on."""
    switches = {"discord": settings.enable_discord, "telegram": settings.enable_telegram,
                "email": settings.enable_email, "web": True}
    return {channel for channel in CHANNELS if switches[channel]}


def build_adapters(settings: Settings) -> list[ChannelAdapter]:
    """The adapters to connect, from the ENABLE_* switches. Discord and email come in Block 2."""
    built: list[ChannelAdapter] = []
    if settings.enable_telegram:
        if settings.telegram_bot_token:
            from app.channels.telegram_bot import TelegramAdapter

            built.append(TelegramAdapter(settings.telegram_bot_token, run_intake))
        else:
            log.error("ENABLE_TELEGRAM=true but TELEGRAM_BOT_TOKEN is empty: Telegram is off")
    return built


async def connect(adapter: ChannelAdapter, stop: asyncio.Event, retry_seconds: float = RETRY_START_SECONDS) -> None:
    """Start an adapter, retrying until it connects or the app stops; then register it."""
    while not stop.is_set():
        try:
            await adapter.start()
        except InvalidToken:
            log.error("%s: the bot token was refused; check it in backend/.env", adapter.channel)
            return
        except Exception as exc:
            log.error("%s adapter could not connect (%s); retrying in %.0f s", adapter.channel,
                      type(exc).__name__, retry_seconds)
            with suppress(Exception):
                await adapter.stop()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), retry_seconds)
            continue
        adapters.add(adapter)
        return


@asynccontextmanager
async def channels_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    mask_chat_sessions()
    dispatcher = get_dispatcher()
    if settings.app_env == "development":
        # POST /api/dev/simulate on a channel switched off here gets its reply from the sink (§10).
        dispatcher.sink = SimulatedSink(CHANNELS - enabled_channels(settings))
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop), name="outbox-dispatcher")
    if adapters.get("web") is None:
        adapters.add(web_adapter)  # part of the API: nothing to connect to
    to_connect = build_adapters(settings)
    connecting = [asyncio.create_task(connect(a, stop), name=f"connect-{a.channel}") for a in to_connect]
    log.info("outbox dispatcher started")
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*connecting, return_exceptions=True)
        try:  # a web message being handled finishes: its reply is queued before the dispatcher stops
            await asyncio.wait_for(web_adapter.drain(), SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            log.warning("web chat messages still in intake after %s s; stopping anyway", SHUTDOWN_GRACE_SECONDS)
        for adapter in to_connect:
            if adapters.get(adapter.channel) is adapter:
                adapters.remove(adapter.channel)
                try:
                    await asyncio.wait_for(adapter.stop(), SHUTDOWN_GRACE_SECONDS)
                except Exception as exc:
                    log.warning("%s adapter did not stop cleanly: %s", adapter.channel, type(exc).__name__)
        if adapters.get("web") is web_adapter:
            adapters.remove("web")
        # Let a tick in progress finish its transaction; cancel only one that hangs.
        try:
            await asyncio.wait_for(loop, SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            log.warning("outbox dispatcher did not stop within %s s; cancelled", SHUTDOWN_GRACE_SECONDS)
