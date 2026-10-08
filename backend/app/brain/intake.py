"""The guided intake state machine (ARCHITECTURE.md §7.1), built as §4.4 describes.

Customer text is untrusted, so intake is **not** a free agent (§4.1). It is a fixed pipeline that
uses the model only to understand and to word things, and it reaches a short, hardcoded tool set
through the MCP hub. Payment, dispatch, and inventory tools are unreachable from here, enforced in
code by INTAKE_TOOLS below -- never by a prompt.

The flow, as a state machine over `conversations.context.awaiting`:

    customer message
      -> awaiting == "serial_number"?  -> the slot-filling handler below
      -> awaiting == "payment_details"?  -> the booking-details handler: extract, validate in code,
         ask only for what is missing; complete -> hand off to commands.py (§7.6)
      -> awaiting == "diagnostic_feedback"?  -> the customer's answer to /diagnose-send: one
         complete_json maps it to {step_number, result, note}; code keeps only the steps that were sent,
         the three results, and notes the customer really wrote -> tickets.record_diagnostic_results
      -> extract_serial (regex) + classification, with catalog.lookup_serial running alongside
      -> new issue with no serial?  -> ask for it (and for the email address too, when the customer
         has none on file: Discord, Telegram), awaiting = "serial_number", stop
      -> serial that the catalog doesn't know?  -> ask again; after MAX_SERIAL_MISSES misses,
         create the ticket flagged unverified_product
      -> device settled, not a duplicate, still no email?  -> ask for it, awaiting = "email"; after
         MAX_EMAIL_MISSES replies without one the ticket is raised anyway
      -> tickets.create_ticket  (concurrently with knowledge.get_playbook)
      -> MODEL_FAST writes the AI summary and the first diagnostic plan
      -> messaging.send_reply: ticket number, what happens next, and safe self-help tips for
         software issues only; and the "ticket raised" email (ticket_created) to the customer's
         address, except on the email channel, where the reply is itself that email

Email addresses are read from the customer's words by code (EMAIL_IN_TEXT), never by a model. The
confirmation is the only email intake can send, and not through INTAKE_TOOLS: see
IntakeTools.send_ticket_confirmation.

Model calls (§4.4, §4.5): the regex is free; with DECISION_PROVIDER=llm, classification and
extraction are a single `complete_json` on MODEL_FAST; the plan is a second one. Both are capped by
LLM_MAX_TOKENS_FAST. A slot-filling reply is classified once, on the first message, and never again.

Duplicate detection (§7.2) runs before a ticket is created. A message with no serial is also
checked against the customer's open tickets *before* the serial is asked for (see
`_attach_to_open_ticket`): a customer chasing their open ticket is never asked for a serial.

When no provider can answer, LLMUnavailable is caught here: the customer gets the §15 fallback
reply, and the ticket is still created when the message gave a serial and a description.

The payment_details slot is opened by the staff /payments command (app/brain/commands.py). Intake
fills it and never calls a payments tool (§4.1): once every detail is in, it hands the details to
commands.finish_payment_details, which creates the link for the ticket and service the agent chose.
"""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field

from app.brain.decide import (
    CATEGORY_OPTIONS,
    INTENT_OPTIONS,
    ISSUE_TYPE_OPTIONS,
    URGENCY_OPTIONS,
    Category,
    Decider,
    Intent,
    IssueType,
    Urgency,
    default_decider,
    extract_serial,
)
from app.brain.commands import (
    DETAILS_TTL,
    HandoffResult,
    finish_payment_details,
    mask_email,
    payment_request_is_live,
)
from app.brain.llm import LLM, LLMUnavailable, default_llm
from app.brain.mcp_hub import hub, split_name
from app.brain.prompts import load as load_prompt
from app.brain.router import ROLE_SERVERS
from app.brain.runtime import AiRunRecord, RunLogger, log_ai_run, log_run_in_background
from app.channels.base import CHANNEL_NAMES, InboundMessage
from app.channels.identity import (
    ContextStore,
    Identity,
    adopt_customer,
    customer_email,
    is_anonymous_customer,
    link_message,
    record_inbound,
    resolve,
)
from app.core.config import Settings, get_settings
from app.core.events import EventBus, bus
from app.payments.details import DetailsExtract, ask_for_missing, merge_details, missing

log = logging.getLogger(__name__)

ROLE = "intake"
TRIGGER = "message.received"

# Exactly the tools a customer message may reach. Every entry is inside
# ROLE_SERVERS["intake"] (§4.1), and tests assert that none of them belongs to payments,
# dispatch, or inventory. Anything not listed here raises ToolNotAllowed before it is called.
INTAKE_TOOLS: frozenset[str] = frozenset({
    # catalog: read, plus the one write that registers ownership on first contact (§6.3, §7.1)
    "catalog__lookup_serial",
    "catalog__lookup_model",
    "catalog__get_customer_products",
    "catalog__link_product_to_customer",
    # tickets: create / find_similar / add_followup / add_message (§4.1), and the two writes
    # §7.1's last step needs
    "tickets__create_ticket",
    "tickets__find_similar_tickets",
    "tickets__add_followup",
    "tickets__add_message",
    "tickets__get_ticket",
    "tickets__set_diagnostic_plan",
    "tickets__update_summary",
    # the customer's own answer to /diagnose-send, validated in code first (§7.1 diagnostic_feedback)
    "tickets__record_diagnostic_results",
    # knowledge: read
    "knowledge__get_playbook",
    "knowledge__suggest_next_steps",
    "knowledge__search_kb",
    # messaging: the customer reply only. No send_email, no notify_staff from customer text.
    "messaging__send_reply",
})

# The one email intake sends: the "ticket raised" confirmation (§7.1). It is not in INTAKE_TOOLS.
# IntakeTools.send_ticket_confirmation builds every argument in code (this template, a fixed subject,
# the ticket number and the device as the catalog has it), so customer text never chooses a template,
# a subject or a word of the body. One per new ticket.
CONFIRMATION_TOOL = "messaging__send_email"
CONFIRMATION_TEMPLATE = "ticket_created"

# Failed serial attempts in the awaiting state before the ticket is created unverified (§7.1).
MAX_SERIAL_MISSES = 2
# Replies with no email address in the email slot before the ticket is raised without one (§7.1).
MAX_EMAIL_MISSES = 2
# An email address in the customer's own words. Read by code, never by a model, then validated.
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Replies in the payment_details slot that give no detail at all before intake stops asking (§7.6).
MAX_DETAIL_MISSES = 3
# The same for the diagnostic_feedback slot (§7.1): three replies with nothing usable close it.
MAX_FEEDBACK_MISSES = 3
FEEDBACK_RESULTS = ("worked", "failed", "skipped")
MAX_FEEDBACK_NOTE = 200

# ---- §7.2 duplicate thresholds. They stay in code and settings, never in a prompt. ----
# DUPLICATE_SIMILARITY_THRESHOLD (0.82) comes from settings; these are the rest of the rule.
SAME_ISSUE_SIMILARITY = 0.70   # same issue_type and at least this similar -> duplicate
BORDERLINE_LOW = 0.60          # 0.60-0.70 -> one decide.is_duplicate check
DUPLICATE_PROBABILITY = 0.50   # that check calls it a duplicate at p >= this

# §4.4 urgency -> tickets.priority (§8.1). "urgent" is reached only by follow-ups (§7.2).
PRIORITY_BY_URGENCY: dict[str, str] = {"low": "low", "medium": "medium", "high": "high"}

# How many of a customer's open tickets one search returns, for "which ticket do you mean?".
OPEN_TICKET_LIMIT = 10

MAX_TITLE_CHARS = 120
MAX_DIAGNOSTIC_STEPS = 5
MAX_SELF_HELP_TIPS = 2

Outcome = Literal[
    "ticket_created", "duplicate", "awaiting_serial", "awaiting_email", "awaiting_details", "payment_link_sent",
    "booking_started", "acknowledged", "fallback", "diagnostics_recorded", "awaiting_feedback",
]
Verdict = Literal["duplicate", "borderline", "different"]


class ToolNotAllowed(RuntimeError):
    """A customer message tried to reach a tool outside INTAKE_TOOLS (§4.1)."""


