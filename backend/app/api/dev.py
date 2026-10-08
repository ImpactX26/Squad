"""Development-only endpoints (ARCHITECTURE.md §10, §15).

`POST /api/dev/simulate` injects a message on any channel, as if it had arrived from Discord,
Telegram, email, or the web widget, and runs the real §7.1 intake pipeline on it. It is the
demo's backup when a platform or the venue Wi-Fi misbehaves (§15), and the way intake is proved
with no bots connected: a channel with no adapter running is delivered to the simulated sink,
which is logged and returned in the response.

`POST /api/dev/simulate-bank-alert` does the same for payments: it builds the bank's credit SMS as
the company phone would forward it and runs it through the real UPI verifier (§7.6).

Two guards: the whole router is unavailable unless APP_ENV is "development", and it needs a staff
login like every other dashboard endpoint. `POST /api/dev/reset-demo` is stricter: admins only,
because it wipes every table.
"""

import importlib.util
import logging
import sys
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain.intake import handle_inbound
from app.channels.base import CHANNELS, InboundMessage, registry
from app.channels.dispatcher import dispatcher
from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.db import SessionLocal, engine, get_session
from app.core.events import bus
from app.core.security import CurrentUser, require_roles
from app.payments.money import IST, format_inr, money_str, to_money
from app.payments.upi_verifier import AlertEmail, ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dev", tags=["dev"])


def require_development(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404, not 403: outside development these routes shouldn't look like they exist."""
    if settings.app_env != "development":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


class SimulateRequest(BaseModel):
    """One made-up inbound message."""

    text: str = Field(min_length=1, max_length=4000)
    channel: Literal["discord", "telegram", "email", "web"] = "web"
    external_user_id: str | None = Field(
        default=None, description='Defaults to "sim-<channel>", so repeat calls continue one conversation')
    external_thread_id: str | None = Field(default=None, description="Defaults to external_user_id")
    display_name: str | None = None
    email: EmailStr | None = Field(default=None, description="What the web pre-chat form or an email From gives")


class SimulatedDelivery(BaseModel):
    """What became of a reply intake queued on the outbox."""

    channel: str
    thread_id: str
    text: str
    ok: bool
    simulated: bool = Field(description="True when nothing was listening and the sink took it")
    attempts: int = 0
    external_message_id: str | None = None
    error: str | None = None


class SimulateResponse(BaseModel):
    channel: str
    external_user_id: str
    external_thread_id: str
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    outcome: str
    reply: str
    awaiting: str | None
    ticket_id: uuid.UUID | None
    ticket_number: str | None
    flags: list[str]
    duplicate_count: int = Field(description="""§7.2: follow-ups on the ticket after this message""")
    priority: str | None = None
    priority_raised: bool = False
    similarity: float | None = Field(default=None, description="Cosine similarity to the matched ticket")
    intent: str | None
    category: str | None
    issue_type: str | None
    urgency: str | None
    serial_number: str | None
    ai_summary: str | None
    diagnostic_steps: list[str]
    model: str | None = Field(description='"<provider>:<model>", as written to ai_runs')
    llm_available: bool
    tool_calls: list[dict[str, Any]]
    intake_ms: int
    deliveries: list[SimulatedDelivery]
    adapters_running: list[str]
    payment: dict[str, Any] | None = Field(
        default=None, description="The payment link /payments' handoff created from this message (§7.6)")


@router.post("/simulate", response_model=SimulateResponse, dependencies=[Depends(require_development)])
async def simulate(body: SimulateRequest, user: CurrentUser) -> SimulateResponse:
    """Inject an inbound message on any channel and run the real intake pipeline on it."""
    external_user_id = body.external_user_id or f"sim-{body.channel}"
    external_thread_id = body.external_thread_id or external_user_id
    inbound = InboundMessage(
        channel=body.channel,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
        display_name=body.display_name,
        text=body.text,
        external_message_id=f"sim-{uuid.uuid4().hex[:12]}",
        raw_meta={"simulated_by": str(user.id)},
    )
    log.info("simulate: %s message from %s", body.channel, external_user_id)

    # What this request produced has to be told apart from this conversation's history, and the
    # background dispatcher may well deliver the reply before the call below gets to it.
    already_queued = await _outbox_ids(body.channel, external_thread_id)
    sink_marker = registry.sink.count

    result = await handle_inbound(inbound, email=str(body.email) if body.email else None,
                                 full_name=body.display_name)
    # Deliver now instead of waiting for the dispatcher's next tick, so the reply is in the response.
    await dispatcher.deliver_pending(conversation_id=result.conversation_id)
    deliveries = await _replies_from_this_call(
        result.conversation_id, already_queued, registry.sink.since(sink_marker))

    return SimulateResponse(
        channel=body.channel,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
        customer_id=result.customer_id,
        conversation_id=result.conversation_id,
        outcome=result.outcome,
        reply=result.reply,
        awaiting=result.awaiting,
        ticket_id=result.ticket_id,
        ticket_number=result.ticket_number,
        flags=result.flags,
        duplicate_count=result.duplicate_count,
        priority=result.priority,
        priority_raised=result.priority_raised,
        similarity=result.similarity,
        intent=result.intent,
        category=result.category,
        issue_type=result.issue_type,
        urgency=result.urgency,
        serial_number=result.serial_number,
        ai_summary=result.ai_summary,
        diagnostic_steps=result.diagnostic_steps,
        model=result.model,
        llm_available=result.llm_available,
        tool_calls=result.tool_calls,
        intake_ms=result.latency_ms,
        deliveries=deliveries,
        adapters_running=registry.running() or [],
        payment=result.payment,
    )


async def _outbox_ids(channel: str, external_thread_id: str) -> set[uuid.UUID]:
    """The outbox rows this conversation already had, so the new ones can be told apart."""
    async with SessionLocal() as session:
        return set((await session.execute(text(
            "SELECT o.id FROM outbox o JOIN conversations c ON c.id = o.conversation_id"
            " WHERE c.channel = :channel AND c.external_thread_id = :thread"
        ), {"channel": channel, "thread": external_thread_id})).scalars().all())


async def _replies_from_this_call(
    conversation_id: uuid.UUID, already_queued: set[uuid.UUID], sink_deliveries: list,
) -> list[SimulatedDelivery]:
    """What became of the replies this call queued, whoever delivered them.

    Read back from the outbox rather than from one dispatcher call: the background loop ticks
    every second and may have delivered the reply first, and the demo still needs to show it.
    """
    async with SessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT o.id, o.status, o.attempts, o.last_error,"
            " o.payload->>'channel' AS channel, o.payload->>'thread_id' AS thread_id,"
            " o.payload->>'text' AS payload_text, m.external_message_id"
            " FROM outbox o LEFT JOIN messages m ON m.id = o.message_id"
            " WHERE o.conversation_id = :conversation_id ORDER BY o.created_at"
        ), {"conversation_id": conversation_id})).mappings().all()

    sunk = {(d.channel, d.thread_id, d.text) for d in sink_deliveries}
    return [
        SimulatedDelivery(
            channel=row["channel"] or "",
            thread_id=row["thread_id"] or "",
            text=row["payload_text"] or "",
            ok=row["status"] == "sent",
            simulated=(row["channel"], row["thread_id"], row["payload_text"]) in sunk,
            attempts=row["attempts"],
            external_message_id=row["external_message_id"],
            error=row["last_error"],
        )
        for row in rows
        if row["id"] not in already_queued
    ]


