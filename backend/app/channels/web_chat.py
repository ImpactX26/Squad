"""Website chat (ARCHITECTURE.md §6.2, §11.4): POST /api/chat/session and WS /ws/chat/{session_id}.

The pre-chat form (name, email) opens a session; its id is the web channel's account and thread
key, kept by the browser so a reload resumes the chat. The socket then carries JSON frames.

Browser → server:
    {"type": "message", "text": "...", "client_id": "<id the page made>"}

Server → browser:
    {"type": "session", "session_id", "name", "ticket_number" | null, "messages": [Message, ...]}
        first, on every connect: the chat so far (the last 200 messages)
    {"type": "message", ...Message}
        the customer's own message echoed with its client_id once the server has it ("delivered"),
        then each reply as it is sent
    {"type": "typing", "on": true | false}
    {"type": "ticket", "ticket_number": "SR-2026-00042"}   once intake has created the ticket
    {"type": "error", "detail": "..."}                      a frame the server couldn't use; the socket stays open
    Message = {"id" | null, "sender": "customer" | "support", "text", "created_at", "client_id" | null}

Close codes: 1008 this session doesn't exist (start a new chat), 1011 the database is down (retry).
Never silence (§15): channels.inbound sends FALLBACK_REPLY_NO_TICKET when intake fails, and the
socket stays open.
"""

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels import identity
from app.channels.base import InboundMessage, limit_idle_transaction
from app.channels.dispatcher import get_dispatcher
from app.channels.inbound import run_intake
from app.core.db import DB_ERRORS, get_sessionmaker

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TEXT = 4000
HISTORY_LIMIT = 200
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SESSION_IN_PATH = re.compile(r"(/ws/chat/[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+")


class MaskChatSessions(logging.Filter):
    """uvicorn logs every socket's path, and a chat's session id is its only credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/ws/chat/" in message:
            record.msg, record.args = SESSION_IN_PATH.sub(r"\1***", message), None
        return True


def mask_chat_sessions() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, MaskChatSessions) for f in logger.filters):
            logger.addFilter(MaskChatSessions())


# ---------- schemas ----------


class ChatSessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class ChatSessionOut(BaseModel):
    session_id: str
    name: str


class IncomingMessage(BaseModel):
    type: str
    text: str = Field(max_length=MAX_TEXT)
    client_id: str

    @field_validator("client_id")
    @classmethod
    def client_id_shape(cls, value: str) -> str:
        if not CLIENT_ID.fullmatch(value):
            raise ValueError("client_id is 1-64 letters, digits, - or _")
        return value


# ---------- the adapter ----------


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WebChatAdapter:
    """The web channel: every open socket of a session gets each frame (a customer may have two tabs)."""

    channel = "web"

    def __init__(self) -> None:
        self._sockets: defaultdict[str, set[WebSocket]] = defaultdict(set)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        pass  # part of the API: nothing to connect to

    async def stop(self) -> None:
        pass

    def attach(self, session_id: str, websocket: WebSocket) -> None:
        self._sockets[session_id].add(websocket)

    def detach(self, session_id: str, websocket: WebSocket) -> None:
        sockets = self._sockets.get(session_id)
        if sockets is not None:
            sockets.discard(websocket)
            if not sockets:
                del self._sockets[session_id]

    async def broadcast(self, session_id: str, frame: dict[str, Any]) -> int:
        delivered = 0
        for websocket in list(self._sockets.get(session_id, ())):
            try:
                await websocket.send_json(frame)
                delivered += 1
            except Exception:  # a socket that closed meanwhile
                self.detach(session_id, websocket)
        return delivered

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        # With no socket open the reply is still stored (messages); the chat shows it on reconnect.
        await self.broadcast(thread_id, {"type": "message", "id": meta.get("message_id"), "sender": "support",
                                         "text": text, "created_at": _now(), "client_id": None})
        return f"web-{meta.get('message_id') or uuid.uuid4()}"

    async def typing(self, thread_id: str) -> None:
        await self.broadcast(thread_id, {"type": "typing", "on": True})

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks[session_id]

    def keep(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the messages being handled (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


web_adapter = WebChatAdapter()


# ---------- database ----------


def get_chat_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Short sessions of their own: a chat socket stays open for minutes and mustn't hold a connection."""
    return get_sessionmaker()


async def _web_session(db: AsyncSession, session_id: str) -> Any:
    await limit_idle_transaction(db)
    return (await db.execute(text(
        """
        SELECT c.id AS conversation_id, ci.display_name, t.ticket_number
        FROM conversations c
        JOIN customer_identities ci ON ci.channel = 'web' AND ci.external_user_id = c.external_thread_id
        LEFT JOIN tickets t ON t.id = c.ticket_id
        WHERE c.channel = 'web' AND c.external_thread_id = :sid
        """), {"sid": session_id})).first()


