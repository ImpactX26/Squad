"""The website chat widget channel (ARCHITECTURE.md §6.2, §11.4).

    POST /api/chat/session     short pre-chat form (name + email) -> a session id
    WS   /ws/chat/{session_id} the conversation itself

The pre-chat form is what links a web session to a customer (§6.2): the email arrives before the
first message, so `identity.resolve` can find or create the right customer straight away. The web
session id is both the external user id and the thread key.

The adapter side is the outbound half: the dispatcher calls `send()` with the conversation's
thread id (§6.1), and this pushes it down that session's socket. When nobody is connected -- a
closed tab, or `POST /api/dev/simulate` with no browser open -- the reply is buffered for the next
connection and also recorded in the simulated sink, so it is never silently dropped.
"""

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from app.brain.intake import FALLBACK_REPLY_NO_TICKET, handle_inbound
from app.channels.base import Channel, InboundMessage, registry
from app.channels.dispatcher import dispatcher
from app.core.db import SessionLocal

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

MAX_MESSAGE_CHARS = 4000
MAX_BUFFERED_REPLIES = 20


class ChatSessionRequest(BaseModel):
    """The pre-chat form (§6.2): a name and an email, nothing else."""

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr


class ChatSessionResponse(BaseModel):
    session_id: str
    ws_path: str
    greeting: str


GREETING = "Hi {name}! Describe what's going wrong with your device and I'll raise a ticket for you."


@dataclass
class WebSession:
    session_id: str
    name: str
    email: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class WebChatAdapter:
    """The `web` ChannelAdapter (§6.1). One live socket per session id."""

    channel: Channel = "web"

    def __init__(self) -> None:
        self.sessions: dict[str, WebSession] = {}
        self._sockets: dict[str, WebSocket] = {}
        self._buffered: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=MAX_BUFFERED_REPLIES))

    async def start(self) -> None:
        """Nothing to connect: the widget's socket is served by this app."""
        return None

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        socket = self._sockets.get(thread_id)
        payload = {"type": "message", "sender": "ai", "text": text,
                   "at": datetime.now(UTC).isoformat()}
        if socket is not None:
            try:
                await socket.send_json(payload)
                return f"web-{uuid.uuid4().hex[:12]}"
            except Exception as e:  # the tab closed between the lookup and the send
                log.info("web chat socket for %s went away: %s", thread_id, type(e).__name__)
                self._sockets.pop(thread_id, None)
        # Nobody is listening: keep it for the next connection, and record it as a simulated
        # delivery so POST /api/dev/simulate can show what the customer would have seen.
        self._buffered[thread_id].append(payload)
        return await registry.sink.send(thread_id, text, {**meta, "channel": "web",
                                                          "reason": "no websocket connected"})

    async def typing(self, thread_id: str) -> None:
        socket = self._sockets.get(thread_id)
        if socket is not None:
            try:
                await socket.send_json({"type": "typing"})
            except Exception:
                self._sockets.pop(thread_id, None)

    # ---------- socket bookkeeping ----------

    def open_session(self, name: str, email: str) -> WebSession:
        session = WebSession(session_id=uuid.uuid4().hex, name=name.strip(), email=email)
        self.sessions[session.session_id] = session
        return session

    async def attach(self, session_id: str, socket: WebSocket) -> None:
        self._sockets[session_id] = socket
        while self._buffered[session_id]:
            await socket.send_json(self._buffered[session_id].popleft())

    def detach(self, session_id: str, socket: WebSocket) -> None:
        if self._sockets.get(session_id) is socket:
            del self._sockets[session_id]

    def connected(self) -> list[str]:
        return sorted(self._sockets)


adapter = WebChatAdapter()


# ---------- routes ----------


@router.post("/api/chat/session", response_model=ChatSessionResponse)
async def create_chat_session(body: ChatSessionRequest) -> ChatSessionResponse:
    """Start a widget session. The name and email link it to a customer (§6.2)."""
    session = adapter.open_session(body.name, str(body.email))
    log.info("web chat session %s opened", session.session_id)
    return ChatSessionResponse(
        session_id=session.session_id,
        ws_path=f"/ws/chat/{session.session_id}",
        greeting=GREETING.format(name=session.name.split()[0]),
    )


@router.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    """The widget's conversation. Each inbound message runs the §7.1 intake pipeline."""
    await websocket.accept()
    session = adapter.sessions.get(session_id)
    if session is None and not await _conversation_exists(session_id):
        # Not a session this process opened, and no conversation to reconnect to.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unknown session")
        return

    await adapter.attach(session_id, websocket)
    await websocket.send_json({"type": "ready", "session_id": session_id})
    try:
        while True:
            raw = await websocket.receive_json()
            body = _message_text(raw)
            if not body:
                await websocket.send_json({"type": "error", "detail": "Send a non-empty text message."})
                continue
            await _handle(session_id, session, body, websocket)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("web chat %s failed", session_id)
        await _close_quietly(websocket)
    finally:
        adapter.detach(session_id, websocket)


async def _handle(session_id: str, session: WebSession | None, body: str, websocket: WebSocket) -> None:
    """One customer message: echo it, show the typing indicator, run intake, flush the reply."""
    await websocket.send_json({"type": "message", "sender": "customer", "text": body,
                               "at": datetime.now(UTC).isoformat()})
    await adapter.typing(session_id)
    inbound = InboundMessage(
        channel="web",
        external_user_id=session_id,
        external_thread_id=session_id,
        display_name=session.name if session else None,
        text=body,
        external_message_id=f"web-{uuid.uuid4().hex[:12]}",
        raw_meta={"session_id": session_id},
    )
    try:
        result = await handle_inbound(
            inbound, email=session.email if session else None,
            full_name=session.name if session else None)
    except Exception:
        # Intake turns a model outage into the §15 reply itself; this is anything else (the database,
        # the tool servers). The customer still gets an answer, and the socket stays open.
        log.exception("web chat %s: intake failed; sending the fallback reply", session_id)
        await websocket.send_json({"type": "message", "sender": "ai", "text": FALLBACK_REPLY_NO_TICKET,
                                   "at": datetime.now(UTC).isoformat()})
        return
    # The reply is already queued on the outbox; deliver it now instead of waiting for the tick.
    await dispatcher.deliver_pending(conversation_id=result.conversation_id)
    if result.ticket_number:
        await websocket.send_json({"type": "ticket", "ticket_number": result.ticket_number,
                                   "ticket_id": str(result.ticket_id)})


def _message_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()[:MAX_MESSAGE_CHARS]
    if isinstance(raw, dict):
        return str(raw.get("text") or "").strip()[:MAX_MESSAGE_CHARS]
    return ""


async def _conversation_exists(session_id: str) -> bool:
    async with SessionLocal() as db:
        return bool((await db.execute(text(
            "SELECT 1 FROM conversations WHERE channel = 'web' AND external_thread_id = :thread"
        ), {"thread": session_id})).scalar())


async def _close_quietly(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
    except Exception:
        pass