# ---------- POST /api/dev/simulate-bank-alert (§15: the bank's SMS without the bank) ----------


class SimulateBankAlertRequest(BaseModel):
    utr: str = Field(pattern=r"^\d{12}$", description="The 12-digit UTR the customer submitted (or will)")
    amount: str = Field(max_length=20, description='Rupees, e.g. "6199" or "6199.00"')
    include_secret: bool = Field(default=True, description="False shows a spoof being rejected (no BANK_SECRET)")
    sender: EmailStr | None = Field(default=None, description="Defaults to the first BANK_ALERT_FROM address")


class SimulateBankAlertResponse(BaseModel):
    bank_alert_id: uuid.UUID | None
    duplicate: bool
    parsed_ok: bool
    reject_reason: str | None
    utr: str | None
    amount: str | None
    match: str | None = Field(description="paid | amount_mismatch | waiting_for_utr | duplicate | not_verifying")
    payment_id: str | None
    invoice_number: str | None
    sender: str
    sms: str = Field(description="The forwarded text that was parsed, with BANK_SECRET masked")


@router.post("/simulate-bank-alert", response_model=SimulateBankAlertResponse,
             dependencies=[Depends(require_development)])
async def simulate_bank_alert(
    body: SimulateBankAlertRequest, user: CurrentUser, settings: Annotated[Settings, Depends(get_settings)],
) -> SimulateBankAlertResponse:
    """Build a realistic forwarded bank credit SMS and run it through the real verifier (§7.6).

    The same checks, parser, and matcher as a mail from the inbox, so a payment it marks paid sets
    off the same payment.paid workflow. Staff login and APP_ENV=development only: it is the demo's
    backup when the phone or the bank's SMS is slow, never a way to pay in production.
    """
    try:
        amount = to_money(body.amount)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    sender = str(body.sender) if body.sender else next(iter(sorted(settings.bank_alert_senders)), "")
    if not sender:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="BANK_ALERT_FROM is empty, so there is no forwarder to simulate")
    secret = settings.bank_secret.strip()
    if body.include_secret and not secret:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="BANK_SECRET is empty: no alert can be genuine until it is set")

    now = datetime.now(IST)
    # The shape of an HDFC Bank UPI credit SMS, as an SMS-forwarding app sends it on: the SMS, then the passcode.
    sms = (f"Money Received - INR {format_inr(amount).lstrip('₹')} in your HDFC Bank A/c xx4321 on "
           f"{now:%d-%m-%y} from VPA customer@okaxis (UPI {body.utr})")
    text_body = f"{sms}\n\n{secret}" if body.include_secret else sms
    alert = AlertEmail(
        message_id=f"<sim-bank-{uuid.uuid4().hex}@servicemesh.dev>", sender=sender,
        subject=settings.bank_alert_subject or "UPI-Verify", body=text_body, received_at=now,
    )
    log.info("simulate-bank-alert by %s: UTR %s, %s, secret %s", user.id, body.utr, money_str(amount),
             "included" if body.include_secret else "left out")
    result = await ingest(alert, settings=settings)
    match = result.match
    return SimulateBankAlertResponse(
        bank_alert_id=uuid.UUID(result.alert_id) if result.alert_id else None,
        duplicate=result.duplicate,
        parsed_ok=result.verdict.ok,
        reject_reason=result.verdict.reason,
        utr=result.verdict.utr,
        amount=money_str(result.verdict.amount) if result.verdict.amount is not None else None,
        match=match.status if match else None,
        payment_id=match.payment_id if match else None,
        invoice_number=match.invoice_number if match else None,
        sender=sender,
        sms=text_body.replace(secret, "[BANK_SECRET]") if secret else text_body,
    )


