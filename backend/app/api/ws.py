import asyncio
import logging

from fastapi import APIRouter, WebSocket, status

from app.core.db import SessionLocal
from app.core.events import bus
from app.core.security import user_from_token

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/staff")
async def staff_ws(websocket: WebSocket, token: str | None = None) -> None:
    """Dashboard realtime (§9): every bus event, as {type, data, ts}."""
    await websocket.accept()
    try:
        async with SessionLocal() as session:
            user = await user_from_token(token, session) if token else None
    except Exception:
        log.exception("ws/staff auth lookup failed")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="auth unavailable")
        return
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        return

    queue = bus.subscribe()

    async def send_events() -> None:
        while True:
            event = await queue.get()
            await websocket.send_text(event.model_dump_json())

    async def wait_for_disconnect() -> None:
        # Detects a closed client even while no events are flowing.
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    tasks = [asyncio.create_task(send_events()), asyncio.create_task(wait_for_disconnect())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bus.unsubscribe(queue)