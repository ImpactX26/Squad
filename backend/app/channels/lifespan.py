"""What the API process runs beside its routes: the channel adapters and the outbox dispatcher
(ARCHITECTURE.md §3, §6). app/main.py passes channels_lifespan to FastAPI.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.base import CHANNELS
from app.channels.dispatcher import SEND_TIMEOUT_SECONDS, SimulatedSink, get_dispatcher
from app.core.config import Settings

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = SEND_TIMEOUT_SECONDS + 5


def enabled_channels(settings: Settings) -> set[str]:
    """Channels this process connects to. The web chat is part of the API, so it is always on."""
    switches = {"discord": settings.enable_discord, "telegram": settings.enable_telegram,
                "email": settings.enable_email, "web": True}
    return {channel for channel in CHANNELS if switches[channel]}


@asynccontextmanager
async def channels_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    dispatcher = get_dispatcher()
    if settings.app_env == "development":
        # POST /api/dev/simulate on a channel switched off here gets its reply from the sink (§10).
        dispatcher.sink = SimulatedSink(CHANNELS - enabled_channels(settings))
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop), name="outbox-dispatcher")
    log.info("outbox dispatcher started")
    try:
        yield
    finally:
        # Let a tick in progress finish its transaction; cancel only one that hangs.
        stop.set()
        try:
            await asyncio.wait_for(loop, SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            log.warning("outbox dispatcher did not stop within %s s; cancelled", SHUTDOWN_GRACE_SECONDS)