# ---------- what the model fills in ----------


class ExtractedFields(BaseModel):
    """Contact details the customer gave in this message (§4.4)."""

    email: str | None = None
    full_name: str | None = None
    phone: str | None = None
    address: str | None = None


class IntakeRecord(BaseModel):
    """The §4.4 record. One complete_json call fills it when DECISION_PROVIDER=llm."""

    intent: Intent
    category: Category
    issue_type: IssueType
    summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    serial_number: str | None = None
    model_number: str | None = None
    urgency: Urgency = "medium"
    language: str = "en"
    extracted_fields: ExtractedFields = Field(default_factory=ExtractedFields)


class StepFeedback(BaseModel):
    """One step the customer mentioned. Checked in code before anything is recorded."""

    step_number: int
    result: str
    note: str | None = None


class DiagnosticFeedback(BaseModel):
    """What MODEL_FAST reads from a reply to /diagnose-send (§7.1 diagnostic_feedback)."""

    steps: list[StepFeedback] = Field(default_factory=list)
    problem_fixed: bool = False


class TicketPlan(BaseModel):
    """What MODEL_FAST writes once the ticket exists (§7.1)."""

    summary: str
    diagnostic_steps: list[str] = Field(default_factory=list)
    self_help: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class IntakeResult:
    """What one intake run did. The channel adapters and /api/dev/simulate return this."""

    outcome: Outcome
    reply: str
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    ticket_id: uuid.UUID | None = None
    ticket_number: str | None = None
    product_id: str | None = None
    serial_number: str | None = None
    flags: list[str] = field(default_factory=list)
    duplicate_count: int = 0          # §7.2: follow-ups on the ticket after this message
    priority: str | None = None
    priority_raised: bool = False
    similarity: float | None = None
    awaiting: str | None = None
    intent: str | None = None
    category: str | None = None
    issue_type: str | None = None
    urgency: str | None = None
    ai_summary: str | None = None
    diagnostic_steps: list[str] = field(default_factory=list)
    model: str | None = None            # "<provider>:<model>", as written to ai_runs
    llm_available: bool = True
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    # payment_link_sent: the link the handoff created (§7.6). Intake's own tool_calls never include it.
    payment: dict[str, Any] | None = None


# ---------- the tool gate ----------


class IntakeTools:
    """hub.call_tool, restricted to INTAKE_TOOLS, recording every call for ai_runs.

    The check is a plain set membership on the tool name, done before the hub is touched, so no
    model output and no prompt can widen it (§4.1). It is intersected with ROLE_SERVERS["intake"]
    as well, so narrowing that frozenset narrows intake too.
    """

    def __init__(self, call_tool=None, events: EventBus | None = None, *, ticket_id=None) -> None:
        self._call_tool = call_tool or hub.call_tool
        self._events = events or bus
        self.ticket_id = ticket_id
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def allows(name: str) -> bool:
        if name not in INTAKE_TOOLS:
            return False
        server, _ = split_name(name)
        return server in ROLE_SERVERS[ROLE]

    async def call(self, name: str, **args: Any) -> Any:
        if not self.allows(name):
            log.warning("intake refused tool %r: not in INTAKE_TOOLS", name[:100])
            raise ToolNotAllowed(f"intake may not call {name[:100]!r}")
        return await self._call_logged(name, args)

    async def _call_logged(self, name: str, args: dict[str, Any]) -> Any:
        """Call the hub, record the call for ai_runs, and publish it for the Agent Activity rail."""
        start = time.monotonic()
        ok = False
        try:
            result = await self._call_tool(name, args)
            ok = True
            return result
        finally:
            record = {"tool": name, "ok": ok, "ms": round((time.monotonic() - start) * 1000)}
            self.records.append(record)
            # §9: the dashboard's Agent Activity panel animates on this.
            await self._events.publish("agent.tool_called", {
                "role": ROLE, "trigger": TRIGGER,
                "ticket_id": str(self.ticket_id) if self.ticket_id else None, **record,
            })

    async def send_ticket_confirmation(self, *, to: str, ticket_id: str, ticket_number: str,
                                       device: str | None, serial: str | None, channel: str) -> Any:
        """The "ticket raised" email (§7.1), the only email intake can send.

        Outside the INTAKE_TOOLS gate on purpose, and narrower than it: the template, the subject and
        the data are all fixed here, from the ticket and the catalog, never from customer text.
        """
        data = {"ticket_number": ticket_number, "device": device, "serial": serial,
                "channel": CHANNEL_NAMES.get(channel, channel)}
        return await self._call_logged(CONFIRMATION_TOOL, {
            "to": to, "subject": f"[{ticket_number}] We've received your support request",
            "template": CONFIRMATION_TEMPLATE, "data": data, "ticket_id": ticket_id})


# ---------- customer-facing text (§7.1, §15) ----------

SERIAL_HINT = (
    "You'll find it on the sticker underneath the laptop, or run "
    "`wmic bios get serialnumber` in Command Prompt on Windows."
)
SERIAL_ASK = "Happy to help. What's the serial number of the device? " + SERIAL_HINT
SERIAL_ASK_AGAIN = (
    "Thanks, but I couldn't find that serial number in our records. "
    "Could you double-check it and send it again? " + SERIAL_HINT
)
SERIAL_NOT_SEEN = "I couldn't spot a serial number in that message. " + SERIAL_HINT
# §7.1: a customer with no email on file (Discord, Telegram) is asked for both at the start.
DETAILS_ASK = (
    "Happy to help. Please send me your email address and the serial number of the device, "
    "so I can raise a ticket and email you a confirmation. " + SERIAL_HINT
)
EMAIL_TOO = " Please include your email address as well."
EMAIL_ASK = "Thanks, I've found your {device}. What's your email address? I'll send the ticket confirmation there."
EMAIL_ASK_PLAIN = "Thanks. What's your email address? I'll send the ticket confirmation there."
EMAIL_ASK_AGAIN = (
    "I couldn't spot an email address in that. What email address should I send your ticket confirmation to?"
)
CONFIRMATION_SENT = "I've also emailed a confirmation to {email}."

# §15: the customer never gets silence when no model provider can answer.
FALLBACK_REPLY = "We've received your message and created a ticket; an agent will follow up."
FALLBACK_REPLY_NO_TICKET = "We've received your message and an agent will follow up shortly."

SMALLTALK_REPLY = (
    "Hello! Tell me what's going wrong with your device and I'll get a ticket raised for you."
)
FOLLOW_UP_REPLY = "Thanks for the update, I've added it to {ticket_number}. An agent will reply here."
FOLLOW_UP_NO_TICKET = (
    "Thanks for getting in touch. Could you describe the problem with your device, "
    "and include the serial number if you have it?"
)
DETAILS_SAVED_REPLY = "Thanks, I've saved those details. An agent will take it from here."
FEEDBACK_THANKS = "Thanks! I've noted that on ticket {ticket}: {summary}. An agent will take it from here."
FEEDBACK_FIXED = (
    "Glad to hear it's working now! I've let the agent on ticket {ticket} know; they'll check with you before "
    "closing it."
)
FEEDBACK_ASK_AGAIN = (
    "Sorry, I couldn't tell which steps you tried. Reply with the step numbers and how each went, for example: "
    "\"1 worked, 2 didn't, skipped 3\"."
)
FEEDBACK_GIVE_UP = "No problem. An agent will follow up with you here."
FEEDBACK_SAID = {"worked": "worked", "failed": "didn't help", "skipped": "skipped"}
DETAILS_GIVE_UP_REPLY = "No problem. An agent will follow up with you here about the booking."
WHICH_TICKET_REPLY = (
    "Thanks, which ticket is this about? You have these open:\n{tickets}\n"
    "Reply with the ticket number. If it's a new problem, send me the device's serial number instead."
)

# §7.2 step 5. The second line only appears when the priority actually moved.
DUPLICATE_REPLY = "This is already being handled under {ticket_number}."
DUPLICATE_REPLY_RAISED = (
    "This is already being handled under {ticket_number}. We've raised its priority."
)


# ---------- the entry point ----------


