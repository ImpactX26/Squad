import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.api import auth, commands, copilot, dev, health, internal, inventory, jobs, pay, payments, search, tickets, ws
from app.brain import workflows
from app.brain.mcp_hub import hub
from app.brain.runtime import drain_pending_logs
from app.channels import web_chat
from app.channels.base import registry
from app.channels.discord_bot import DiscordAdapter
from app.channels.dispatcher import dispatcher
from app.channels.email_channel import EmailAdapter
from app.channels.telegram_bot import TelegramAdapter
from app.core.config import get_settings
from app.core.db import engine
from app.core.events import bus
from app.core.logging import setup_logging
from app.payments.upi_verifier import UpiVerifier

settings = get_settings()
setup_logging(settings.app_env)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the MCP hub, the channel adapters, the outbox dispatcher, and the UPI verifier (§12)."""
    # Register the MCP tools (§4.3 step 1). connect_all never raises: a server that is down
    # contributes no tools and is retried on the next call, so the API still starts.
    await hub.connect_all()

    # The web widget's socket is served by this app, so its adapter is always available (§6.2).
    registry.register(web_chat.adapter)
    # Each bot starts only on the demo host, and only when its credentials are there (§15).
    started = await asyncio.gather(
        _start_channel("telegram", TelegramAdapter),
        _start_channel("discord", DiscordAdapter),
        _start_channel("email", EmailAdapter),
    )
    running = [adapter for adapter in started if adapter is not None]

    # Delivers everything messaging.send_reply queues, to the conversation's own channel (§5.4).
    dispatcher.start()
    log.info("channels running: %s", ", ".join(registry.running()))
    # The fixed chains (§4.2): payment.paid -> receipt and booking; job.status_changed / job.completed.
    workflows.register()
    # UPI payments (§7.6): polls the bank alerts when the inbox and BANK_* are set, and always runs
    # the sweeps (expired links, UTRs waiting too long for their alert).
    verifier = UpiVerifier(settings)
    await verifier.start()
    try:
        yield
    finally:
        await verifier.stop()
        await bus.drain()  # a receipt or booking chain still running finishes before the dispatcher stops
        await workflows.drain()  # and a free warranty booking
        await dispatcher.stop()
        await asyncio.gather(*(adapter.stop() for adapter in running), return_exceptions=True)
        await drain_pending_logs()
        await engine.dispose()


async def _start_channel(name: str, adapter_class: type) -> Any:
    """Start one channel adapter, but only on the demo host (§6.2, §15).

    Each class decides whether it is configured: the ENABLE_* flag **and** its credentials. When
    it is not, nothing connects and the dispatcher sends that channel's replies to the simulated
    sink instead, which is what makes POST /api/dev/simulate a working demo backup.

    A bot that fails to start - bad token, no network, Gateway refusing - is logged and skipped.
    The dashboard must still come up.
    """
    if not adapter_class.enabled(settings):
        log.info("%s channel off (not enabled, or its credentials are empty)", name)
        return None
    adapter = adapter_class(settings)
    try:
        await adapter.start()
    except Exception:
        log.exception("%s channel failed to start; carrying on without it", name)
        return None
    registry.register(adapter)
    return adapter


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    # Unhandled exceptions become 500s outside CORSMiddleware, so the browser sees a CORS
    # error instead of the real cause. Handling DB failures here keeps CORS headers on a 503.
    log.error("database unavailable during %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable. Try again shortly."})


# Bad credentials arrive wrapped as DBAPIError; refused connections, DNS failures, and
# connect timeouts arrive as raw OSError subclasses.
for exc_type in (DBAPIError, ConnectionError, socket.gaierror, TimeoutError):
    app.add_exception_handler(exc_type, database_unavailable)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(tickets.router)
app.include_router(commands.router)
app.include_router(search.router)
app.include_router(copilot.router)
app.include_router(jobs.router)
app.include_router(inventory.router)
app.include_router(payments.router)
# Public: the payment link's token is the only key, and every route is rate-limited (§7.6).
app.include_router(pay.router)
app.include_router(web_chat.router)
app.include_router(ws.router)
app.include_router(internal.router)
# Dev-only, and each route also checks APP_ENV (§10, §15).
app.include_router(dev.router)