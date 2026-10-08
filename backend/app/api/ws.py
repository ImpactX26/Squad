"""WS /ws/staff?token=: every bus event, live, to signed-in dashboards (ARCHITECTURE.md §9, §10).

Messages are {type, data, ts}. Close codes: 1008 sign in again (no, bad or expired token),
1011 the server can't check tokens (no JWT_SECRET, database down), 1013 the connection fell
too far behind: reconnect and refetch.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.auth import staff_for_token
from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.db import DB_ERRORS, get_sessionmaker
from app.core.events import Listener, ListenerOverflow, bus
from app.core.security import AuthNotConfigured
from app.models import StaffUser

log = logging.getLogger(__name__)
router = APIRouter()

TokenAuthenticator = Callable[[str], Awaitable[StaffUser | None]]


def get_token_authenticator(settings: Settings = Depends(get_app_settings)) -> TokenAuthenticator:
    async def authenticate(token: str) -> StaffUser | None:
        # A short session of its own: a socket stays open for hours and mustn't hold a connection.
        async with get_sessionmaker()() as session:
            return await staff_for_token(session, token, settings)

    return authenticate


@router.websocket("/ws/staff")
async def ws_staff(
    websocket: WebSocket,
    token: str = "",
    authenticate: TokenAuthenticator = Depends(get_token_authenticator),
) -> None:
    # Listen before accepting: an event published while the token is being checked is queued,
    # not lost, so a dashboard that connects and then fetches never misses an update.
    with bus.listen() as listener:
        # Accept before checking, so the browser sees the close code and its reason.
        await websocket.accept()
        try:
            staff = await authenticate(token) if token else None
        except AuthNotConfigured:
            await websocket.close(code=1011, reason="Sign-in isn't set up")
            return
        except DB_ERRORS as exc:
            log.warning("/ws/staff: database unreachable: %s", type(exc).__name__)
            await websocket.close(code=1011, reason="The database is unavailable")
            return
        if staff is None:
            await websocket.close(code=1008, reason="Sign in again")
            return

        # A task group, so both tasks end with this handler, even when the server cancels it.
        try:
            async with asyncio.TaskGroup() as group:
                sender = group.create_task(send_events(websocket, listener))
                receiver = group.create_task(wait_for_close(websocket))
                receiver.add_done_callback(lambda _: sender.cancel())
        except* ListenerOverflow:
            await websocket.close(code=1013, reason="Too far behind: reconnect")
        except* WebSocketDisconnect:
            pass  # the dashboard left while an event was on its way


async def send_events(websocket: WebSocket, listener: Listener) -> None:
    while True:
        event = await listener.get()
        await websocket.send_json(event.model_dump(mode="json"))


async def wait_for_close(websocket: WebSocket) -> None:
    # The dashboard sends nothing we act on; reading is how a closed socket is noticed.
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