async def run_intake(
    identity: Identity,
    text: str,
    *,
    message_id: uuid.UUID | None = None,
    call_tool=None,
    llm: LLM | None = None,
    decider: Decider | None = None,
    store: ContextStore | None = None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    settings: Settings | None = None,
    payments_handoff=None,
) -> IntakeResult:
    """Run the §7.1 pipeline for one customer message. Writes one ai_runs row per run.

    `payments_handoff` is what receives completed booking details (commands.finish_payment_details
    unless a test passes a fake). It runs outside intake's tool gate on purpose, and intake's
    ai_runs row and tool_calls never include its calls.

    Never raises for a model outage: LLMUnavailable becomes the §15 fallback reply. Tool and
    database failures do propagate, because the caller (the channel adapter) has to know the
    message was not handled.
    """
    settings = settings or get_settings()
    llm = llm or default_llm()
    decider = decider or default_decider()
    store = store or ContextStore()
    events = events or bus
    tools = IntakeTools(call_tool, events, ticket_id=identity.ticket_id)

    state = _Run(
        identity=identity, text=text, message_id=message_id, tools=tools, llm=llm,
        decider=decider, store=store, settings=settings,
        payments_handoff=payments_handoff or finish_payment_details,
    )
    start = time.monotonic()
    error: str | None = None
    try:
        result = await state.execute()
        return replace(result, latency_ms=round((time.monotonic() - start) * 1000))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        # Fire-and-forget, like every other ai_runs write (§4.3 step 4).
        log_run_in_background(log_run or log_ai_run, AiRunRecord(
            role=ROLE,
            trigger=TRIGGER,
            ticket_id=state.ticket_id,
            model=",".join(dict.fromkeys(state.models)) or None,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            tool_calls=tools.records,
            latency_ms=round((time.monotonic() - start) * 1000),
            error=error,
        ))


async def handle_inbound(
    inbound: InboundMessage,
    *,
    email: str | None = None,
    full_name: str | None = None,
    **options: Any,
) -> IntakeResult:
    """The whole inbound path for one message (§6.1).

        adapter -> InboundMessage -> identity.resolve() -> intake pipeline

    Resolves the customer and conversation, stores the customer's message, publishes
    message.received (§9), runs the pipeline, and links the stored message to the ticket when
    one was created. Every channel adapter and POST /api/dev/simulate go through here, so none of
    them can skip a step. `options` are passed on to run_intake (used by the tests).
    """
    events = options.get("events") or bus
    identity = await resolve(inbound, email=email, full_name=full_name)
    message_id = await record_inbound(identity, inbound)
    await events.publish("message.received", {
        "message_id": str(message_id),
        "conversation_id": str(identity.conversation_id),
        "customer_id": str(identity.customer_id),
        "ticket_id": str(identity.ticket_id) if identity.ticket_id else None,
        "channel": inbound.channel,
        "text": inbound.text,
        "display_name": inbound.display_name,
    })
    result = await run_intake(identity, inbound.text, message_id=message_id, **options)
    if result.ticket_id is not None:
        await link_message(message_id, result.ticket_id)
    return result