async def _history(db: AsyncSession, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (await db.execute(text(
        """
        SELECT id, sender_type, body, created_at, external_message_id FROM messages
        WHERE conversation_id = :c AND NOT is_internal_note AND sender_type <> 'system'
        ORDER BY created_at DESC, id DESC LIMIT :limit
        """), {"c": conversation_id, "limit": HISTORY_LIMIT})).all()
    return [{"id": str(r.id), "sender": "customer" if r.sender_type == "customer" else "support", "text": r.body,
             "created_at": r.created_at.isoformat(),
             "client_id": r.external_message_id if r.sender_type == "customer" else None}
            for r in reversed(rows)]


async def _ticket_number(db: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    await limit_idle_transaction(db)
    return await db.scalar(text(
        "SELECT t.ticket_number FROM conversations c JOIN tickets t ON t.id = c.ticket_id WHERE c.id = :c"),
        {"c": conversation_id})


# ---------- routes ----------


@router.post("/api/chat/session", response_model=ChatSessionOut, status_code=201)
async def create_chat_session(
    body: ChatSessionIn, maker: Callable[[], AsyncSession] = Depends(get_chat_sessionmaker)
) -> ChatSessionOut:
    """The pre-chat form: name and email → a new chat session. Public (customers have no account)."""
    async with maker() as db:
        try:
            opened = await identity.open_web_session(body.name, body.email, db)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    return ChatSessionOut(session_id=opened.session_id, name=body.name)


async def _handle(session_id: str, name: str | None, conversation_id: uuid.UUID, incoming: IncomingMessage,
                  maker: Callable[[], AsyncSession], last_ticket: list[str | None]) -> None:
    """Intake for one message, then the reply flushed at once and the ticket number if it is new."""
    inbound = InboundMessage(channel="web", external_user_id=session_id, external_thread_id=session_id,
                             display_name=name, text=incoming.text.strip(), attachments=[],
                             external_message_id=incoming.client_id, raw_meta={})
    async with web_adapter.lock(session_id):  # one message at a time per session, in order
        await run_intake(web_adapter, inbound)
        await web_adapter.broadcast(session_id, {"type": "typing", "on": False})
        try:
            # A request path passes its own conversation: this customer never waits on the loop (§5.4).
            await get_dispatcher().deliver_pending(conversation_id)
            async with maker() as db:
                number = await _ticket_number(db, conversation_id)
        except (*DB_ERRORS, TimeoutError) as exc:
            log.warning("web chat: after intake: %s", type(exc).__name__)
            return
        if number and number != last_ticket[0]:
            last_ticket[0] = number
            await web_adapter.broadcast(session_id, {"type": "ticket", "ticket_number": number})


@router.websocket("/ws/chat/{session_id}")
async def ws_chat(
    websocket: WebSocket, session_id: str, maker: Callable[[], AsyncSession] = Depends(get_chat_sessionmaker)
) -> None:
    # Accept before checking, so the browser sees the close code (1008 starts a new chat).
    await websocket.accept()
    try:
        async with maker() as db:
            found = await _web_session(db, session_id)
            history = await _history(db, found.conversation_id) if found else []
    except (*DB_ERRORS, TimeoutError) as exc:
        log.warning("/ws/chat: database unreachable: %s", type(exc).__name__)
        await websocket.close(code=1011, reason="The chat is unavailable; try again shortly")
        return
    if found is None:
        await websocket.close(code=1008, reason="Unknown chat session: start a new chat")
        return

    last_ticket: list[str | None] = [found.ticket_number]
    await websocket.send_json({"type": "session", "session_id": session_id, "name": found.display_name,
                               "ticket_number": found.ticket_number, "messages": history})
    web_adapter.attach(session_id, websocket)
    try:
        while True:
            try:
                frame = await websocket.receive_json()
            except (ValueError, KeyError):  # not JSON, or a binary frame
                await websocket.send_json({"type": "error", "detail": "Send JSON text frames."})
                continue
            if not isinstance(frame, dict) or frame.get("type") != "message":
                await websocket.send_json({"type": "error", "detail": 'Only {"type": "message"} frames are accepted.'})
                continue
            try:
                incoming = IncomingMessage.model_validate(frame)
            except ValidationError as exc:
                await websocket.send_json({"type": "error", "detail": exc.errors()[0]["msg"],
                                           "client_id": frame.get("client_id")})
                continue
            if not incoming.text.strip():
                await websocket.send_json({"type": "error", "detail": "The message is empty.",
                                           "client_id": incoming.client_id})
                continue
            # The echo is the page's "delivered".
            await web_adapter.broadcast(session_id, {"type": "message", "id": None, "sender": "customer",
                                                     "text": incoming.text.strip(), "created_at": _now(),
                                                     "client_id": incoming.client_id})
            # In the background, so the socket keeps reading; not cancelled when the customer leaves:
            # the ticket and the stored reply are there when they come back.
            web_adapter.keep(asyncio.create_task(
                _handle(session_id, found.display_name, found.conversation_id, incoming, maker, last_ticket)))
    except WebSocketDisconnect:
        pass
    finally:
        web_adapter.detach(session_id, websocket)
