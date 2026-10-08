"""The stage backups (ARCHITECTURE.md §10, §15, §17.6): POST /api/dev/simulate and GET /api/dev/channels.

Development only (APP_ENV=development, which the demo server keeps, §17.3): anywhere else these
routes are a 404. Staff login required.

/api/dev/simulate takes a fake inbound message down the same path as a real one: the channel
layer's run_intake (typing, never silence) → intake → identity, ticket, reply → the outbox. The
reply goes out through the channel's adapter when one runs in this process, or else to the
dispatcher's simulated sink (a channel switched off here), and comes back in the response.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_staff
from app.api.deps import get_app_settings
from app.brain.intake import handle_inbound
from app.channels import identity
from app.channels.base import ChannelAdapter, InboundMessage, adapters
from app.channels.dispatcher import Dispatcher, SimulatedSink, get_dispatcher
from app.channels.inbound import IntakeHandler, run_intake
from app.channels.lifespan import enabled_channels
from app.core.config import Settings
from app.core.db import get_session
from app.models import StaffUser
from app.schemas.dev import DevChannels, SimulatedReply, SimulatedTicket, SimulateIn, SimulateOut

router = APIRouter()


def require_development(settings: Settings = Depends(get_app_settings)) -> None:
    if settings.app_env != "development":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")


def get_intake() -> IntakeHandler:
    return handle_inbound


def get_dev_dispatcher() -> Dispatcher:
    return get_dispatcher()


class _SimulatedChannel:
    """What run_intake sees: the channel's adapter when it runs here, otherwise the simulated sink."""

    def __init__(self, channel: str, adapter: ChannelAdapter | None, sink: SimulatedSink | None) -> None:
        self.channel = channel
        self._adapter = adapter
        self._sink = sink if sink is not None and channel in sink.channels else None

    async def start(self) -> None:
        pass

    async def typing(self, thread_id: str) -> None:
        if self._adapter is not None:
            await self._adapter.typing(thread_id)

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        # Only the channel layer's last-resort fallback comes here; replies go through the outbox.
        if self._adapter is not None:
            return await self._adapter.send(thread_id, text, meta)
        if self._sink is not None:
            return await self._sink.send(self.channel, thread_id, text, meta)
        raise LookupError(f"no {self.channel} adapter is running in this process")


def _delivery(channel: str, sink: SimulatedSink | None) -> str:
    if adapters.get(channel) is not None:
        return "adapter"
    return "simulated" if sink is not None and channel in sink.channels else "none"


def _inbound(body: SimulateIn) -> InboundMessage:
    user = (body.external_user_id or "").strip()
    meta: dict[str, Any] = {"simulated": True}
    if body.channel == "email":
        user = (user or body.email or "").strip().lower()
        if "@" not in user:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail="An email message needs the sender's address: email or external_user_id.")
        if body.subject:
            meta["subject"] = body.subject
    elif body.channel == "web" and body.email:
        meta["email"] = body.email
    user = user or f"sim-{uuid.uuid4().hex[:12]}"
    return InboundMessage(
        channel=body.channel, external_user_id=user, external_thread_id=(body.external_thread_id or user).strip(),
        display_name=body.display_name, text=body.text, attachments=[],
        external_message_id=f"sim-{uuid.uuid4().hex}", raw_meta=meta,
    )


@router.post(
    "/api/dev/simulate",
    response_model=SimulateOut,
    dependencies=[Depends(require_development)],
    responses={401: {}, 404: {"description": "Not in development"}, 422: {}},
)
async def simulate(
    body: SimulateIn,
    staff: StaffUser = Depends(get_current_staff),
    session: AsyncSession = Depends(get_session),
    intake: IntakeHandler = Depends(get_intake),
    dispatcher: Dispatcher = Depends(get_dev_dispatcher),
) -> SimulateOut:
    """Inject a fake inbound message on any channel (a backup when a platform is down on stage)."""
    inbound = _inbound(body)
    # What the conversation held before: the replies are what intake adds to it.
    before = list(await session.scalars(text(
        """SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id
           WHERE c.channel = :channel AND c.external_thread_id = :thread"""),
        {"channel": inbound.channel, "thread": inbound.external_thread_id}))
    await session.commit()  # end these reads: intake takes seconds and uses its own connections

    errors: list[str] = []

    async def tracked(message: InboundMessage) -> None:
        try:
            await intake(message)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}"[:300])
            raise  # run_intake sends the fallback reply

    await run_intake(_SimulatedChannel(body.channel, adapters.get(body.channel), dispatcher.sink), inbound, tracked)
    resolved = await identity.resolve(inbound, session)  # stored once: this finds the message intake handled
    await dispatcher.deliver_pending(resolved.conversation_id)  # a request path flushes its own replies (§5.4)

    rows = (await session.execute(text(
        """
        SELECT m.id, m.body, m.created_at, o.status, o.last_error
        FROM messages m LEFT JOIN outbox o ON o.message_id = m.id
        WHERE m.conversation_id = :conv AND m.sender_type <> 'customer' AND NOT (m.id = ANY(:before))
        ORDER BY m.created_at, m.id
        """), {"conv": resolved.conversation_id, "before": before})).all()
    sunk = {r["message_id"] for r in dispatcher.sink.replies(resolved.conversation_id)} if dispatcher.sink else set()
    conversation = (await session.execute(text(
        """
        SELECT c.context ->> 'awaiting' AS awaiting, t.id, t.ticket_number, t.title, t.status, t.priority, t.flags
        FROM conversations c LEFT JOIN tickets t ON t.id = c.ticket_id WHERE c.id = :conv
        """), {"conv": resolved.conversation_id})).one()

    ticket = None
    if conversation.id is not None:
        ticket = SimulatedTicket(id=conversation.id, ticket_number=conversation.ticket_number, title=conversation.title,
                                 status=conversation.status, priority=conversation.priority,
                                 flags=list(conversation.flags))
    return SimulateOut(
        channel=body.channel,
        external_user_id=inbound.external_user_id,
        external_thread_id=inbound.external_thread_id,
        delivery=_delivery(body.channel, dispatcher.sink),
        customer_id=resolved.customer_id,
        conversation_id=resolved.conversation_id,
        message_id=resolved.message_id,
        awaiting=conversation.awaiting,
        ticket=ticket,
        replies=[SimulatedReply(message_id=r.id, text=r.body, status=r.status, simulated=str(r.id) in sunk,
                                last_error=r.last_error, created_at=r.created_at) for r in rows],
        intake_error=errors[0] if errors else None,
    )


@router.get(
    "/api/dev/channels",
    response_model=DevChannels,
    dependencies=[Depends(require_development), Depends(get_current_staff)],
    responses={401: {}, 404: {"description": "Not in development"}},
)
async def channels(
    settings: Settings = Depends(get_app_settings), dispatcher: Dispatcher = Depends(get_dev_dispatcher)
) -> DevChannels:
    """Which channel adapters are connected in this process, and where the others' replies go."""
    return DevChannels(
        connected=adapters.connected(),
        enabled=sorted(enabled_channels(settings)),
        simulated=sorted(dispatcher.sink.channels) if dispatcher.sink else [],
    )