@dataclass
class _Run:
    """One pass of the pipeline. Holds the bits the ai_runs row needs from every branch."""

    identity: Identity
    text: str
    message_id: uuid.UUID | None
    tools: IntakeTools
    llm: LLM
    decider: Decider
    store: ContextStore
    settings: Settings
    payments_handoff: Any = None

    models: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    ticket_id: uuid.UUID | None = None

    # ---------- state machine ----------

    async def execute(self) -> IntakeResult:
        awaiting = self.identity.context.get("awaiting")
        if awaiting == "serial_number":
            return await self._awaiting_serial()
        if awaiting == "email":
            return await self._awaiting_email()
        if awaiting == "payment_details":
            return await self._awaiting_payment_details()
        if awaiting == "diagnostic_feedback":
            return await self._awaiting_diagnostic_feedback()
        return await self._new_message()

    async def _new_message(self) -> IntakeResult:
        """A message with no slot to fill: classify it, looking the serial up alongside (§7.1)."""
        candidate = extract_serial(self.text)
        record, unit = await self._understand_and_lookup(candidate)
        if record is None:  # no provider could answer (§4.6 step 4)
            return await self._fallback(candidate, unit)

        serial = candidate or _serial_shaped(record.serial_number)
        if serial and unit is None:
            # Only the model saw a serial, so it wasn't looked up alongside the classification.
            unit = await self._lookup_serial(serial)

        if not serial and record.intent in ("new_issue", "follow_up", "provide_info"):
            # §7.2: a customer chasing an open ticket is not asked for a serial.
            attached = await self._attach_to_open_ticket(record)
            if attached is not None:
                return attached

        if record.intent == "new_issue":
            if not serial:
                ask = DETAILS_ASK if self._wants_email() else SERIAL_ASK
                return await self._ask_for_serial(ask, record, misses=0)
            if not _found(unit):
                return await self._ask_for_serial(SERIAL_ASK_AGAIN + self._email_too(), record, misses=1,
                                                  attempted=serial)
            return await self._create_ticket(record, unit, serial)

        if record.intent == "follow_up":
            return await self._chase(record, unit, serial)

        if record.intent == "payment_details":
            # §4.1: customer text never reaches the payments tools. The details are parked in
            # conversations.context for the staff-side /payments workflow to pick up (§7.6).
            return await self._save_details(record)

        return await self._acknowledge(record)

    async def _awaiting_serial(self) -> IntakeResult:
        """The slot-filling handler: this message should be the serial number (§7.1)."""
        context = dict(self.identity.context)
        pending = dict(context.get("pending") or {})
        if email := extract_email(self.text):
            pending["email"] = email  # §7.1: asked for together with the serial
        misses = int(context.get("serial_misses") or 0)
        candidate = extract_serial(self.text)
        unit = await self._lookup_serial(candidate) if candidate else None

        if _found(unit):
            # The classification from the first message still applies; no second model call.
            record = _record_from_pending(pending)
            return await self._create_ticket(record, unit, candidate, pending=pending)

        misses += 1
        record = _record_from_pending(pending)
        if misses >= MAX_SERIAL_MISSES and pending.get("description"):
            return await self._create_ticket(record, None, candidate, pending=pending, misses=misses)
        reply = (SERIAL_ASK_AGAIN if candidate else SERIAL_NOT_SEEN) + self._email_too(pending)
        return await self._ask_for_serial(reply, record, misses=misses, attempted=candidate, pending=pending)

    async def _awaiting_email(self) -> IntakeResult:
        """The email slot (§7.1): the device is settled, and this message should be the email address.

        Read by code (EMAIL_IN_TEXT, then validated), never by a model. After MAX_EMAIL_MISSES replies
        without one, the ticket is raised anyway, with no confirmation email: a customer is never stuck.
        """
        context = dict(self.identity.context)
        pending = dict(context.get("pending") or {})
        record = _record_from_pending(pending)
        if email := extract_email(self.text):
            pending["email"] = email
        else:
            misses = int(context.get("email_misses") or 0) + 1
            if misses < MAX_EMAIL_MISSES:
                context["email_misses"] = misses
                await asyncio.gather(self.store.set(self.identity.conversation_id, context),
                                     self._reply(EMAIL_ASK_AGAIN))
                return self._email_result(EMAIL_ASK_AGAIN, record, serial=pending.get("serial"))
        serial = pending.get("serial")
        unit = await self._lookup_serial(serial) if serial else None
        return await self._create_ticket(record, unit, serial, pending=pending, email_asked=True)

    async def _awaiting_payment_details(self) -> IntakeResult:
        """The booking-details slot /payments opened (§7.6): name, email, phone, service address.

        MODEL_FAST reads this message; app/payments/details.py checks every value's format in code
        and that the customer really wrote it. Only what is still missing is asked for. When the
        last detail arrives, the details go to commands.finish_payment_details -- never to a
        payments tool from here (§4.1).
        """
        context = dict(self.identity.context)
        request = context.get("payment_request")
        if not payment_request_is_live(request):
            # Stale or malformed: forget the booking and treat this as an ordinary message.
            context = {k: v for k, v in context.items()
                       if k not in ("awaiting", "collected", "payment_request", "details_misses")}
            await self.store.set(self.identity.conversation_id, context)
            self.identity = replace(self.identity, context=context)
            return await self._new_message()

        self.ticket_id = uuid.UUID(str(request["ticket_id"]))
        self.tools.ticket_id = self.ticket_id
        collected = dict(context.get("collected") or {})
        try:
            extract, result = await self.llm.complete_json(
                [
                    {"role": "system", "content": load_prompt("payment_details_extract")},
                    {"role": "user", "content": f"Still needed: {', '.join(missing(collected))}\n\n"
                                                f"Customer message:\n{self.text[:1500]}"},
                ],
                DetailsExtract,
                tier="fast",
            )
            self._note(result)
        except LLMUnavailable as e:
            log.warning("intake: no model to read booking details, sending the fallback reply: %s", e)
            await self._reply(FALLBACK_REPLY_NO_TICKET)
            return replace(self._details_result("fallback", FALLBACK_REPLY_NO_TICKET), llm_available=False)

        update = merge_details(collected, extract, self.text)
        misses = int(context.get("details_misses") or 0) + 1 if update.gave_nothing else 0
        if misses >= MAX_DETAIL_MISSES:
            context = {k: v for k, v in context.items()
                       if k not in ("awaiting", "collected", "payment_request", "details_misses")}
            await asyncio.gather(self.store.set(self.identity.conversation_id, context),
                                 self._reply(DETAILS_GIVE_UP_REPLY))
            return replace(self._details_result("acknowledged", DETAILS_GIVE_UP_REPLY), awaiting=None)

        context.update({"collected": update.collected, "details_misses": misses})
        if not update.complete:
            reply = ask_for_missing(update)
            await asyncio.gather(self.store.set(self.identity.conversation_id, context), self._reply(reply))
            return self._details_result("awaiting_details", reply)

        # Saved first, so a failed handoff loses nothing the customer typed.
        await self.store.set(self.identity.conversation_id, context)
        handoff: HandoffResult = await self.payments_handoff(
            replace(self.identity, context=context), update.collected, request)
        return replace(
            self._details_result(handoff.outcome if handoff.ok else "acknowledged", handoff.reply),
            awaiting=None, payment=handoff.payment,
        )

    async def _awaiting_diagnostic_feedback(self) -> IntakeResult:
        """The customer's answer to /diagnose-send (§7.1): which steps worked, failed or were skipped.

        One MODEL_FAST complete_json reads the reply; feedback_results keeps only the step numbers that
        were sent, the three results, and notes the customer really wrote, so a model can invent nothing.
        Anything the customer didn't mention stays pending. "It's fixed now" is only noted for the agent:
        nothing here resolves or closes a ticket. Intake still reaches no payments, dispatch or inventory
        tool (§4.1).
        """
        context = dict(self.identity.context)
        request = context.get("diagnostic_request")
        if not diagnostic_request_is_live(request):
            # Stale or malformed: forget it and treat this as an ordinary message.
            context = _without_feedback(context)
            await self.store.set(self.identity.conversation_id, context)
            self.identity = replace(self.identity, context=context)
            return await self._new_message()

        self.ticket_id = uuid.UUID(str(request["ticket_id"]))
        self.tools.ticket_id = self.ticket_id
        steps = [str(step) for step in request.get("steps") or []]
        listed = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
        try:
            extract, result = await self.llm.complete_json(
                [
                    {"role": "system", "content": load_prompt("diagnostic_feedback_extract")},
                    {"role": "user", "content": f"The steps we sent:\n{listed}\n\nCustomer reply:\n{self.text[:1500]}"},
                ],
                DiagnosticFeedback,
                tier="fast",
            )
            self._note(result)
        except LLMUnavailable as e:
            log.warning("intake: no model to read the diagnostic answer, sending the fallback reply: %s", e)
            await self._reply(FALLBACK_REPLY_NO_TICKET)
            return replace(self._feedback_result("fallback", FALLBACK_REPLY_NO_TICKET), llm_available=False)

        results = feedback_results(extract, [str(i) for i in request["step_ids"]], self.text)
        ticket = str(request.get("ticket_number") or "")
        if not results and not extract.problem_fixed:
            misses = int(context.get("feedback_misses") or 0) + 1
            if misses >= MAX_FEEDBACK_MISSES:
                context = _without_feedback(context)
                await asyncio.gather(self.store.set(self.identity.conversation_id, context),
                                     self._reply(FEEDBACK_GIVE_UP))
                return replace(self._feedback_result("acknowledged", FEEDBACK_GIVE_UP), awaiting=None)
            context["feedback_misses"] = misses
            await asyncio.gather(self.store.set(self.identity.conversation_id, context), self._reply(FEEDBACK_ASK_AGAIN))
            return self._feedback_result("awaiting_feedback", FEEDBACK_ASK_AGAIN)

        if results:
            recorded = await self.tools.call("tickets__record_diagnostic_results",
                                             ticket_id=str(self.ticket_id), results=results)
            if not isinstance(recorded, dict) or not recorded.get("ok"):
                log.warning("intake: diagnostic results not recorded: %s", recorded)
        if extract.problem_fixed:
            # Only a note for the agent: whether it is really fixed is a person's call.
            await self.tools.call(
                "tickets__add_message", ticket_id=str(self.ticket_id), sender_type="system",
                body=f"The customer's reply suggests the problem may be fixed: \"{_one_line(self.text)[:200]}\". "
                     "Check with them before resolving the ticket.")
        await self.store.set(self.identity.conversation_id, _without_feedback(context))
        number = {step_id: i for i, step_id in enumerate(request["step_ids"], start=1)}
        summary = ", ".join(f"step {number[r['step_id']]} {FEEDBACK_SAID[r['result']]}" for r in results)
        reply = FEEDBACK_FIXED.format(ticket=ticket) if extract.problem_fixed else \
            FEEDBACK_THANKS.format(ticket=ticket, summary=summary)
        await self._reply(reply)
        return replace(self._feedback_result("diagnostics_recorded", reply), awaiting=None)

    def _feedback_result(self, outcome: Outcome, reply: str) -> IntakeResult:
        return replace(self._details_result(outcome, reply), awaiting="diagnostic_feedback",
                       intent="diagnostic_feedback")

    def _details_result(self, outcome: Outcome, reply: str) -> IntakeResult:
        return IntakeResult(
            outcome=outcome,
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            ticket_id=self.ticket_id,  # the customer's details land on the ticket's timeline
            awaiting="payment_details",
            intent="payment_details",
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )

    # ---------- the model calls ----------

    async def _understand_and_lookup(self, candidate: str | None) -> tuple[IntakeRecord | None, Any]:
        """Classification and catalog.lookup_serial at the same time (one ~250 ms window)."""
        lookup = self._lookup_serial(candidate) if candidate else _none()
        record, unit = await asyncio.gather(self._understand(), lookup, return_exceptions=True)
        if isinstance(unit, BaseException):
            log.warning("intake: lookup_serial failed, carrying on unverified: %s", unit)
            unit = None
        if isinstance(record, LLMUnavailable):
            log.warning("intake: no model provider available, sending the fallback reply: %s", record)
            return None, unit
        if isinstance(record, BaseException):
            raise record
        return record, unit

    async def _understand(self) -> IntakeRecord:
        """The §4.4 record: one combined call with DECISION_PROVIDER=llm, else decide + extract."""
        if self.settings.decision_provider == "llm":
            return await self._extract()

        classification = await self.decider.classify_intake(self.text)
        needs_extraction = (
            classification.intent in ("new_issue", "payment_details") or bool(classification.low_confidence)
        )
        if not needs_extraction:
            return IntakeRecord(
                intent=classification.intent, category=classification.category,
                issue_type=classification.issue_type, urgency=classification.urgency,
                summary=_one_line(self.text),
            )
        extracted = await self._extract()
        # The typed decision wins on the fields it answered confidently (§4.6).
        confident = set(classification.low_confidence)
        return extracted.model_copy(update={
            field_name: getattr(classification, field_name)
            for field_name in ("intent", "category", "issue_type", "urgency")
            if field_name not in confident
        })

    async def _extract(self) -> IntakeRecord:
        record, result = await self.llm.complete_json(
            [
                {"role": "system", "content": _extract_prompt()},
                {"role": "user", "content": self.text},
            ],
            IntakeRecord,
            tier="fast",
        )
        self._note(result)
        return record

    async def _write_plan(self, record: IntakeRecord, playbook: Any, product: str | None) -> TicketPlan | None:
        """MODEL_FAST writes the AI summary and the opening diagnostic plan (§7.1)."""
        steps = _playbook_steps(playbook)
        context = [
            f"Device: {product or 'not identified'}",
            f"Problem ({record.category}/{record.issue_type}): {record.summary or _one_line(self.text)}",
            f"Customer wrote: {self.text[:600]}",
        ]
        if steps:
            context.append("Playbook steps to follow:\n" + "\n".join(f"- {s}" for s in steps))
        try:
            plan, result = await self.llm.complete_json(
                [
                    {"role": "system", "content": load_prompt("intake_plan")},
                    {"role": "user", "content": "\n".join(context)},
                ],
                TicketPlan,
                tier="fast",
            )
        except LLMUnavailable as e:
            # The ticket already exists, so this is a degraded success, not a failure (§15).
            log.warning("intake: no AI summary or plan for this ticket: %s", e)
            if steps:
                return TicketPlan(summary="", diagnostic_steps=steps[:MAX_DIAGNOSTIC_STEPS])
            return None
        self._note(result)
        return plan

    def _note(self, result: Any) -> None:
        """Record the provider, model, and tokens of one model call for the ai_runs row."""
        self.models.append(f"{result.provider}:{result.model}")
        self.input_tokens = _add(self.input_tokens, result.input_tokens)
        self.output_tokens = _add(self.output_tokens, result.output_tokens)

    # ---------- the tool steps ----------

    async def _lookup_serial(self, serial: str | None) -> Any:
        if not serial:
            return None
        return await self.tools.call("catalog__lookup_serial", serial_number=serial)

    async def _create_ticket(
        self,
        record: IntakeRecord,
        unit: Any,
        serial: str | None,
        *,
        pending: dict[str, Any] | None = None,
        misses: int = 0,
        already_searched: list[dict[str, Any]] | None = None,
        email_asked: bool = False,
    ) -> IntakeResult:
        """§7.1 from the duplicate check to messaging.send_reply and the confirmation email.

        `already_searched` carries the §7.2 candidates when the caller has fetched them, so the
        follow-up branch does not pay for the same search (and the same embedding) twice.
        `email_asked` is set by the email slot, which has asked for the address all it will.
        """
        pending = pending or {}
        description = _description(pending.get("description"), self.text)
        title = _one_line(record.summary or pending.get("title") or description)

        flags: list[str] = []
        product_id = model_id = product_category = product_name = None
        if _found(unit):
            product_id, model_id = unit.get("product_id"), unit.get("model_id")
            product_category = unit.get("category")
            product_name = unit.get("model_name")
            if unit.get("in_warranty") is False:
                flags.append("out_of_warranty")
            # Settles the identity too, so the duplicate search below runs as the right customer.
            settled = await self._settle_ownership(
                unit, (pending or {}).get("email") or extract_email(self.text), email_asked=email_asked)
            if settled is None:
                # Someone else's serial on a chat account with nothing on it: their email decides who they are.
                return await self._ask_for_email(record, unit, serial, pending, product_name)
            flags += settled
        else:
            # §7.1: the serial was never confirmed, so the agent verifies the device.
            flags.append("unverified_product")

        # Both read-only and independent of each other. The playbook is only needed if this
        # turns out to be a new ticket, but fetching it alongside saves a round trip in the
        # common case and it is cached for a minute anyway (mcp_hub.CACHEABLE_TOOLS).
        similar, playbook = await asyncio.gather(
            _already(already_searched) if already_searched is not None
            else self._find_similar(description, product_id),
            self.tools.call(
                "knowledge__get_playbook",
                issue_type=record.issue_type,
                category=product_category,
                model_id=model_id,
            ),
            return_exceptions=True,
        )
        if isinstance(playbook, BaseException):
            log.warning("intake: no playbook for this ticket: %s", playbook)
            playbook = None
        if isinstance(similar, BaseException):
            # A failed duplicate search must not stop a customer raising a ticket.
            log.warning("intake: duplicate search failed, treating this as new: %s", similar)
            similar = []

        if already_searched is None:
            followup = await self._duplicate_of(similar, record, description)
            if followup is not None:
                return await self._add_followup(followup, record)

        # §7.1: the device is settled (an adopted account may have brought its email with it), so ask
        # for the email only now, and only when there is still none.
        if not email_asked and self._wants_email(pending):
            return await self._ask_for_email(record, unit, serial, pending, product_name)
        recipient = await self._confirmation_address(pending)

        ticket = await self.tools.call(
                "tickets__create_ticket",
                customer_id=str(self.identity.customer_id),
                product_id=product_id,
                category=record.category,
                issue_type=record.issue_type,
                title=title,
                description=description,
                source_channel=self.identity.channel,
                conversation_id=str(self.identity.conversation_id),
                flags=flags,
                priority=PRIORITY_BY_URGENCY.get(record.urgency, "medium"),
        )
        if not ticket.get("ticket_id"):
            raise RuntimeError(f"create_ticket did not create a ticket: {ticket}")

        self.ticket_id = uuid.UUID(ticket["ticket_id"])
        self.tools.ticket_id = self.ticket_id
        ticket_number = ticket["ticket_number"]

        plan = await self._write_plan(record, playbook, product_name)
        summary = (plan.summary or "").strip() if plan else ""
        steps = [s.strip() for s in (plan.diagnostic_steps if plan else []) if s.strip()][:MAX_DIAGNOSTIC_STEPS]
        # §7.1: safe self-help tips for software issues only. Enforced here, not in the prompt.
        tips = [t.strip() for t in (plan.self_help if plan else []) if t.strip()][:MAX_SELF_HELP_TIPS]
        if record.category != "software":
            tips = []

        unit_serial = unit.get("serial_number") if _found(unit) else None
        emailed = mask_email(recipient) if recipient else None
        reply = (
            _ticket_reply(ticket_number, product_name, flags, tips, unit_serial, emailed=emailed)
            if plan is not None or summary or steps
            else FALLBACK_REPLY + f" Your reference is {ticket_number}."
            + (" " + CONFIRMATION_SENT.format(email=emailed) if emailed else "")
        )

        # The closing writes are independent of each other.
        await asyncio.gather(
            self._update_summary(summary),
            self._store_plan(steps),
            self.tools.call(
                "messaging__send_reply",
                conversation_id=str(self.identity.conversation_id),
                text=reply,
            ),
            self.store.set(self.identity.conversation_id, _cleared_context(self.identity.context)),
            self._send_confirmation(recipient, ticket_number, product_name, unit_serial),
        )

        return IntakeResult(
            outcome="ticket_created",
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            ticket_id=self.ticket_id,
            ticket_number=ticket_number,
            product_id=product_id,
            serial_number=serial,
            flags=list(ticket.get("flags") or flags),
            intent=record.intent,
            category=record.category,
            issue_type=record.issue_type,
            urgency=record.urgency,
            ai_summary=summary or None,
            diagnostic_steps=steps,
            model=",".join(dict.fromkeys(self.models)) or None,
            llm_available=plan is not None,
            tool_calls=self.tools.records,
        )

    async def _chase(self, record: IntakeRecord, unit: Any, serial: str | None) -> IntakeResult:
        """A customer chasing a problem they already reported (§7.2).

        This is the case §7.2 is written for: "a complaint on Discord and a later email about the
        same battery land on the same ticket once the identities are linked". Such a message
        rarely repeats the serial, so the candidate search is scoped by the product only when
        this message told us which one it is -- otherwise by customer alone, which is what makes
        the cross-channel example in §7.2 work at all.

        Nothing to attach it to? With a confirmed device it is a new issue after all; without one
        there is nothing to file, so the customer is asked what the problem is.
        """
        product_id = unit.get("product_id") if _found(unit) else None
        matches = await self._find_similar(self.text, product_id)
        match = await self._duplicate_of(matches, record, self.text)
        if match is not None:
            return await self._add_followup(match, record)
        if _found(unit):
            return await self._create_ticket(record, unit, serial, already_searched=matches)
        return await self._acknowledge(record)

    async def _attach_to_open_ticket(self, record: IntakeRecord) -> IntakeResult | None:
        """A message with no serial that belongs to a ticket the customer already has (§7.2).

        The ticket is the conversation's own, if it is still open, else the customer's only open
        ticket. A follow_up or provide_info goes straight onto it. A new_issue only does when the
        §7.2 scoring says it is that ticket; otherwise it is a different problem and the caller
        asks for the serial. With several open tickets and no conversation ticket, the best §7.2
        match wins, and when none passes the customer is asked which one (never for a serial).

        Returns None when there is nothing to attach to, and the caller carries on as before.
        """
        try:
            matches = await self._find_similar(self.text, None, limit=OPEN_TICKET_LIMIT)
        except Exception as e:
            # A failed search must not leave the customer unanswered: carry on as a new message.
            log.warning("intake: open-ticket search failed, carrying on without it: %s", e)
            return None

        current = await self._conversation_ticket()
        if current is None and len(matches) == 1:
            current = matches[0]

        if current is not None:
            if record.intent != "new_issue":
                return await self._add_followup(current, record)
            same = [m for m in matches if str(m.get("ticket_id")) == str(current.get("ticket_id"))]
            match = await self._duplicate_of(same, record, self.text)
            if match is not None:
                return await self._add_followup(match, record)

        if len(matches) > 1:
            match = _named_ticket(matches, self.text) or await self._duplicate_of(matches, record, self.text)
            if match is not None:
                return await self._add_followup(match, record)
            return await self._ask_which_ticket(matches, record)
        return None

    async def _conversation_ticket(self) -> dict[str, Any] | None:
        """The conversation's ticket, when it has one and it is still open."""
        if not self.identity.ticket_id:
            return None
        try:
            found = await self.tools.call("tickets__get_ticket", ticket_id=str(self.identity.ticket_id))
        except Exception as e:
            log.warning("intake: could not read the conversation's ticket: %s", e)
            return None
        if not isinstance(found, dict) or not found.get("found") or found.get("status") in ("resolved", "closed"):
            return None
        return found

    async def _ask_which_ticket(self, matches: list[dict[str, Any]], record: IntakeRecord) -> IntakeResult:
        """Several open tickets and no clear match: list them instead of asking for a serial."""
        listing = "\n".join(
            f"- {m.get('ticket_number')}: {_one_line(str(m.get('title') or ''))}" for m in matches)
        reply = WHICH_TICKET_REPLY.format(tickets=listing)
        await self._reply(reply)
        return replace(self._no_ticket("acknowledged", reply, record), ticket_id=None)

    async def _find_similar(
        self, description: str, product_id: str | None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """§7.2 step 1: open tickets for the same customer and product, ranked by similarity."""
        extra = {"limit": limit} if limit else {}
        found = await self.tools.call(
            "tickets__find_similar_tickets",
            customer_id=str(self.identity.customer_id),
            product_id=product_id,
            text=description,
            **extra,
        )
        return list(found.get("matches") or []) if isinstance(found, dict) else []

    async def _duplicate_of(
        self, matches: list[dict[str, Any]], record: IntakeRecord, description: str
    ) -> dict[str, Any] | None:
        """The ticket this message is a follow-up on, or None (§7.2 steps 2 and 3).

        The thresholds are code and settings. The only model involved is the borderline
        decide.is_duplicate check, and even that only returns a probability: this code decides.
        """
        if not matches:
            return None
        threshold = self.settings.duplicate_similarity_threshold
        match, verdict = pick_duplicate(matches, record.issue_type, threshold=threshold)
        if match is None or verdict == "different":
            return None
        if verdict == "duplicate":
            log.info("intake: this is a duplicate of %s (similarity %.3f)",
                     match.get("ticket_number"), _as_float(match.get("similarity")) or 0.0)
            return match
        try:
            probability = await self.decider.is_duplicate(description, ticket_text(match))
        except LLMUnavailable as e:
            # Borderline and no decision available: raise a new ticket rather than quietly
            # folding a possibly different problem into an old one.
            log.warning("intake: borderline duplicate undecided, treating it as new: %s", e)
            return None
        self.models.append(f"decide:{self.settings.decision_provider}")
        log.info("intake: borderline %.3f against %s -> p(same problem)=%.2f",
                 _as_float(match.get("similarity")) or 0.0, match.get("ticket_number"), probability)
        return match if probability >= DUPLICATE_PROBABILITY else None

    async def _add_followup(self, match: dict[str, Any], record: IntakeRecord) -> IntakeResult:
        """§7.2 steps 4 and 5: link the message, bump the count, raise priority, reply."""
        ticket_id = match["ticket_id"]
        self.ticket_id = uuid.UUID(str(ticket_id))
        self.tools.ticket_id = self.ticket_id

        result = await self.tools.call(
            "tickets__add_followup",
            ticket_id=str(ticket_id),
            message_id=str(self.message_id) if self.message_id else None,
            channel=self.identity.channel,
        )
        raised = bool(result.get("priority_raised"))
        ticket_number = result.get("ticket_number") or match.get("ticket_number") or ""
        template = DUPLICATE_REPLY_RAISED if raised else DUPLICATE_REPLY
        reply = template.format(ticket_number=ticket_number)

        await asyncio.gather(
            self._reply(reply),
            self.store.set(self.identity.conversation_id, _cleared_context(self.identity.context)),
        )
        return IntakeResult(
            outcome="duplicate",
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            ticket_id=self.ticket_id,
            ticket_number=ticket_number,
            duplicate_count=result.get("duplicate_count") or 0,
            priority=result.get("priority"),
            priority_raised=raised,
            similarity=_as_float(match.get("similarity")),
            intent=record.intent,
            category=record.category,
            issue_type=record.issue_type,
            urgency=record.urgency,
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )

    async def _settle_ownership(self, unit: Any, typed_email: str | None, *, email_asked: bool) -> list[str] | None:
        """Settle who owns the unit and who the customer is (§6.3, §7.1). Returns the ticket's flags, or None
        when that can only be decided once the customer has given their email.

        All fixed pipeline steps rather than tools the model can choose:

        - nobody owns the unit: register it to this customer (first contact).
        - this customer owns it: nothing to do.
        - somebody else owns it, and this customer has history or an email of their own: a different
          person, so the ticket is flagged `ownership_mismatch` for the agent. The unit is never reassigned.
        - somebody else owns it, and this is a chat account with nothing on it (Discord, Telegram): the
          owner writing in from a new platform, or someone else who knows the serial. The serial alone
          never decides: only an email that matches the owner's links the account to the owner (§6.3).
          No email yet: None, and the caller asks for it. A different email, or none after asking: a
          different person, flagged `ownership_mismatch`; their ticket and confirmation stay their own.

        This used to link on the serial alone, so a Discord user who typed their own email and somebody
        else's serial got merged into that customer, and the confirmation went to the owner's address.
        """
        owner = unit.get("customer_id")
        mine = str(self.identity.customer_id)
        if owner is None:
            linked = await self.tools.call(
                "catalog__link_product_to_customer", product_id=unit["product_id"], customer_id=mine)
            return ["ownership_mismatch"] if linked.get("ownership_mismatch") else []
        if str(owner) == mine:
            return []
        if not await is_anonymous_customer(self.identity.customer_id):
            return ["ownership_mismatch"]
        if typed_email is None:
            return None if not email_asked else ["ownership_mismatch"]
        owner_email = await customer_email(uuid.UUID(str(owner)))
        if owner_email and owner_email.strip().casefold() == typed_email.strip().casefold():
            self.identity = await adopt_customer(self.identity, uuid.UUID(str(owner)))
            return []
        return ["ownership_mismatch"]

    async def _update_summary(self, summary: str) -> None:
        if summary:
            await self.tools.call("tickets__update_summary", ticket_id=str(self.ticket_id), summary=summary)

    async def _store_plan(self, steps: list[str]) -> None:
        if steps:
            await self.tools.call(
                "tickets__set_diagnostic_plan", ticket_id=str(self.ticket_id), steps=steps, suggested_by="ai")

    async def _reply(self, text: str) -> None:
        await self.tools.call(
            "messaging__send_reply", conversation_id=str(self.identity.conversation_id), text=text)

    # ---------- the customer's email and the confirmation (§7.1) ----------

    def _wants_email(self, pending: dict[str, Any] | None = None) -> bool:
        """Ask for an email? Not on the email channel (the reply is itself an email), and only when
        there is none on file and the customer hasn't typed one yet."""
        if self.identity.channel == "email" or self.identity.customer_email:
            return False
        return not ((pending or {}).get("email") or extract_email(self.text))

    def _email_too(self, pending: dict[str, Any] | None = None) -> str:
        return EMAIL_TOO if self._wants_email(pending) else ""

    async def _confirmation_address(self, pending: dict[str, Any] | None) -> str | None:
        """Where the "ticket raised" email goes, or None.

        The address on file wins: a chat message never changes a customer's email (§6.3). A typed one
        is saved to the customer only when they have none and no other customer has it; the
        confirmation goes to it either way, since it is the address this person gave.
        """
        if self.identity.channel == "email":
            return None
        if self.identity.customer_email:
            return self.identity.customer_email
        typed = (pending or {}).get("email") or extract_email(self.text)
        if not typed:
            return None
        filled = await self.store.fill_in_email(self.identity.customer_id, typed)
        if filled:
            self.identity = replace(self.identity, customer_email=filled)
        return typed

    async def _send_confirmation(self, to: str | None, ticket_number: str, device: str | None,
                                 serial: str | None) -> None:
        """Queue the confirmation. Never raises: the ticket and the chat reply matter more."""
        if not to:
            return
        try:
            queued = await self.tools.send_ticket_confirmation(
                to=to, ticket_id=str(self.ticket_id), ticket_number=ticket_number, device=device,
                serial=serial, channel=self.identity.channel)
            if not isinstance(queued, dict) or not queued.get("ok"):
                log.warning("intake: the confirmation for %s was refused: %s", ticket_number, queued)
        except Exception:
            log.exception("intake: the confirmation email for %s was not queued", ticket_number)

    async def _ask_for_email(self, record: IntakeRecord, unit: Any, serial: str | None,
                             pending: dict[str, Any] | None, product_name: str | None) -> IntakeResult:
        """The device is settled but there is no email yet: keep everything and ask for it."""
        kept = dict(pending or {})
        if not kept.get("description"):
            kept.update({"description": self.text, "title": _one_line(record.summary or self.text),
                         "category": record.category, "issue_type": record.issue_type, "urgency": record.urgency})
        kept["serial"] = (unit.get("serial_number") if _found(unit) else None) or serial
        context = {**_cleared_context(self.identity.context), "awaiting": "email", "email_misses": 0,
                   "pending": kept}
        reply = EMAIL_ASK.format(device=product_name) if _found(unit) and product_name else EMAIL_ASK_PLAIN
        await asyncio.gather(self.store.set(self.identity.conversation_id, context), self._reply(reply))
        return self._email_result(reply, record, serial=kept["serial"])

    def _email_result(self, reply: str, record: IntakeRecord, *, serial: str | None = None) -> IntakeResult:
        return IntakeResult(
            outcome="awaiting_email",
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            awaiting="email",
            serial_number=serial,
            intent=record.intent,
            category=record.category,
            issue_type=record.issue_type,
            urgency=record.urgency,
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )

    # ---------- the branches that don't create a ticket ----------

    async def _ask_for_serial(
        self,
        reply: str,
        record: IntakeRecord,
        *,
        misses: int,
        attempted: str | None = None,
        pending: dict[str, Any] | None = None,
    ) -> IntakeResult:
        """Ask for the serial and remember the problem, so the ticket can be built from the reply."""
        kept = dict(pending or {})
        if not kept.get("description"):
            kept = {
                "description": self.text,
                "title": _one_line(record.summary or self.text),
                "category": record.category,
                "issue_type": record.issue_type,
                "urgency": record.urgency,
            }
        if email := extract_email(self.text):
            kept["email"] = email
        context = {
            **self.identity.context,
            "awaiting": "serial_number",
            "serial_misses": misses,
            "pending": kept,
        }
        if attempted:
            context["last_serial_attempt"] = attempted
        await asyncio.gather(self.store.set(self.identity.conversation_id, context), self._reply(reply))
        return IntakeResult(
            outcome="awaiting_serial",
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            awaiting="serial_number",
            serial_number=attempted,
            intent=record.intent,
            category=record.category,
            issue_type=record.issue_type,
            urgency=record.urgency,
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )

    async def _save_details(self, record: IntakeRecord) -> IntakeResult:
        """Park contact details in conversations.context (§4.1): no payment tool is reachable."""
        given = {k: v for k, v in record.extracted_fields.model_dump().items() if v}
        context = {
            **self.identity.context,
            "collected": {**(self.identity.context.get("collected") or {}), **given},
        }
        await asyncio.gather(
            self.store.set(self.identity.conversation_id, context), self._reply(DETAILS_SAVED_REPLY))
        return self._no_ticket("acknowledged", DETAILS_SAVED_REPLY, record)

    async def _acknowledge(self, record: IntakeRecord) -> IntakeResult:
        """follow_up, provide_info, smalltalk, other: reply, never silence (§15).

        A follow-up on an existing ticket is only acknowledged here; §7.2's add_followup flow
        (duplicate detection, priority raising) is the next step and is deliberately not stubbed.
        """
        if record.intent == "follow_up" or self.identity.ticket_id:
            reply = (
                FOLLOW_UP_REPLY.format(ticket_number=await self._ticket_number())
                if self.identity.ticket_id
                else FOLLOW_UP_NO_TICKET
            )
        elif record.intent == "smalltalk":
            reply = SMALLTALK_REPLY
        else:
            reply = FOLLOW_UP_NO_TICKET
        await self._reply(reply)
        return self._no_ticket("acknowledged", reply, record)

    async def _ticket_number(self) -> str:
        found = await self.tools.call("tickets__get_ticket", ticket_id=str(self.identity.ticket_id))
        return found.get("ticket_number") or "your open ticket"

    async def _fallback(self, candidate: str | None, unit: Any) -> IntakeResult:
        """§15: no model provider answered. Still create the ticket when we can.

        A serial and a description are enough to raise a ticket without any model: the category
        is unknown, and an agent fills the rest in.
        """
        ticket_id = ticket_number = None
        flags: list[str] = []
        product_id = None
        if candidate and self.text.strip():
            if unit is None:
                try:
                    unit = await self._lookup_serial(candidate)
                except Exception as e:
                    log.warning("intake fallback: lookup_serial failed: %s", e)
                    unit = None
            if _found(unit):
                product_id = unit.get("product_id")
            else:
                flags.append("unverified_product")
            ticket = await self.tools.call(
                "tickets__create_ticket",
                customer_id=str(self.identity.customer_id),
                product_id=product_id,
                category="unknown",
                issue_type="other",
                title=_one_line(self.text),
                description=self.text,
                source_channel=self.identity.channel,
                conversation_id=str(self.identity.conversation_id),
                flags=flags,
            )
            ticket_id = ticket.get("ticket_id")
            ticket_number = ticket.get("ticket_number")
            if ticket_id:
                self.ticket_id = uuid.UUID(ticket_id)
                self.tools.ticket_id = self.ticket_id

        reply = FALLBACK_REPLY + f" Your reference is {ticket_number}." if ticket_number else FALLBACK_REPLY_NO_TICKET
        await self._reply(reply)
        return IntakeResult(
            outcome="fallback",
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            ticket_id=self.ticket_id,
            ticket_number=ticket_number,
            product_id=product_id,
            serial_number=candidate,
            flags=flags,
            category="unknown" if ticket_number else None,
            issue_type="other" if ticket_number else None,
            llm_available=False,
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )

    def _no_ticket(self, outcome: Outcome, reply: str, record: IntakeRecord) -> IntakeResult:
        return IntakeResult(
            outcome=outcome,
            reply=reply,
            conversation_id=self.identity.conversation_id,
            customer_id=self.identity.customer_id,
            ticket_id=self.identity.ticket_id,
            intent=record.intent,
            category=record.category,
            issue_type=record.issue_type,
            urgency=record.urgency,
            model=",".join(dict.fromkeys(self.models)) or None,
            tool_calls=self.tools.records,
        )


# ---------- helpers ----------


def _extract_prompt() -> str:
    """The extraction prompt plus the enum meanings, kept in decide.py as the single source."""
    blocks = [
        ("intent", INTENT_OPTIONS),
        ("category", CATEGORY_OPTIONS),
        ("issue_type", ISSUE_TYPE_OPTIONS),
        ("urgency", URGENCY_OPTIONS),
    ]
    lines = [
        f"- {name}: " + "; ".join(f"{option} = {why}" for option, why in options.items())
        for name, options in blocks
    ]
    return load_prompt("intake_extract") + "\n" + "\n".join(lines)


async def _none() -> None:
    return None


async def _already(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A finished search, so one gather() can take either a fresh call or a reused result."""
    return matches


def duplicate_verdict(similarity: float, same_issue_type: bool, *, threshold: float) -> Verdict:
    """§7.2 step 3, as a pure rule over one candidate.

    At or above `threshold` (DUPLICATE_SIMILARITY_THRESHOLD, 0.82) it is a duplicate. The same
    issue_type makes SAME_ISSUE_SIMILARITY (0.70) enough. Between BORDERLINE_LOW and
    SAME_ISSUE_SIMILARITY it is borderline, worth one decide.is_duplicate check. Below that it
    is a different problem.
    """
    if similarity >= threshold:
        return "duplicate"
    if same_issue_type and similarity >= SAME_ISSUE_SIMILARITY:
        return "duplicate"
    if BORDERLINE_LOW <= similarity <= SAME_ISSUE_SIMILARITY:
        return "borderline"
    return "different"


def pick_duplicate(
    matches: list[dict[str, Any]], issue_type: str, *, threshold: float
) -> tuple[dict[str, Any] | None, Verdict]:
    """The candidate to act on, and what it is.

    A clear duplicate wins over a more similar candidate that is only borderline, because the
    same issue_type is part of the score (§7.2 step 2), not a tie-break. Candidates arrive
    most-similar-first from pgvector.
    """
    borderline: dict[str, Any] | None = None
    for match in matches:
        similarity = _as_float(match.get("similarity"))
        if similarity is None:
            continue
        same_issue = str(match.get("issue_type") or "") == issue_type
        verdict = duplicate_verdict(similarity, same_issue, threshold=threshold)
        if verdict == "duplicate":
            return match, "duplicate"
        if verdict == "borderline" and borderline is None:
            borderline = match
    if borderline is not None:
        return borderline, "borderline"
    return None, "different"


def _named_ticket(matches: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """The open ticket whose number the customer typed, e.g. when answering "which ticket?"."""
    upper = text.upper()
    for match in matches:
        number = str(match.get("ticket_number") or "").upper()
        if number and number in upper:
            return match
    return None


def ticket_text(match: dict[str, Any]) -> str:
    """The existing ticket as text, for the borderline decide.is_duplicate check."""
    parts = [match.get("title"), match.get("ai_summary") or match.get("description")]
    return "\n".join(part for part in parts if part)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _found(unit: Any) -> bool:
    return bool(isinstance(unit, dict) and unit.get("found") and unit.get("product_id"))


def _serial_shaped(value: str | None) -> str | None:
    """Keep a model-extracted serial only when it really looks like one (§4.4)."""
    return extract_serial(value) if value else None


def _one_line(text: str) -> str:
    line = " ".join((text or "").split())
    return line[:MAX_TITLE_CHARS].rstrip() or "Support request"


def _description(pending: str | None, latest: str) -> str:
    """The original problem text, plus anything new the serial reply added."""
    if not pending:
        return latest
    extra = latest.strip()
    if extra and extra.lower() not in pending.lower() and len(extra) > 24:
        return f"{pending}\n\n{extra}"
    return pending


def extract_email(text: str | None) -> str | None:
    """The first valid email address in the customer's own words, lower-cased (§7.1). No model."""
    for match in EMAIL_IN_TEXT.finditer(text or ""):
        try:
            return validate_email(match.group(0), check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            continue
    return None


def _record_from_pending(pending: dict[str, Any]) -> IntakeRecord:
    """Rebuild the §4.4 record saved when the serial was first asked for: no second model call."""
    return IntakeRecord(
        intent="new_issue",
        category=pending.get("category") or "unknown",
        issue_type=pending.get("issue_type") or "other",
        urgency=pending.get("urgency") or "medium",
        summary=pending.get("title") or "",
    )


def diagnostic_request_is_live(request: Any, *, now: datetime | None = None) -> bool:
    """Whether a conversation's diagnostic_request was set by /diagnose-send and is under 24 hours old."""
    if not isinstance(request, dict) or not request.get("step_ids"):
        return False
    try:
        uuid.UUID(str(request.get("ticket_id")))
        requested = datetime.fromisoformat(str(request.get("requested_at")))
    except (TypeError, ValueError):
        return False
    return requested.tzinfo is not None and (now or datetime.now(UTC)) - requested <= DETAILS_TTL


def feedback_results(extract: DiagnosticFeedback, step_ids: list[str], text: str) -> list[dict[str, Any]]:
    """The model's reading, as only code lets it through: a step number from the list that was sent, one of
    the three results, the first mention of each step, and a note only when the customer wrote those words
    (cut to 200 characters). Everything else is dropped, so unmentioned steps stay pending."""
    said = " ".join(text.split()).casefold()
    out: dict[str, dict[str, Any]] = {}
    for item in extract.steps:
        result = (item.result or "").strip().lower()
        if not 1 <= item.step_number <= len(step_ids) or result not in FEEDBACK_RESULTS:
            continue
        step_id = step_ids[item.step_number - 1]
        if step_id in out:
            continue
        note = " ".join((item.note or "").split())
        out[step_id] = {"step_id": step_id, "result": result,
                        "notes": note[:MAX_FEEDBACK_NOTE] if note and note.casefold() in said else None}
    return list(out.values())


def _without_feedback(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if k not in ("awaiting", "diagnostic_request", "feedback_misses")}


def _cleared_context(context: dict[str, Any]) -> dict[str, Any]:
    """The conversation's context with the serial slot closed."""
    return {k: v for k, v in context.items() if k not in ("awaiting", "serial_misses", "pending",
                                                          "last_serial_attempt", "email_misses")}


def _playbook_steps(playbook: Any) -> list[str]:
    if not isinstance(playbook, dict) or not playbook.get("found"):
        return []
    steps = []
    for entry in playbook.get("steps") or []:
        step = entry.get("step") if isinstance(entry, dict) else str(entry)
        if step and step.strip():
            steps.append(step.strip())
    return steps


def _ticket_reply(ticket_number: str, product: str | None, flags: list[str], tips: list[str],
                  serial: str | None = None, *, emailed: str | None = None) -> str:
    # The device as the catalog found it, serial included, so the customer can see it's the right one.
    device = f" for your {product}" + (f" (serial {serial})" if serial else "") if product else ""
    lines = [f"Thanks! I've created ticket {ticket_number}{device}."]
    if emailed:  # masked: an adopted account's address is never shown in full in a chat
        lines.append(CONFIRMATION_SENT.format(email=emailed))
    if "unverified_product" in flags:
        lines.append("I couldn't confirm that serial number, so an agent will check the device with you.")
    if "ownership_mismatch" in flags:
        lines.append("That serial is registered to a different account, so an agent will confirm the details.")
    lines.append("A support agent will review it and reply to you here.")
    if tips:
        lines.append("While you wait, it's safe to try:")
        lines += [f"- {tip}" for tip in tips]
    return "\n".join(lines)


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


# Re-exported so callers and tests have one import for the regex (§4.4).
__all__ = [
    "INTAKE_TOOLS",
    "MAX_SERIAL_MISSES",
    "IntakeRecord",
    "IntakeResult",
    "IntakeTools",
    "ToolNotAllowed",
    "TicketPlan",
    "duplicate_verdict",
    "extract_email",
    "extract_serial",
    "handle_inbound",
    "pick_duplicate",
    "run_intake",
]