class ChannelsResponse(BaseModel):
    channels: list[str]
    adapters_running: list[str]
    sink_deliveries: int


@router.get("/channels", response_model=ChannelsResponse, dependencies=[Depends(require_development)])
async def channels(user: CurrentUser) -> ChannelsResponse:
    """Which channel adapters are connected in this process, for the demo checklist (§15)."""
    return ChannelsResponse(
        channels=list(CHANNELS),
        adapters_running=registry.running(),
        sink_deliveries=registry.sink.count,
    )


# ---------- POST /api/dev/reset-demo (§15: back to the demo story in under 5 seconds) ----------


class ResetDemoResponse(BaseModel):
    ok: bool
    seconds: float = Field(description="Wall time of the whole reset, as the server saw it")
    phases: dict[str, float] = Field(description="Seconds per phase: build, serialise, truncate, insert, commit")
    rows: dict[str, int] = Field(description="Rows now in each seeded table")
    demo_customers: list[str] = Field(description="The demo-story customers, each with no open tickets")


_seed_module: Any = None


def _seed() -> Any:
    """db/seed/seed.py, loaded once. It is the same code `make seed` runs, so the two can't drift apart."""
    global _seed_module
    if _seed_module is None:
        path = REPO_ROOT / "db" / "seed" / "seed.py"
        spec = importlib.util.spec_from_file_location("servicemesh_seed", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["servicemesh_seed"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _seed_module = module
    return _seed_module


@router.post(
    "/reset-demo",
    response_model=ResetDemoResponse,
    dependencies=[Depends(require_development), Depends(require_roles("admin"))],
)
async def reset_demo(session: Annotated[AsyncSession, Depends(get_session)]) -> ResetDemoResponse:
    """Wipe every table and re-seed the demo story, then tell open dashboards to refetch."""
    # The login check left a transaction open on this session, and its read lock on staff_users
    # would make the TRUNCATE below wait on this very request.
    await session.rollback()
    started = time.perf_counter()
    done = await _seed().reseed(engine)
    # Same event the inbox already refetches on (§9); there is no ticket_id because every ticket changed.
    await bus.publish("ticket.updated", {"reason": "demo_reset"})
    seconds = time.perf_counter() - started
    seed = _seed()
    rows = done["rows"]
    log.info("reset-demo: %d tickets in %.2fs", len(rows[seed.Ticket]), seconds)
    return ResetDemoResponse(
        ok=True,
        seconds=round(seconds, 3),
        phases={name: round(value, 3) for name, value in done["phases"].items()},
        rows={model.__tablename__: len(rows[model]) for model in seed.INSERT_ORDER},
        demo_customers=[c["full_name"] for c in seed.load("customers.json")["demo_story"]],
    )