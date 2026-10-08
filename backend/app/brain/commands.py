"""Slash commands (ARCHITECTURE.md §7.5): the nine built-ins and the agents' custom commands.

POST /api/tickets/{id}/commands runs a command for a staff member and streams its progress
(app/api/commands.py). A built-in command is a fixed pipeline, not a free agent: the model may at
most pick which catalog service the agent's words mean (/payments, /schedule, /parts) or write the
words of a summary (/summary) or a question to the customer (/ask), and code does everything else.

    /payments [service]  below, §7.6
    /diagnose            knowledge.suggest_next_steps -> tickets.set_diagnostic_plan (playbook steps)
    /diagnose-send       the pending checklist steps (after /diagnose's playbook step if the checklist is
                         empty) -> messaging.send_reply, a fixed numbered template -> context.awaiting =
                         diagnostic_feedback -> tickets.update_status(awaiting_customer). Intake's
                         diagnostic_feedback slot reads the answer (§7.1)
    /summary             tickets.get_ticket -> MODEL_FAST writes the summary -> tickets.update_summary
    /ask <what>          MODEL_FAST phrases the question (a fixed template if it can't, or adds a
                         number) -> messaging.send_reply -> tickets.update_status(awaiting_customer)
    /schedule [service]  /payments' warranty path only: a repair that isn't free is refused. Books at
                         once when the customer's address and phone are on file, else asks for them.
    /parts [service]     the service's part for this device -> inventory.find_compatible_part (stock)
    /escalate <reason>   priority up one level (timeline event) -> messaging.notify_staff(role=admin)
    /close <note>        refused while a job or payment is open -> closing reply -> resolved

A custom command (slash_commands, §7.5) is the agent's own prompt template, rendered with plain
{{var}} substitution from a fixed list of variables (never eval), and run through the copilot tool
loop with only its allowed_tools. The same gates apply as everywhere: router.filter_tools and
runtime.run_tool_loop drop MODEL_FORBIDDEN_TOOLS whatever the command lists, and every call is
checked against the command's own list again before the hub is reached.

/payments [service], in order:

    tickets.get_ticket -> catalog.lookup_serial (warranty, model)
    -> the service: the agent's words, or (none) the ticket's, matched in code; at most one decide choice
    -> the warranty, in code: a covered repair in warranty is free (money.WARRANTY_COVERED_SERVICES)
    catalog.get_service_price (shown to the agent; the invoice is computed again, in code, §5.5)
    -> conversations.context.awaiting = payment_details on the customer's own conversation
    -> messaging.send_reply: the price (or "free under warranty") and please send full name, email,
       phone, address
    -> tickets.update_status(awaiting_customer), the note saying which service, why, warranty, price

The customer's replies are read by intake's payment_details slot filling (§7.1), which never calls a
payments tool (§4.1). When every field is in, intake hands off to `finish_payment_details` below:
the address is saved, and with the ticket and service the *agent* chose (customer text supplies
contact details only, never an amount or a service), either payments.create_payment_request runs and
the customer gets the link on their channel plus the invoice email, or, for a free warranty repair,
the booking starts at once (workflows.start_warranty_booking), authorized by the staff member who ran
/payments.

Every tool call goes through CommandTools, an allowlist checked in code, and publishes
agent.tool_called; each run writes one ai_runs row (§4.3 step 4).
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.brain.decide import Decider, Question, default_decider
from app.brain.llm import LLM, LLMUnavailable, default_llm
from app.brain.mcp_hub import McpToolError, hub, split_name
from app.brain.prompts import load as load_prompt
from app.brain.router import (
    BILLING_PIPELINE_TOOLS,
    MODEL_FORBIDDEN_TOOLS,
    ROLE_SERVERS,
    WORKFLOW_ONLY_TOOLS,
    compact_tools,
    filter_tools,
)
from app.brain.runtime import AiRunRecord, RunLogger, log_ai_run, log_run_in_background, run_tool_loop
from app.brain.writer import invented_numbers
from app.channels.identity import ContextStore, Identity
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.events import Event, EventBus, bus
from app.payments.invoice import load_invoice
from app.payments.money import format_inr, free_under_warranty, money_str

log = logging.getLogger(__name__)

ROLE = "copilot"
PAYMENTS_TRIGGER = "/payments"

# Exactly the tools /payments may reach, checked before the hub is called.
PAYMENTS_TOOLS: frozenset[str] = frozenset({
    "tickets__get_ticket",
    "tickets__update_status",
    "catalog__lookup_serial",
    "catalog__lookup_model",
    "catalog__get_service_price",
    "payments__create_payment_request",
    "messaging__send_reply",
    "messaging__send_email",
    "messaging__notify_staff",
})


@dataclass(frozen=True)
class Builtin:
    """One §7.5 built-in: what the menu shows, and the only tools its pipeline may call."""

    name: str
    usage: str        # "/ask <what>"
    description: str  # one line, for the menu and for the suggestion chips' yes/no question
    args: str         # "none", "optional" or "required"
    tools: frozenset[str]
    example: str = ""  # words an agent might add after it


# In menu order. Each command's tools are checked in code (CommandTools) before the hub is called.
BUILTINS: dict[str, Builtin] = {b.name: b for b in (
    Builtin("payments", "/payments [service]",
            "Bill the repair: price it, ask the customer for their details, then send the UPI payment link",
            "optional", PAYMENTS_TOOLS, "display replacement"),
    Builtin("diagnose", "/diagnose",
            "Add the playbook's next diagnostic steps to the checklist, skipping what was already tried",
            "none", frozenset({"tickets__get_ticket", "knowledge__suggest_next_steps", "tickets__set_diagnostic_plan"})),
    Builtin("diagnose-send", "/diagnose-send",
            "Send the pending diagnostic steps to the customer; their answer ticks the checklist",
            "none", frozenset({"tickets__get_ticket", "knowledge__suggest_next_steps", "tickets__set_diagnostic_plan",
                               "messaging__send_reply", "tickets__update_status"})),
    Builtin("summary", "/summary",
            "Rewrite the AI summary from the whole conversation, across every channel",
            "none", frozenset({"tickets__get_ticket", "tickets__update_summary"})),
    Builtin("ask", "/ask <what>",
            "Ask the customer for specific information, phrased professionally, on their own channel",
            "required", frozenset({"tickets__get_ticket", "messaging__send_reply", "tickets__update_status"}),
            "a photo of the error message"),
    Builtin("schedule", "/schedule [service]",
            "Book a free warranty repair without payment; a repair that isn't free is refused",
            "optional", frozenset({"tickets__get_ticket", "tickets__update_status", "catalog__lookup_serial",
                                   "catalog__lookup_model", "catalog__get_service_price", "messaging__send_reply"}),
            "display replacement"),
    Builtin("parts", "/parts [service]",
            "Show the compatible part for this device and its stock",
            "optional", frozenset({"tickets__get_ticket", "catalog__lookup_serial", "catalog__lookup_model",
                                   "inventory__find_compatible_part"}),
            "battery"),
    Builtin("escalate", "/escalate <reason>",
            "Raise the priority one level and notify the admins",
            "required", frozenset({"tickets__get_ticket", "messaging__notify_staff"}),
            "customer waited a week"),
    Builtin("close", "/close <note>",
            "Resolve the ticket and send the customer a closing message",
            "required", frozenset({"tickets__get_ticket", "tickets__update_status", "messaging__send_reply"}),
            "battery replaced, customer confirmed"),
)}
BUILT_COMMANDS = frozenset(BUILTINS)

# How long the customer has to send their details before the request is treated as stale.
DETAILS_TTL = timedelta(hours=24)

DETAILS_LIST = (
    "Please reply with:\n"
    "- your full name\n"
    "- your email address\n"
    "- a 10-digit mobile number\n"
    "- {where}"
)
WHERE_VISIT = ("the full service address where the technician should come "
               "(flat or house number and street, city, and the 6-digit PIN code)")
WHERE_SHIPPED = ("the full address we should send it to "
                 "(flat or house number and street, city, and the 6-digit PIN code)")
ASK_DETAILS = "To book the {service} for your {device}, the total comes to {total} ({breakdown}). " + DETAILS_LIST
IN_WARRANTY_ASK = (
    "Good news: your {device} is under warranty until {until}, so the {service} is free of charge. "
    "To book it, " + DETAILS_LIST[0].lower() + DETAILS_LIST[1:]
)
FREE_BOOKING_REPLY = (
    "Thanks, {name}! Your {service} is free under warranty. I'm booking it now and will confirm "
    "the details here shortly."
)
FREE_DELAYED_REPLY = "Thanks, I've got your details. An agent will confirm your booking here shortly."
LINK_REPLY = (
    "Thanks, {name}! Here's the payment link for invoice {invoice} ({total}):\n{url}\n"
    "Open it, scan the QR code with any UPI app, then enter the 12-digit UTR (UPI reference number) "
    "from your payment app on the same page. The link expires on {expires}. {email_note}"
)
LINK_DELAYED_REPLY = "Thanks, I've got your details. An agent will send your payment link here shortly."
# /diagnose-send: a fixed template, so the steps reach the customer exactly as stored (no model).
DIAGNOSTICS_ASK = (
    "Hi {name}, to help us with ticket {ticket}, could you please try these steps and tell us how each one went?\n"
    "{steps}\n"
    "Reply with what worked, what didn't, or which you skipped, for example: \"1 worked, 2 didn't, skipped 3\"."
)
# /ask when the model can't phrase it, or its wording added a number the agent never wrote.
ASK_TEMPLATE = "Hi {name}, to help us with your {device} (ticket {ticket}), could you please send us {what}? Just reply here."
CLOSE_REPLY = (
    "Hi {name}, we've marked ticket {ticket} ({title}) as resolved. If anything still isn't right, "
    "just reply here and we'll pick it up again."
)
# The ai_summary column is shown to agents only; this caps what /summary stores.
MAX_SUMMARY_CHARS = 1200
SUMMARY_MAX_TOKENS = 250
ASK_MAX_TOKENS = 200
PRIORITIES = ["low", "medium", "high", "urgent"]


class CommandError(Exception):
    """Shown to the agent as the command's error. Nothing was sent to the customer."""


class CommandToolNotAllowed(CommandError):
    """A command tried to reach a tool outside its allowlist."""


Progress = Callable[[dict[str, Any]], Awaitable[None]]


async def _no_progress(event: dict[str, Any]) -> None:
    return None


# ---------- the tool gate ----------


class CommandTools:
    """hub.call_tool, restricted to an allowlist, recording every call for ai_runs.

    Used by the commands here (role copilot) and by the fixed automations (role automation: the
    payment.paid workflow and the UPI verifier's notifications). The allowlist is intersected with
    ROLE_SERVERS[role] in code, like intake's (§4.1).

    `model_driven` marks a gate whose calls come from a model (a custom command's tool loop): it
    refuses every tool in MODEL_FORBIDDEN_TOOLS, including create_payment_request, which the
    built-in /payments pipeline alone is allowed to call in code (BILLING_PIPELINE_TOOLS).
    """

    def __init__(self, allowed: frozenset[str], call_tool=None, events: EventBus | None = None, *,
                 trigger: str, ticket_id: uuid.UUID | str | None = None, role: str = ROLE,
                 model_driven: bool = False) -> None:
        self._allowed = allowed
        self.model_driven = model_driven
        self._call_tool = call_tool or hub.call_tool
        self._events = events or bus
        self.role = role
        self.trigger = trigger
        self.ticket_id = ticket_id
        self.records: list[dict[str, Any]] = []

    def allows(self, name: str) -> bool:
        if name not in self._allowed:
            return False
        # The fixed automations are code, not a model: they alone may use the stock and dispatch
        # writers, and a built-in pipeline (not a model-driven gate) may create the payment link it
        # bills with. Every other forbidden tool stays out of reach of every gate.
        if name in MODEL_FORBIDDEN_TOOLS:
            fixed_code = (self.role == "automation" and name in WORKFLOW_ONLY_TOOLS) or (
                not self.model_driven and name in BILLING_PIPELINE_TOOLS)
            if not fixed_code:
                return False
        server, _ = split_name(name)
        return server in ROLE_SERVERS[self.role]

    async def call(self, name: str, **args: Any) -> Any:
        if not self.allows(name):
            raise CommandToolNotAllowed(f"{self.trigger} may not call {name[:100]!r}")
        start = time.monotonic()
        ok = False
        try:
            result = await self._call_tool(name, args)
            ok = not (isinstance(result, dict) and result.get("ok") is False)
            return result
        finally:
            record = {"tool": name, "ok": ok, "ms": round((time.monotonic() - start) * 1000)}
            self.records.append(record)
            await self._events.publish("agent.tool_called", {
                "role": self.role, "trigger": self.trigger,
                "ticket_id": str(self.ticket_id) if self.ticket_id else None, **record,
            })


# ---------- database reads and writes no MCP tool covers ----------


class CommandData:
    """What /payments reads and writes outside the MCP tools: the service list, the customer's
    conversation, the open payment, and the booking details. Tests pass a fake."""

    async def services(self) -> list[dict[str, Any]]:
        async with SessionLocal() as session:
            rows = (await session.execute(text(
                "SELECT code, name, part_type, requires_visit FROM service_catalog ORDER BY code"))).mappings().all()
        return [dict(row) for row in rows]

    async def customer_conversation(self, ticket_id: str, customer_id: str) -> dict[str, Any] | None:
        """The conversation the customer last wrote on about this ticket: their own channel (§6.1)."""
        async with SessionLocal() as session:
            row = (await session.execute(text("""
                SELECT c.id AS conversation_id, c.channel
                FROM conversations c
                WHERE c.customer_id = CAST(:customer AS uuid)
                  AND c.id = COALESCE(
                    (SELECT m.conversation_id FROM messages m
                     WHERE m.ticket_id = CAST(:ticket AS uuid) AND m.sender_type = 'customer'
                       AND m.conversation_id IS NOT NULL
                     ORDER BY m.created_at DESC LIMIT 1),
                    (SELECT c2.id FROM conversations c2 WHERE c2.ticket_id = CAST(:ticket AS uuid)
                     ORDER BY c2.last_message_at DESC LIMIT 1))
            """), {"ticket": ticket_id, "customer": customer_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def open_payment(self, ticket_id: str) -> dict[str, Any] | None:
        async with SessionLocal() as session:
            row = (await session.execute(text(
                "SELECT id, invoice_number, status FROM payments WHERE ticket_id = CAST(:t AS uuid)"
                " AND (status = 'verifying' OR (status = 'pending' AND expires_at > now())) LIMIT 1"),
                {"t": ticket_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def save_details(self, customer_id: str, details: dict[str, Any]) -> dict[str, Any]:
        """Save the service address (with the customer's maps link, if they pasted one) and the
        customer's name and phone. Returns the address id and the email on the customer's record.

        The email is filled in only when the record has none (§6.3): changing a customer's email
        from a chat message would hand their account to whoever wrote it, because the email channel
        resolves customers by address.
        """
        address = details["address"]
        async with SessionLocal() as session:
            address_id = (await session.execute(text(
                "SELECT id FROM addresses WHERE customer_id = CAST(:c AS uuid) AND lower(line1) = lower(:line1)"
                " AND lower(city) = lower(:city) AND coalesce(postal_code, '') = :pin LIMIT 1"),
                {"c": customer_id, "line1": address["line1"], "city": address["city"],
                 "pin": address["postal_code"]})).scalar()
            if address_id is None:
                address_id = (await session.execute(text(
                    "INSERT INTO addresses (customer_id, line1, line2, city, state, postal_code, location_url,"
                    " is_default) VALUES (CAST(:c AS uuid), :line1, :line2, :city, :state, :pin, :url, TRUE)"
                    " RETURNING id"),
                    {"c": customer_id, "line1": address["line1"], "line2": address.get("line2"),
                     "city": address["city"], "state": address.get("state"),
                     "pin": address["postal_code"], "url": details.get("location_url")})).scalar()
            elif details.get("location_url"):  # the same address, now with the customer's maps link
                await session.execute(text("UPDATE addresses SET location_url = :url WHERE id = CAST(:a AS uuid)"),
                                      {"url": details["location_url"], "a": str(address_id)})
            await session.execute(text(
                "UPDATE addresses SET is_default = (id = CAST(:a AS uuid)) WHERE customer_id = CAST(:c AS uuid)"),
                {"a": str(address_id), "c": customer_id})
            await session.execute(text(
                "UPDATE customers SET full_name = :name, phone = :phone WHERE id = CAST(:c AS uuid)"),
                {"name": details["full_name"], "phone": details["phone"], "c": customer_id})
            savepoint = await session.begin_nested()
            try:
                await session.execute(text(
                    "UPDATE customers SET email = :email WHERE id = CAST(:c AS uuid) AND email IS NULL"),
                    {"email": details["email"], "c": customer_id})
                await savepoint.commit()
            except Exception as e:  # the address belongs to another customer (customers.email is UNIQUE)
                await savepoint.rollback()
                log.info("booking email is another customer's; customer %s keeps theirs (%s)",
                         customer_id, type(e).__name__)
            email = (await session.execute(text(
                "SELECT email FROM customers WHERE id = CAST(:c AS uuid)"), {"c": customer_id})).scalar()
            await session.commit()
        return {"address_id": str(address_id), "email": email}

    async def invoice(self, payment_id: str) -> dict[str, Any] | None:
        return await load_invoice(payment_id=payment_id)

    async def open_job(self, ticket_id: str) -> dict[str, Any] | None:
        """A technician job on the ticket that isn't completed or cancelled."""
        async with SessionLocal() as session:
            row = (await session.execute(text(
                "SELECT id, status, scheduled_date FROM service_jobs WHERE ticket_id = CAST(:t AS uuid)"
                " AND status NOT IN ('completed', 'cancelled') LIMIT 1"), {"t": ticket_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def saved_address(self, customer_id: str) -> dict[str, Any] | None:
        """The customer's default address, when their phone is on file too: what /schedule needs to
        book at once (the technician calls to agree the time, §7.7)."""
        async with SessionLocal() as session:
            row = (await session.execute(text(
                "SELECT a.id AS address_id, a.line1, a.city, a.postal_code FROM addresses a"
                " JOIN customers c ON c.id = a.customer_id"
                " WHERE a.customer_id = CAST(:c AS uuid) AND coalesce(c.phone, '') <> ''"
                " ORDER BY a.is_default DESC, a.created_at DESC LIMIT 1"), {"c": customer_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def raise_priority(self, ticket_id: str, priority: str, previous: str, reason: str, staff_id: str) -> None:
        """/escalate: the new priority and a priority_raised timeline event, in one transaction.
        No MCP tool sets a priority; this is the same write as PATCH /api/tickets/{id} (§10)."""
        async with SessionLocal() as session:
            await session.execute(text(
                "UPDATE tickets SET priority = :p, updated_at = now() WHERE id = CAST(:t AS uuid)"),
                {"p": priority, "t": ticket_id})
            await session.execute(text(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor)"
                " VALUES (CAST(:t AS uuid), 'priority_raised', CAST(:payload AS jsonb), :actor)"),
                {"t": ticket_id, "actor": staff_id, "payload": json.dumps(
                    {"priority": priority, "from": previous, "note": f"Escalated: {reason}"})})
            await session.commit()

    async def diagnostics_sent(self, ticket_id: str, staff_id: str, payload: dict[str, Any]) -> None:
        """/diagnose-send's diagnostics_sent timeline event, the staff member as its actor. No MCP tool
        writes an arbitrary timeline event; this is the same kind of write as raise_priority's."""
        async with SessionLocal() as session:
            await session.execute(text(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor)"
                " VALUES (CAST(:t AS uuid), 'diagnostics_sent', CAST(:payload AS jsonb), :actor)"),
                {"t": ticket_id, "actor": staff_id, "payload": json.dumps(payload)})
            await session.commit()

    async def custom_command(self, name: str, owner_id: Any) -> dict[str, Any] | None:
        """The staff member's own custom command with this name (§7.5: per agent)."""
        async with SessionLocal() as session:
            row = (await session.execute(text(
                "SELECT id, name, description, prompt_template, allowed_tools FROM slash_commands"
                " WHERE name = :n AND owner_id = CAST(:o AS uuid) AND NOT is_builtin"),
                {"n": name, "o": str(owner_id)})).mappings().one_or_none()
        return dict(row) if row else None


# ---------- results ----------


@dataclass(frozen=True)
class CommandResult:
    outcome: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HandoffResult:
    """What finish_payment_details did with the customer's completed details."""

    ok: bool
    reply: str
    payment: dict[str, Any] | None = None
    error: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "payment_link_sent"  # or "booking_started" for a free warranty repair


# ---------- the entry point ----------


class CommandNotFound(CommandError):
    """No built-in, and none of the staff member's own custom commands, has this name."""


async def run_command(
    ticket_id: uuid.UUID,
    name: str,
    args: str,
    staff: Any,
    *,
    progress: Progress | None = None,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    settings: Settings | None = None,
    store: ContextStore | None = None,
    decider: Decider | None = None,
    data: CommandData | None = None,
    llm: LLM | None = None,
    start_booking: Callable[..., Any] | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
) -> CommandResult:
    """Run one command for a staff member. Raises CommandError with a message for the agent.

    A built-in runs its fixed pipeline. Any other name is the staff member's own custom command,
    run through the copilot tool loop (run_custom_command); CommandNotFound when there is none.
    """
    name = normalize_name(name)
    data = data or CommandData()
    spec = BUILTINS.get(name)
    if spec is None:
        custom = await data.custom_command(name, staff.id)
        if custom is None:
            raise CommandNotFound(f"No command named /{name}. Type / in the composer to see yours.")
        return await run_custom_command(ticket_id, custom, args, staff, progress=progress, call_tool=call_tool,
                                        events=events, log_run=log_run, data=data, llm=llm, tool_specs=tool_specs)
    if spec.args == "required" and not args.strip():
        raise CommandError(f"Add words after the command: {spec.usage}, e.g. /{spec.name} {spec.example}.")
    events = events or bus
    trigger = f"/{name}"
    run = _Run(
        name=name, ticket_id=str(ticket_id), args=args, staff=staff, progress=progress or _no_progress,
        tools=CommandTools(spec.tools, call_tool, events, trigger=trigger, ticket_id=ticket_id),
        settings=settings or get_settings(), store=store or ContextStore(),
        decider=decider or default_decider(), data=data, events=events, llm=llm, start_booking=start_booking,
    )
    start = time.monotonic()
    error: str | None = None
    try:
        return await run.execute()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        log_run_in_background(log_run or log_ai_run, AiRunRecord(
            role=ROLE, trigger=trigger, ticket_id=ticket_id, model=run.model,
            input_tokens=run.input_tokens, output_tokens=run.output_tokens, tool_calls=run.tools.records,
            latency_ms=round((time.monotonic() - start) * 1000), error=error,
        ))


def normalize_name(name: str) -> str:
    return name.strip().lstrip("/").strip().lower()


@dataclass
class _Run:
    """One built-in command's run: the shared steps, then one method per command."""

    name: str
    ticket_id: str
    args: str
    staff: Any
    progress: Progress
    tools: CommandTools
    settings: Settings
    store: ContextStore
    decider: Decider
    data: CommandData
    events: EventBus
    llm: LLM | None = None
    start_booking: Callable[..., Any] | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    async def step(self, step: str, detail: str, status: str = "ok") -> None:
        await self.progress({"type": "step", "step": step, "status": status, "detail": detail})

    async def execute(self) -> CommandResult:
        handlers = {
            "payments": self.payments, "diagnose": self.diagnose, "diagnose-send": self.diagnose_send,
            "summary": self.summary, "ask": self.ask,
            "schedule": self.schedule, "parts": self.parts, "escalate": self.escalate, "close": self.close,
        }
        return await handlers[self.name]()

    # ---------- shared steps ----------

    async def load_ticket(self, *, refuse_closed: str | None = None) -> dict[str, Any]:
        ticket = await self.tools.call("tickets__get_ticket", ticket_id=self.ticket_id)
        if not isinstance(ticket, dict) or not ticket.get("found"):
            raise CommandError("Ticket not found.")
        if refuse_closed and ticket.get("status") in ("resolved", "closed"):
            raise CommandError(f"{ticket['ticket_number']} is {ticket['status']}; reopen it before {refuse_closed}.")
        customer = ticket.get("customer") or {}
        await self.step("ticket", f"{ticket['ticket_number']} · {customer.get('full_name') or 'customer'}")
        return ticket

    async def load_unit(self, ticket: dict[str, Any], why: str) -> tuple[dict[str, Any], str]:
        """The ticket's device from the catalog (warranty, model), and how to name it."""
        product = ticket.get("product") or {}
        if not product.get("serial_number"):
            raise CommandError(f"This ticket has no verified device, so {why}. "
                               "Confirm the serial number with the customer first.")
        unit = await self.tools.call("catalog__lookup_serial", serial_number=product["serial_number"])
        if not isinstance(unit, dict) or not unit.get("found"):
            raise CommandError(f"Serial {product['serial_number']} isn't in the catalog.")
        device = f"{unit.get('model_name')} ({unit['serial_number']})"
        await self.step("device", f"{device}, {unit.get('category') or 'device'}")
        return unit, device

    async def conversation(self, ticket: dict[str, Any]) -> dict[str, Any]:
        conversation = await self.data.customer_conversation(self.ticket_id, ticket["customer_id"])
        if conversation is None:
            raise CommandError("The customer has no conversation on this ticket to reply on.")
        return conversation

    async def price(self, service: dict[str, Any], unit: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        """The catalog price (shown to the agent; an invoice is computed again, in code, §5.5)."""
        price = await self.tools.call("catalog__get_service_price", service_code=service["code"],
                                      model_id=unit.get("model_id"))
        if not isinstance(price, dict) or not price.get("found"):
            raise CommandError(f"{service['code']} has no price in the catalog.")
        if service.get("part_type") and not price.get("part"):
            raise CommandError(f"No {service['part_type']} part is compatible with the {unit.get('model_name')}.")
        total = format_inr(str(price["total"]))
        breakdown = ", ".join(f"{_short_label(item['label'])} {format_inr(str(item['amount']))}"
                              for item in price["line_items"])
        return price, total, breakdown

    async def refuse_open_job(self, ticket: dict[str, Any]) -> None:
        job = await self.data.open_job(self.ticket_id)
        if job is not None:
            raise CommandError(f"{ticket['ticket_number']} already has an open technician job "
                               f"({job['status']}, {job['scheduled_date']}).")

    def used(self, result: Any) -> None:
        """Record which model a run's own call used, for its ai_runs row."""
        self.model = f"{result.provider}:{result.model}"
        self.input_tokens = (self.input_tokens or 0) + (result.input_tokens or 0)
        self.output_tokens = (self.output_tokens or 0) + (result.output_tokens or 0)

    # ---------- /payments (§7.6) ----------

    async def payments(self) -> CommandResult:
        ticket = await self.load_ticket(refuse_closed="billing")
        unit, device = await self.load_unit(ticket, "the service can't be priced")

        service, why = await self.resolve_service(ticket, unit)
        await self.step("service", f"{service['code']} · {service['name']}: {why}")

        free = free_under_warranty(service["code"], bool(unit.get("in_warranty")))
        warranty = warranty_text(unit, service["code"])
        await self.step("warranty", warranty)

        conversation = await self.conversation(ticket)
        price, total, breakdown = await self.price(service, unit)
        await self.refuse_open_job(ticket)
        if free:
            await self.step("price", f"free under warranty (out of warranty it would be {total})")
        else:
            await self.step("price", f"{total} ({breakdown})")
            if not self.settings.upi_configured:
                raise CommandError("UPI_ID and UPI_PAYEE_NAME are not set, so a payment link can't be paid. "
                                   "Set them in backend/.env first.")
            existing = await self.data.open_payment(self.ticket_id)
            if existing is not None:
                raise CommandError(f"{ticket['ticket_number']} already has an open payment "
                                   f"{existing['invoice_number']} ({existing['status']}). Cancel it first.")
        return await self.ask_for_details(ticket, unit, device, service, why, warranty, free, price, total,
                                          breakdown, conversation)

    async def ask_for_details(self, ticket: dict[str, Any], unit: dict[str, Any], device: str,
                              service: dict[str, Any], why: str, warranty: str, free: bool, price: dict[str, Any],
                              total: str, breakdown: str, conversation: dict[str, Any]) -> CommandResult:
        """Open the customer's payment_details slot (§7.6 step 1) and ask for name, email, phone, address."""
        context = await self.store.get(conversation["conversation_id"])
        context = {k: v for k, v in context.items()
                   if k not in ("pending", "serial_misses", "last_serial_attempt", "details_misses")}
        request = {
            "ticket_id": self.ticket_id,
            "ticket_number": ticket["ticket_number"],
            "customer_id": str(ticket["customer_id"]),
            "service_code": service["code"],
            "service_name": service["name"],
            "requested_by": str(self.staff.id),
            "requested_by_name": getattr(self.staff, "name", None),
            "requested_at": datetime.now(UTC).isoformat(),
            # Decided here, in code, from the warranty: the handoff books a free repair at once
            # instead of billing it. Customer text can't set it (§4.1).
            "free": free,
        }
        if self.name != "payments":
            request["command"] = f"/{self.name}"
        context.update({"awaiting": "payment_details", "collected": {}, "payment_request": request})
        await self.store.set(conversation["conversation_id"], context)
        where = WHERE_VISIT if price.get("requires_visit", True) else WHERE_SHIPPED
        if free:
            ask = IN_WARRANTY_ASK.format(device=device, until=unit.get("warranty_until"),
                                         service=service["name"].lower(), where=where)
        else:
            ask = ASK_DETAILS.format(service=service["name"].lower(), device=device, total=total,
                                     breakdown=breakdown, where=where)
        await self.tools.call("messaging__send_reply", conversation_id=str(conversation["conversation_id"]), text=ask)
        await self.step("ask", f"asked the customer on {conversation['channel']} for name, email, phone and address"
                               + (" (free under warranty)" if free else ""))
        billing = "Free under warranty, no invoice" if free else f"{total} ({breakdown})"
        await self.tools.call("tickets__update_status", ticket_id=self.ticket_id, status="awaiting_customer",
                              note=f"/{self.name} {service['code']}: {why}. {warranty}. {billing}. "
                                   "Asked the customer for booking details.")
        await self.step("status", "awaiting_customer")
        if free:
            return CommandResult(
                "in_warranty",
                "Free under warranty: no invoice. Asked the customer for their details; the booking starts "
                "when they reply.",
                {"service_code": service["code"], "why": why, "free": True, "channel": conversation["channel"],
                 "conversation_id": str(conversation["conversation_id"]), "reply": ask},
            )
        return CommandResult(
            "awaiting_payment_details",
            f"Asked the customer for their details. The payment link goes out when they reply ({total}).",
            {"service_code": service["code"], "why": why, "free": False, "total": money_str(str(price["total"])),
             "channel": conversation["channel"], "conversation_id": str(conversation["conversation_id"]),
             "reply": ask},
        )

    # ---------- /schedule: a free warranty repair, no payment (§7.5, §7.7) ----------

    async def schedule(self) -> CommandResult:
        ticket = await self.load_ticket(refuse_closed="scheduling a repair")
        unit, device = await self.load_unit(ticket, "its warranty can't be checked")
        service, why = await self.resolve_service(ticket, unit)
        await self.step("service", f"{service['code']} · {service['name']}: {why}")
        free = free_under_warranty(service["code"], bool(unit.get("in_warranty")))
        warranty = warranty_text(unit, service["code"])
        await self.step("warranty", warranty)
        if not free:
            # Decided in code from the warranty: /schedule never books a repair someone should pay for.
            raise CommandError(f"{service['name']} isn't free under warranty ({warranty}). "
                               f"Use /payments to bill it.")
        conversation = await self.conversation(ticket)
        price, total, breakdown = await self.price(service, unit)
        await self.refuse_open_job(ticket)
        await self.step("price", f"free under warranty (out of warranty it would be {total})")

        address = await self.data.saved_address(str(ticket["customer_id"]))
        if address is None:
            return await self.ask_for_details(ticket, unit, device, service, why, warranty, True, price, total,
                                              breakdown, conversation)
        where = f"{address['line1']}, {address['city']}"
        await self.tools.call("tickets__update_status", ticket_id=self.ticket_id, status="in_progress",
                              note=f"/schedule {service['code']}: {why}. {warranty}. Free under warranty, no invoice. "
                                   f"Booking at the address on file ({where}).")
        start_booking = self.start_booking
        if start_booking is None:
            from app.brain import workflows  # workflows imports this module

            start_booking = workflows.start_warranty_booking
        start_booking(ticket_id=self.ticket_id, service_code=service["code"], address_id=str(address["address_id"]),
                      request_key=datetime.now(UTC).isoformat(), staff_id=str(self.staff.id),
                      staff_name=getattr(self.staff, "name", None), command="/schedule")
        await self.step("booking", f"started for the address on file: {where}")
        return CommandResult(
            "booking_started",
            "Free under warranty: booking the visit at the customer's saved address. The technician and the "
            "customer's confirmation appear in Agent Activity.",
            {"service_code": service["code"], "why": why, "free": True, "address": where},
        )

    # ---------- /parts ----------

    async def parts(self) -> CommandResult:
        ticket = await self.load_ticket()
        unit, _ = await self.load_unit(ticket, "its parts can't be looked up")
        services = await self.data.services()
        best: dict[str, Any] | None = None
        if self.args.strip():
            best, tied = match_service(self.args, services)
            if best is None and tied and len({s.get("part_type") for s in tied}) == 1:
                best = tied[0]  # "ssd": a replacement and an upgrade take the same part
        if best is not None:
            service, why = best, f"the agent asked for \"{self.args.strip()[:60]}\""
        else:
            service, why = await self.resolve_service(ticket, unit)
        await self.step("service", f"{service['code']} · {service['name']}: {why}")
        if not service.get("part_type"):
            return CommandResult("no_part", f"{service['name']} needs no part.", {"service_code": service["code"]})
        part = await self.tools.call("inventory__find_compatible_part", model_id=unit.get("model_id"),
                                     part_type=service["part_type"])
        if not isinstance(part, dict) or not part.get("found"):
            return CommandResult("no_part", f"No {service['part_type']} part fits the {unit.get('model_name')}.",
                                 {"service_code": service["code"]})
        await self.step("part", f"{part['sku']} · {part['name']} (₹{part['unit_price']})")
        for row in part.get("stock") or []:
            await self.step("stock", f"{row['warehouse_name']}: {row['on_hand']} on hand, {row['reserved']} reserved, "
                                     f"{row['available']} available" + (", running low" if row.get("low") else ""),
                            status="warning" if row.get("low") else "ok")
        if not part.get("stock"):
            await self.step("stock", "not stocked in any warehouse", status="warning")
        low = any(row.get("low") for row in part.get("stock") or [])
        return CommandResult(
            "stock",
            f"{part['sku']} fits the {unit.get('model_name')}: {part['available']} available"
            + (" (running low)." if low else "."),
            {"sku": part["sku"], "part_name": part["name"], "available": part["available"], "low": low,
             "stock": part.get("stock") or []},
        )

    # ---------- /diagnose ----------

    async def diagnose(self) -> CommandResult:
        ticket = await self.load_ticket()
        found = await self.tools.call("knowledge__suggest_next_steps", ticket_id=self.ticket_id, limit=5)
        if not isinstance(found, dict) or not found.get("found"):
            raise CommandError("Ticket not found.")
        tried = {"worked": found.get("worked") or [], "failed": found.get("failed") or []}
        if not found.get("playbook_title"):
            return CommandResult("no_playbook",
                                 f"There's no playbook for {ticket.get('issue_type') or 'this'} issues on this device "
                                 "yet, so there are no steps to suggest.", tried)
        await self.step("playbook", str(found["playbook_title"]))
        steps = [str(s.get("step")).strip() for s in found.get("next_steps") or [] if str(s.get("step") or "").strip()]
        if not steps:
            return CommandResult("nothing_new", "Every step in the playbook is already on the checklist.", tried)
        added = await self.tools.call("tickets__set_diagnostic_plan", ticket_id=self.ticket_id, steps=steps,
                                      suggested_by="playbook")
        if not isinstance(added, dict) or not added.get("ok"):
            raise CommandError(f"Couldn't add the steps: {added.get('error') if isinstance(added, dict) else added}")
        for text_ in steps:
            await self.step("next", text_)
        count = added.get("added", len(steps))
        return CommandResult("steps_added",
                             f"Added {count} playbook step{'s' if count != 1 else ''} to the diagnostics checklist.",
                             {"steps": steps, **tried})

    # ---------- /diagnose-send ----------

    async def diagnose_send(self) -> CommandResult:
        """The pending steps to the customer, numbered, in a fixed template; their reply fills the
        diagnostic_feedback slot (§7.1), and the steps stay pending until it does."""
        ticket = await self.load_ticket(refuse_closed="sending diagnostic steps")
        if not ticket.get("diagnostic_steps"):
            await self.step("checklist", "empty: adding the playbook's steps first")
            await self.diagnose()
            ticket = await self.load_ticket()
        pending = [s for s in ticket.get("diagnostic_steps") or [] if s.get("result") == "pending"]
        if not pending:
            return CommandResult("nothing_pending", "No diagnostic step is pending, so nothing was sent. "
                                                    "Add steps with /diagnose first.")
        conversation = await self.conversation(ticket)
        context = await self.store.get(conversation["conversation_id"])
        if context.get("awaiting") == "payment_details" and payment_request_is_live(context.get("payment_request")):
            raise CommandError("The customer is still being asked for booking details (/payments) on that "
                               "conversation. Send the steps once they have replied.")
        number = ticket["ticket_number"]
        lines = "\n".join(f"{i}. {step['step']}" for i, step in enumerate(pending, start=1))
        text_ = DIAGNOSTICS_ASK.format(name=_first_name((ticket.get("customer") or {}).get("full_name")),
                                       ticket=number, steps=lines)
        await self.tools.call("messaging__send_reply", conversation_id=str(conversation["conversation_id"]), text=text_)
        count = f"{len(pending)} step{'s' if len(pending) != 1 else ''}"
        await self.step("sent", f"{count} on {conversation['channel']}")

        request = {
            "ticket_id": self.ticket_id, "ticket_number": number,
            "step_ids": [str(step["step_id"]) for step in pending],
            "steps": [step["step"] for step in pending],  # what the customer saw, for reading their answer
            "requested_by": str(self.staff.id), "requested_at": datetime.now(UTC).isoformat(),
        }
        context = {k: v for k, v in context.items()
                   if k not in ("pending", "serial_misses", "last_serial_attempt", "details_misses", "feedback_misses")}
        context.update({"awaiting": "diagnostic_feedback", "diagnostic_request": request})
        await self.store.set(conversation["conversation_id"], context)
        await self.tools.call("tickets__update_status", ticket_id=self.ticket_id, status="awaiting_customer",
                              note=f"/diagnose-send: asked the customer to try {count}")
        await self.data.diagnostics_sent(self.ticket_id, str(self.staff.id), {
            "step_ids": request["step_ids"], "steps": request["steps"], "channel": conversation["channel"],
            "conversation_id": str(conversation["conversation_id"]),
            "note": f"Sent {count} to the customer on {conversation['channel']}"})
        await self.step("status", "awaiting_customer")
        return CommandResult(
            "diagnostics_sent",
            f"Sent {count} to the customer on {conversation['channel']}. Their reply ticks the checklist.",
            {"reply": text_, "channel": conversation["channel"], "step_ids": request["step_ids"]},
        )

    # ---------- /summary ----------

    async def summary(self) -> CommandResult:
        ticket = await self.load_ticket()
        messages = [{"role": "system", "content": load_prompt("ticket_summary")},
                    {"role": "user", "content": conversation_digest(ticket)}]
        try:
            result = await (self.llm or default_llm()).complete(messages, tier="fast", max_tokens=SUMMARY_MAX_TOKENS)
        except LLMUnavailable as e:
            raise CommandError("No model is reachable right now, so the summary is unchanged. "
                               "Try again in a minute.") from e
        self.used(result)
        summary = " ".join(result.text.split())[:MAX_SUMMARY_CHARS]
        if not summary:
            raise CommandError("The model returned an empty summary, so nothing was changed.")
        updated = await self.tools.call("tickets__update_summary", ticket_id=self.ticket_id, summary=summary)
        if not isinstance(updated, dict) or not updated.get("ok"):
            raise CommandError("Couldn't save the summary.")
        await self.step("summary", summary)
        return CommandResult("summary_updated", "Summary refreshed.", {"summary": summary})

    # ---------- /ask <what> ----------

    async def ask(self) -> CommandResult:
        what = " ".join(self.args.split())[:300]
        ticket = await self.load_ticket(refuse_closed="asking the customer anything")
        conversation = await self.conversation(ticket)
        question, how = await self.phrase_question(what, ticket)
        await self.step("question", f"{how}: {question}")
        await self.tools.call("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                              text=question)
        await self.step("ask", f"sent on {conversation['channel']}")
        await self.tools.call("tickets__update_status", ticket_id=self.ticket_id, status="awaiting_customer",
                              note=f"/ask: {what}")
        await self.step("status", "awaiting_customer")
        return CommandResult("asked", f"Asked the customer on {conversation['channel']}.",
                             {"reply": question, "channel": conversation["channel"], "phrased_by": how})

    async def phrase_question(self, what: str, ticket: dict[str, Any]) -> tuple[str, str]:
        """The question for the customer, and who wrote it. MODEL_FAST phrases it; the fixed template
        is used when no model answers, or when its wording mentions a number the agent never wrote."""
        name = _first_name((ticket.get("customer") or {}).get("full_name"))
        device = (ticket.get("product") or {}).get("model_name") or "device"
        fallback = ASK_TEMPLATE.format(name=name, device=device, ticket=ticket["ticket_number"], what=what)
        messages = [
            {"role": "system", "content": load_prompt("ask_customer")},
            {"role": "system", "content": f"Customer's first name: {name}. Ticket {ticket['ticket_number']}: "
                                          f"{(ticket.get('title') or '')[:200]}. Device: {device}."},
            {"role": "user", "content": f"Ask the customer for: {what}"},
        ]
        try:
            result = await (self.llm or default_llm()).complete(messages, tier="fast", max_tokens=ASK_MAX_TOKENS)
        except LLMUnavailable as e:
            log.warning("/ask: no model, using the template: %s", e)
            return fallback, "fixed template (no model reachable)"
        self.used(result)
        candidate = result.text.strip().strip('"').strip()
        if not candidate or len(candidate) > 700:
            return fallback, "fixed template"
        reference = f"{what} {ticket['ticket_number']} {ticket.get('title') or ''} {device}"
        if invented_numbers(reference, candidate):
            return fallback, "fixed template (the model's wording added a number)"
        return candidate, "phrased by the model"

    # ---------- /escalate <reason> ----------

    async def escalate(self) -> CommandResult:
        reason = " ".join(self.args.split())[:300]
        ticket = await self.load_ticket(refuse_closed="escalating it")
        current = ticket.get("priority") if ticket.get("priority") in PRIORITIES else "medium"
        raised = PRIORITIES[min(PRIORITIES.index(current) + 1, len(PRIORITIES) - 1)]
        await self.data.raise_priority(self.ticket_id, raised, current, reason, str(self.staff.id))
        await self.events.publish("ticket.updated", {
            "ticket_id": self.ticket_id, "ticket_number": ticket["ticket_number"], "priority": raised,
            "by": str(self.staff.id)})
        changed = raised != current
        await self.step("priority", f"{current.capitalize()} → {raised.capitalize()}" if changed
                        else f"already {raised.capitalize()}")
        notified = await self.tools.call(
            "messaging__notify_staff", role="admin", type="escalation",
            title=f"{ticket['ticket_number']} escalated to {raised.capitalize()}",
            body=f"{getattr(self.staff, 'name', 'An agent')}: {reason}"[:300], link=f"/tickets/{self.ticket_id}")
        count = int(notified.get("recipients") or 0) if isinstance(notified, dict) and notified.get("ok") else 0
        await self.step("notify", f"{count} admin{'s' if count != 1 else ''} notified" if count
                        else "no admin to notify", status="ok" if count else "warning")
        return CommandResult(
            "escalated",
            (f"Priority raised to {raised.capitalize()}" if changed else f"Priority is already {raised.capitalize()}")
            + f"; {count} admin{'s' if count != 1 else ''} notified.",
            {"priority": raised, "previous": current, "notified": count},
        )

    # ---------- /close <note> ----------

    async def close(self) -> CommandResult:
        note = " ".join(self.args.split())[:500]
        ticket = await self.load_ticket()
        number = ticket["ticket_number"]
        if ticket.get("status") in ("resolved", "closed"):
            raise CommandError(f"{number} is already {ticket['status']}.")
        job = await self.data.open_job(self.ticket_id)
        if job is not None:
            raise CommandError(f"{number} has an open technician job ({job['status']}, {job['scheduled_date']}). "
                               "Complete or cancel it first.")
        payment = await self.data.open_payment(self.ticket_id)
        if payment is not None:
            raise CommandError(f"{number} has an open payment {payment['invoice_number']} ({payment['status']}). "
                               "Wait for it, or cancel it, first.")
        conversation = await self.data.customer_conversation(self.ticket_id, ticket["customer_id"])
        if conversation is not None:
            reply = CLOSE_REPLY.format(name=_first_name((ticket.get("customer") or {}).get("full_name")),
                                       ticket=number, title=ticket.get("title") or "your request")
            await self.tools.call("messaging__send_reply", conversation_id=str(conversation["conversation_id"]),
                                  text=reply)
            await self.step("reply", f"closing message sent on {conversation['channel']}")
        else:
            await self.step("reply", "no customer conversation to message", status="skipped")
        await self.tools.call("tickets__update_status", ticket_id=self.ticket_id, status="resolved",
                              note=f"/close by {getattr(self.staff, 'name', 'staff')}: {note}",
                              customer_told=conversation is not None)
        await self.step("status", "resolved")
        where = f" and told the customer on {conversation['channel']}" if conversation else ""
        return CommandResult("closed", f"Resolved {number}{where}.",
                             {"channel": conversation["channel"] if conversation else None})

    # ---------- which service ----------

    async def resolve_service(self, ticket: dict[str, Any], unit: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The catalog service to bill, and why it was picked (shown to the agent and on the timeline).

        With words, they name it (§7.6). With none, the ticket does: a keyword match in code over the
        services that fit this device, then at most one decide choice among them. A model only ever
        picks a catalog code; price and warranty stay in code.
        """
        services = await self.data.services()
        if self.args.strip():
            return await self._service_from_words(services)
        return await self._service_from_ticket(services, ticket, unit)

    async def _service_from_words(self, services: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        best, candidates = match_service(self.args, services)
        if best is not None:
            return best, f"the agent asked for \"{self.args.strip()[:60]}\""
        options = candidates or services
        chosen = await self._decide(self.args, options, "Which service from the catalog does the agent want to bill?")
        if chosen is None:
            raise CommandError("Couldn't tell which service that is. Options: " + ", ".join(s["code"] for s in options))
        return chosen, f"\"{self.args.strip()[:60]}\" read by the decision model"

    async def _service_from_ticket(self, services: list[dict[str, Any]], ticket: dict[str, Any],
                                   unit: dict[str, Any]) -> tuple[dict[str, Any], str]:
        category = unit.get("category") or "device"
        model = await self.tools.call("catalog__lookup_model", model_number=unit.get("model_number") or "")
        part_types = {p.get("part_type") for p in (model.get("compatible_parts") or [])} \
            if isinstance(model, dict) and model.get("found") else set()
        fitting = services_that_fit(services, category, part_types)
        add = ADD_THE_SERVICE.format(command=self.name)
        if not fitting:
            raise CommandError(f"No service in the catalog fits a {category}. {add}")
        best, tied, reasons = pick_from_ticket(ticket, fitting)
        if best is not None:
            return best, "from the ticket: " + "; ".join(reasons)
        options = tied or fitting
        chosen = await self._decide(ticket_state(ticket, unit), options,
                                    f"Which repair does this {category}'s ticket need?")
        if chosen is None:
            raise CommandError(f"Couldn't tell which service this ticket needs. {add}")
        return chosen, (f"from the ticket, chosen by the decision model among {len(options)} services "
                        f"that fit a {category}")

    async def _decide(self, state: str, options: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
        """One decide choice limited to `options`; the answer must be one of their codes. None when unsure."""
        try:
            answers = await self.decider.decide(state, {"service": Question.choice(
                question, {s["code"]: s["name"] for s in options})})
        except LLMUnavailable as e:
            log.warning("/%s: no decision model to pick the service: %s", self.name, e)
            return None
        self.model = f"decide:{self.settings.decision_provider}"
        answer = answers.get("service")
        chosen = answer.selected if answer and answer.is_confident(self.settings.decision_min_confidence) else None
        return next((s for s in options if s["code"] == chosen), None)


def _first_name(full_name: Any) -> str:
    return (str(full_name or "").split(" ")[0]) or "there"


# What /summary's model reads: the ticket and its cross-channel conversation, oldest first, capped.
MAX_DIGEST_CHARS = 6000


def conversation_digest(ticket: dict[str, Any]) -> str:
    product = ticket.get("product") or {}
    lines = [
        f"Ticket {ticket.get('ticket_number')}: {ticket.get('title') or ''}",
        f"Status {ticket.get('status')}, priority {ticket.get('priority')}, issue type {ticket.get('issue_type')}.",
        f"Device: {product.get('model_name') or 'not verified'}"
        + (f" ({product.get('serial_number')})" if product.get("serial_number") else ""),
        f"Customer's first message: {(ticket.get('description') or '')[:600]}",
        "Conversation, oldest first:",
    ]
    for m in ticket.get("messages") or []:
        lines.append(f"[{m.get('sender_type')} via {m.get('channel')}] {' '.join(str(m.get('body') or '').split())[:400]}")
    tried = [f"{s.get('step')} ({s.get('result')})" for s in ticket.get("diagnostic_steps") or []
             if s.get("result") != "pending"]
    if tried:
        lines.append("Diagnostics tried: " + "; ".join(tried))
    notes = [f"{e.get('type')}: {(e.get('payload') or {}).get('note') or (e.get('payload') or {}).get('status') or ''}"
             for e in (ticket.get("events") or [])[-8:] if e.get("type") in ("status_changed", "priority_raised")]
    if notes:
        lines.append("Recent changes: " + "; ".join(n[:160] for n in notes))
    return "\n".join(lines)[-MAX_DIGEST_CHARS:]


# ---------- custom commands (§7.5) ----------

# The only variables a custom command's template may use, with what each holds. Rendering is a
# plain substitution of these values: never eval, never a format string.
TEMPLATE_VARIABLES: dict[str, str] = {
    "ticket.number": "the ticket number, e.g. SR-2026-00042",
    "ticket.title": "the ticket's title",
    "ticket.status": "its status, e.g. awaiting_customer",
    "ticket.priority": "its priority",
    "ticket.issue_type": "the issue type, e.g. battery",
    "ticket.category": "hardware, software or unknown",
    "ticket.summary": "the AI summary",
    "ticket.description": "the customer's first message",
    "customer.name": "the customer's full name",
    "customer.email": "the customer's email",
    "customer.phone": "the customer's phone",
    "product.serial": "the device's serial number",
    "product.model": "the model name, e.g. Aurora 14",
    "product.model_number": "the model number, e.g. AX14",
    "product.color": "the device's color",
    "product.warranty_until": "the warranty end date",
    "agent.name": "your name",
    "args": "the words typed after the command",
}
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_]+(?:\.[A-Za-z_]+)?)\s*\}\}")
MAX_CUSTOM_TOOLS = 12


def template_variables(template: str) -> list[str]:
    return list(dict.fromkeys(_VARIABLE.findall(template)))


def unknown_variables(template: str) -> list[str]:
    return [v for v in template_variables(template) if v not in TEMPLATE_VARIABLES]


def render_template(template: str, values: dict[str, str]) -> str:
    """Replace each {{variable}} with its value; an unknown one becomes empty (saving refuses them)."""
    return _VARIABLE.sub(lambda m: values.get(m.group(1), ""), template)


def template_values(ticket: dict[str, Any], staff: Any, args: str) -> dict[str, str]:
    customer = ticket.get("customer") or {}
    product = ticket.get("product") or {}
    raw = {
        "ticket.number": ticket.get("ticket_number"), "ticket.title": ticket.get("title"),
        "ticket.status": ticket.get("status"), "ticket.priority": ticket.get("priority"),
        "ticket.issue_type": ticket.get("issue_type"), "ticket.category": ticket.get("category"),
        "ticket.summary": ticket.get("ai_summary"), "ticket.description": ticket.get("description"),
        "customer.name": customer.get("full_name"), "customer.email": customer.get("email"),
        "customer.phone": customer.get("phone"),
        "product.serial": product.get("serial_number"), "product.model": product.get("model_name"),
        "product.model_number": product.get("model_number"), "product.color": product.get("color"),
        "product.warranty_until": product.get("warranty_until"),
        "agent.name": getattr(staff, "name", None), "args": args.strip(),
    }
    return {key: "" if value is None else str(value) for key, value in raw.items()}


def check_allowed_tools(names: list[str], known: set[str]) -> list[str]:
    """What is wrong with a custom command's allowed_tools, one message per name; empty when all is fine.

    `known` is every tool the hub has registered. A name must be a copilot tool the MCP servers
    really offer, and never one of MODEL_FORBIDDEN_TOOLS (§4.6, §5.5).
    """
    problems = []
    for name in names:
        try:
            server, _ = split_name(name)
        except McpToolError:
            problems.append(f"{name[:80]!r} isn't a <server>__<tool> name")
            continue
        if name in MODEL_FORBIDDEN_TOOLS:
            problems.append(f"{name} is never offered to a model: only staff actions and the automations use it")
        elif server not in ROLE_SERVERS[ROLE]:
            problems.append(f"{name}: there is no {server} server")
        elif name not in known:
            if not any(k.startswith(f"{server}__") for k in known):
                problems.append(f"{name} can't be checked: the {server} MCP server isn't reachable")
            else:
                problems.append(f"{name} isn't a tool the {server} server offers")
    if len(names) > MAX_CUSTOM_TOOLS:
        problems.append(f"at most {MAX_CUSTOM_TOOLS} tools")
    return problems


class _ProgressEvents:
    """The event bus, plus each of the tool loop's calls as a step of the command's own stream."""

    def __init__(self, events: EventBus, progress: Progress) -> None:
        self._events = events
        self._progress = progress

    async def publish(self, type: Any, data: dict[str, Any]) -> Event:
        event = await self._events.publish(type, data)
        if type == "agent.tool_called":
            await self._progress({"type": "step", "step": "tool", "status": "ok" if data.get("ok") else "failed",
                                  "detail": f"{data.get('tool')} · {data.get('ms')} ms"})
        return event


async def run_custom_command(
    ticket_id: uuid.UUID,
    command: dict[str, Any],
    args: str,
    staff: Any,
    *,
    progress: Progress | None = None,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    data: CommandData | None = None,
    llm: LLM | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
) -> CommandResult:
    """A custom command (§7.5): its template, rendered for this ticket, through the copilot tool loop
    with only its allowed_tools. runtime.run_tool_loop writes the ai_runs row."""
    name = command["name"]
    trigger = f"/{name}"
    progress = progress or _no_progress
    events = events or bus
    data = data or CommandData()
    call = call_tool or hub.call_tool
    # The template's values are read by code, not by the model: tickets__get_ticket is offered to
    # the model only if the command lists it.
    reader = CommandTools(frozenset({"tickets__get_ticket"}), call, events, trigger=trigger, ticket_id=ticket_id)
    ticket = await reader.call("tickets__get_ticket", ticket_id=str(ticket_id))
    if not isinstance(ticket, dict) or not ticket.get("found"):
        raise CommandError("Ticket not found.")
    await progress({"type": "step", "step": "ticket", "status": "ok",
                    "detail": f"{ticket['ticket_number']} · {(ticket.get('customer') or {}).get('full_name') or 'customer'}"})
    conversation = await data.customer_conversation(str(ticket_id), ticket["customer_id"])
    prompt = render_template(command["prompt_template"], template_values(ticket, staff, args))

    gate = CommandTools(frozenset(command.get("allowed_tools") or []), call, events, trigger=trigger,
                        ticket_id=ticket_id, model_driven=True)
    specs = tool_specs if tool_specs is not None else hub.tools(ROLE_SERVERS[ROLE])
    wanted = [t for t in command.get("allowed_tools") or [] if gate.allows(t)]
    tools = compact_tools(filter_tools(specs, ROLE_SERVERS[ROLE], allowed_tools=wanted))
    offered = sorted(t["function"]["name"] for t in tools)
    await progress({"type": "step", "step": "tools", "status": "ok", "detail": ", ".join(offered) or "none"})
    missing = sorted(set(command.get("allowed_tools") or []) - set(offered))
    if missing:
        await progress({"type": "step", "step": "tools", "status": "warning",
                        "detail": "not available, so not offered: " + ", ".join(missing)})

    async def gated(tool: str, tool_args: dict[str, Any]) -> Any:
        if not gate.allows(tool):  # run_tool_loop already refuses a tool it didn't offer; this is the second lock
            raise CommandToolNotAllowed(f"{trigger} may not call {tool[:100]!r}")
        return await call(tool, tool_args)

    where = (f"The customer's conversation id is {conversation['conversation_id']} ({conversation['channel']}); "
             "messaging__send_reply with it reaches them on their own channel."
             if conversation else "The customer has no conversation to reply on.")
    messages = [
        {"role": "system", "content": load_prompt("custom_command")},
        {"role": "system", "content": f"Ticket id {ticket_id} ({ticket['ticket_number']}). "
                                      f"Customer id {ticket['customer_id']}. {where}"},
        {"role": "user", "content": prompt},
    ]
    try:
        result = await run_tool_loop(ROLE, trigger, messages, tools, gated, ticket_id=ticket_id, llm=llm,
                                     log_run=log_run, events=_ProgressEvents(events, progress))  # type: ignore[arg-type]
    except LLMUnavailable as e:
        raise CommandError(f"No model is reachable right now, so /{name} stopped. Try again in a minute.") from e
    answer = result.text.strip() or ("Stopped at the tool-call limit before answering." if result.hit_limit
                                     else "Done.")
    return CommandResult("answered", answer, {"tool_calls": result.tool_calls, "hit_limit": result.hit_limit,
                                              "model": result.model})


# ---------- which service: the agent's words, or the ticket (§7.6) ----------

ADD_THE_SERVICE = "Add the service, e.g. /{command} display replacement"

# What staff and customers call each service, as word-start regexes matched case-insensitively.
# One point per pattern found. "replace" and "upgrade" also score for every *_REPLACE / *_UPGRADE
# service (ACTIONS), so "ssd upgrade" and "ssd replacement" land on different services.
SERVICE_PATTERNS: dict[str, tuple[str, ...]] = {
    "BATTERY_REPLACE": (r"(?<!cmos )(?<!bios )batter(?:y|ies)", r"drain", r"won'?t (?:hold (?:a )?)?charge",
                        r"not (?:holding (?:a )?)?charg(?:e|ing)\b", r"0 ?%", r"swollen", r"bulg"),
    "CMOS_REPLACE": (r"cmos", r"bios batter", r"rtc\b", r"(?:clock|date|time)s? (?:keeps? )?reset"),
    "DISPLAY_REPLACE": (r"screen", r"display", r"flicker", r"panel", r"lcd", r"oled", r"pixel", r"backlight"),
    "KEYBOARD_REPLACE": (r"keyboard", r"keys?\b", r"keycap", r"typing"),
    "FAN_REPLACE": (r"fans?\b", r"overheat", r"too hot", r"heats? up", r"thermal", r"temperature"),
    "SSD_REPLACE": (r"ssd", r"(?:hard )?drive\b", r"disk", r"(?<!os )(?<!windows )corrupt", r"bad sector",
                    r"no bootable"),
    "SSD_UPGRADE": (r"ssd", r"storage", r"(?:more|bigger|larger) (?:disk|space)"),
    "RAM_UPGRADE": (r"ram\b", r"memory"),
    "CHARGER_REPLACE": (r"charger", r"adapt[eo]r", r"power brick"),
    "OS_REINSTALL": (r"os\b", r"windows", r"re-?install", r"operating system", r"won'?t boot", r"boot ?loop",
                     r"blue screen", r"bsod", r"(?:os|windows) (?:is |got |was )?corrupt"),
    "EAR_CUSHION_REPLACE": (r"ear ?cushion", r"ear ?pads?", r"cushion", r"ear ?tips?", r"padding", r"foam"),
    "UPI_TEST": (r"upi\b", r"test\b"),
}
ACTIONS: dict[str, str] = {"_REPLACE": r"replac", "_UPGRADE": r"upgrad"}

# §4.4 issue types that point straight at one repair. ("charging" could be the battery or the
# charger, and "boot" the drive or the OS, so they only count through the words.)
ISSUE_TYPE_SERVICES = {
    "battery": "BATTERY_REPLACE", "display": "DISPLAY_REPLACE", "keyboard": "KEYBOARD_REPLACE",
    "overheating": "FAN_REPLACE", "os": "OS_REINSTALL",
}
# Weights when the ticket picks the service: its title says the most, the customer's own words least.
TICKET_TEXT_WEIGHTS = (("title", 3), ("ai_summary", 2), ("description", 1))
ISSUE_TYPE_WEIGHT = 3
# Services with no part are repairs only when they need a visit to a computer (OS_REINSTALL);
# one with no part and no visit (UPI_TEST) is never picked from a ticket, only by name.
COMPUTER_CATEGORIES = frozenset({"laptop", "desktop"})


def _compile(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(r"\b" + p, re.IGNORECASE) for p in patterns]


_SERVICE_REGEX = {code: _compile(patterns) for code, patterns in SERVICE_PATTERNS.items()}
_ACTION_REGEX = {suffix: re.compile(r"\b" + p, re.IGNORECASE) for suffix, p in ACTIONS.items()}


def _service_regexes(service: dict[str, Any]) -> list[re.Pattern[str]]:
    """The service's patterns; a service added to the catalog later falls back to its name's words."""
    if service["code"] in _SERVICE_REGEX:
        return _SERVICE_REGEX[service["code"]]
    words = [w for w in re.findall(r"[a-z]{3,}", service["name"].lower()) if w not in ("the", "and")]
    return [re.compile(r"\b" + re.escape(w), re.IGNORECASE) for w in words]


def _hits(text_: str, service: dict[str, Any], *, actions: bool) -> list[str]:
    found = [m.group(0).lower() for regex in _service_regexes(service) if (m := regex.search(text_))]
    if actions:
        for suffix, regex in _ACTION_REGEX.items():
            if service["code"].endswith(suffix) and (m := regex.search(text_)):
                found.append(m.group(0).lower())
    return found


def match_service(args: str, services: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The catalog service the agent's words name, decided in code where the words are clear.

    Returns (service, []) for a clear match, (None, tied candidates) when several fit equally, and
    (None, []) when nothing fits. A service code typed as is ("BATTERY_REPLACE") always matches.
    """
    code = re.sub(r"[\s-]+", "_", args.strip()).upper()
    for service in services:
        if service["code"] == code:
            return service, []
    words = " ".join(re.sub(r"[_-]+", " ", args).split())
    if not words:
        return None, []
    scored = [(len(hits), service) for service in services if (hits := _hits(words, service, actions=True))]
    if not scored:
        return None, []
    top = max(score for score, _ in scored)
    tied = sorted((service for score, service in scored if score == top), key=lambda s: s["code"])
    return (tied[0], []) if len(tied) == 1 else (None, tied)


def services_that_fit(services: list[dict[str, Any]], category: str, part_types: set[str]) -> list[dict[str, Any]]:
    """The services that can apply to this device: a part compatible with its model, or (no part) a
    visit to a computer. Worked out in code from the catalog, so a model can't widen the choice."""
    def fits(service: dict[str, Any]) -> bool:
        if service.get("part_type"):
            return service["part_type"] in part_types
        return bool(service.get("requires_visit")) and category in COMPUTER_CATEGORIES

    return [s for s in services if fits(s)]


def pick_from_ticket(ticket: dict[str, Any], services: list[dict[str, Any]]
                     ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """The service the ticket's own words point at, in code: (service, [], reasons) for a clear
    winner, (None, tied, []) for a tie, (None, [], []) when nothing matches."""
    scores: list[tuple[int, dict[str, Any], list[str]]] = []
    for service in services:
        score, words, places, reasons = 0, {}, [], []
        for field_name, weight in TICKET_TEXT_WEIGHTS:
            hits = _hits(str(ticket.get(field_name) or ""), service, actions=False)
            if hits:
                score += weight * len(hits)
                words.update(dict.fromkeys(hits))
                places.append(_FIELD_NAMES[field_name])
        if words:
            reasons.append(", ".join(f'"{w}"' for w in words) + " in the " + _and(places))
        if ISSUE_TYPE_SERVICES.get(str(ticket.get("issue_type") or "")) == service["code"]:
            score += ISSUE_TYPE_WEIGHT
            reasons.append(f"issue type {ticket['issue_type']}")
        if score:
            scores.append((score, service, reasons))
    if not scores:
        return None, [], []
    scores.sort(key=lambda entry: (-entry[0], entry[1]["code"]))
    top = scores[0][0]
    tied = [service for score, service, _ in scores if score == top]
    if len(tied) > 1:
        return None, tied, []
    return scores[0][1], [], scores[0][2]


_FIELD_NAMES = {"title": "title", "ai_summary": "summary", "description": "customer's message"}


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def ticket_state(ticket: dict[str, Any], unit: dict[str, Any]) -> str:
    """What the decision model reads to pick a service: the ticket, the device, what was tried."""
    tried = "; ".join(f"{s.get('step')} ({s.get('result')})" for s in (ticket.get("diagnostic_steps") or [])
                      if s.get("result") in ("worked", "failed", "skipped"))
    lines = [
        f"Device: {unit.get('model_name')} ({unit.get('category')})",
        f"Ticket title: {ticket.get('title') or ''}",
        f"Issue type: {ticket.get('issue_type') or 'other'}",
        f"Summary: {(ticket.get('ai_summary') or '')[:400]}",
        f"Customer wrote: {(ticket.get('description') or '')[:600]}",
    ]
    if tried:
        lines.append(f"Already tried: {tried[:400]}")
    return "\n".join(lines)


def warranty_text(unit: dict[str, Any], service_code: str) -> str:
    """The warranty result in one line, for the agent's steps and the ticket timeline."""
    until = unit.get("warranty_until")
    if not unit.get("in_warranty"):
        return f"Out of warranty (ended {until})" if until else "No warranty on record"
    if free_under_warranty(service_code, True):
        return f"In warranty until {until}"
    return f"In warranty until {until}, but {service_code} is paid (upgrades and UPI_TEST are never free)"


def _short_label(label: str) -> str:
    """"Labour — Battery replacement" -> "labour"; a part keeps its name."""
    return "labour" if label.lower().startswith("labour") else label


# ---------- the handoff from intake's payment_details slot (§4.1, §7.6) ----------


def payment_request_is_live(request: Any, *, now: datetime | None = None) -> bool:
    """Whether a conversation's payment_request was set by /payments and is still fresh."""
    if not isinstance(request, dict):
        return False
    try:
        uuid.UUID(str(request.get("ticket_id")))
        uuid.UUID(str(request.get("customer_id")))
        requested = datetime.fromisoformat(str(request.get("requested_at")))
    except (TypeError, ValueError):
        return False
    if not request.get("service_code") or requested.tzinfo is None:
        return False
    return (now or datetime.now(UTC)) - requested <= DETAILS_TTL


async def finish_payment_details(
    identity: Identity,
    collected: dict[str, Any],
    request: dict[str, Any],
    *,
    call_tool=None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    settings: Settings | None = None,
    store: ContextStore | None = None,
    data: CommandData | None = None,
    start_booking: Callable[..., Any] | None = None,
) -> HandoffResult:
    """Act on the customer's completed details (§7.6): send the payment link, or book a free repair.

    The ticket, customer and service come from the agent's /payments request in the conversation's
    context; the customer's text supplied only the contact details. The amount is computed by the
    payments server; whether it is free was decided by /payments, in code, from the warranty. On any
    failure the customer is told an agent will follow up, the agent who ran /payments is notified,
    and the slot is closed so the customer isn't stuck in it.
    """
    settings = settings or get_settings()
    store = store or ContextStore()
    data = data or CommandData()
    ticket_id = str(request.get("ticket_id"))
    tools = CommandTools(PAYMENTS_TOOLS, call_tool, events, trigger=PAYMENTS_TRIGGER, ticket_id=ticket_id)
    conversation_id = str(identity.conversation_id)
    free = request.get("free") is True
    delayed = FREE_DELAYED_REPLY if free else LINK_DELAYED_REPLY
    start = time.monotonic()
    error: str | None = None
    try:
        if free:
            result = await _finish_free(identity, collected, request, tools=tools, data=data,
                                        start_booking=start_booking)
        else:
            result = await _finish(identity, collected, request, tools=tools, settings=settings, data=data)
    except Exception as e:  # tool or database failure: the customer still gets an answer (§15)
        log.exception("/payments handoff failed for ticket %s", ticket_id)
        result = HandoffResult(ok=False, reply=delayed, error=f"{type(e).__name__}: {e}"[:300])
    if not result.ok:
        error = result.error
        await _tell_the_customer_and_the_agent(tools, conversation_id, request, result.error or "unknown error",
                                               reply=delayed)
    context = {k: v for k, v in identity.context.items()
               if k not in ("awaiting", "collected", "payment_request", "details_misses")}
    if result.payment:
        context["last_payment"] = {"payment_id": result.payment["payment_id"],
                                   "invoice_number": result.payment["invoice_number"]}
    await store.set(identity.conversation_id, context)
    log_run_in_background(log_run or log_ai_run, AiRunRecord(
        role=ROLE, trigger=PAYMENTS_TRIGGER, ticket_id=ticket_id, model=None, input_tokens=None,
        output_tokens=None, tool_calls=tools.records, latency_ms=round((time.monotonic() - start) * 1000),
        error=error,
    ))
    return HandoffResult(ok=result.ok, reply=result.reply, payment=result.payment, error=result.error,
                         tool_calls=tools.records, outcome=result.outcome)


async def _finish_free(identity: Identity, collected: dict[str, Any], request: dict[str, Any], *,
                       tools: CommandTools, data: CommandData,
                       start_booking: Callable[..., Any] | None) -> HandoffResult:
    """A free warranty repair: no invoice. Save the details and start the booking (§7.7)."""
    customer_id = str(request["customer_id"])
    if customer_id != str(identity.customer_id):
        return HandoffResult(ok=False, reply=FREE_DELAYED_REPLY,
                             error="the details came from a different customer than the ticket's")
    saved = await data.save_details(customer_id, collected)
    name = (collected.get("full_name") or "").split(" ")[0] or "there"
    reply = FREE_BOOKING_REPLY.format(name=name, service=str(request.get("service_name") or "repair").lower())
    await tools.call("messaging__send_reply", conversation_id=str(identity.conversation_id), text=reply)
    if start_booking is None:
        from app.brain import workflows  # workflows imports this module

        start_booking = workflows.start_warranty_booking
    # /schedule's request names its command, so the booking says who authorized it and how.
    extra = {"command": str(request["command"])} if request.get("command") else {}
    start_booking(ticket_id=str(request["ticket_id"]), service_code=str(request["service_code"]),
                  address_id=saved["address_id"], request_key=str(request["requested_at"]),
                  staff_id=str(request["requested_by"]), staff_name=request.get("requested_by_name"), **extra)
    return HandoffResult(ok=True, reply=reply, outcome="booking_started")


async def _finish(identity: Identity, collected: dict[str, Any], request: dict[str, Any], *,
                  tools: CommandTools, settings: Settings, data: CommandData) -> HandoffResult:
    customer_id = str(request["customer_id"])
    if customer_id != str(identity.customer_id):
        return HandoffResult(ok=False, reply=LINK_DELAYED_REPLY,
                             error="the details came from a different customer than the ticket's")
    if not settings.upi_configured:
        return HandoffResult(ok=False, reply=LINK_DELAYED_REPLY, error="UPI_ID and UPI_PAYEE_NAME are not set")

    saved = await data.save_details(customer_id, collected)
    created = await tools.call(
        "payments__create_payment_request", ticket_id=str(request["ticket_id"]), customer_id=customer_id,
        service_code=str(request["service_code"]), address_id=saved["address_id"])
    if not isinstance(created, dict) or not created.get("ok"):
        reason = (f"{created.get('error')}: {created.get('message')}" if isinstance(created, dict)
                  else str(created)[:200])
        return HandoffResult(ok=False, reply=LINK_DELAYED_REPLY, error=f"create_payment_request refused: {reason}")

    invoice = await data.invoice(created["payment_id"])
    if invoice is None:
        return HandoffResult(ok=False, reply=LINK_DELAYED_REPLY, error="the new payment could not be read back")
    reply = await send_payment_link(tools, str(identity.conversation_id), invoice, given_email=collected.get("email"))
    payment = {key: invoice[key] for key in ("payment_id", "invoice_number", "total", "pay_url", "expires_at")}
    return HandoffResult(ok=True, reply=reply, payment=payment)


async def send_payment_link(tools: CommandTools, conversation_id: str | None, invoice: dict[str, Any], *,
                            given_email: str | None = None) -> str | None:
    """The link on the customer's channel and the invoice by email (§7.6 step 3): /payments' handoff and
    the payments page's "New payment request" send the same words. Returns the reply, or None when there
    was no conversation to send it on."""
    email = invoice["customer"]["email"]
    if email and given_email and email.lower() == given_email.lower():
        email_note = f"I've also emailed the invoice to {email}."
    elif email:
        email_note = (f"I've also emailed the invoice to the address on your account, {mask_email(email)}. "
                      "If that isn't right, reply here and an agent will update it.")
    else:
        email_note = ""
    first_name = (invoice["customer"]["full_name"] or "").split(" ")[0] or "there"
    reply = LINK_REPLY.format(name=first_name, invoice=invoice["invoice_number"], total=invoice["total_display"],
                              url=invoice["pay_url"], expires=invoice["expires_display"], email_note=email_note).strip()
    if conversation_id:
        await tools.call("messaging__send_reply", conversation_id=conversation_id, text=reply)
    if email:
        await tools.call("messaging__send_email", to=email, template="payment_link", data=invoice,
                         ticket_id=invoice["ticket_id"],
                         subject=f"Invoice {invoice['invoice_number']} for ticket {invoice['ticket_number']}")
    return reply if conversation_id else None


async def _tell_the_customer_and_the_agent(tools: CommandTools, conversation_id: str, request: dict[str, Any],
                                           error: str, *, reply: str = LINK_DELAYED_REPLY) -> None:
    """The failure path: the customer is never left in silence (§15), and the agent knows why."""
    what = "start the booking" if request.get("free") is True else "send the payment link"
    for name, args in (
        ("messaging__send_reply", {"conversation_id": conversation_id, "text": reply}),
        ("messaging__notify_staff", {
            "user_id": request.get("requested_by"), "type": "payment_link_failed",
            "title": f"Couldn't {what} for {request.get('ticket_number') or 'a ticket'}",
            "body": error[:300], "link": f"/tickets/{request.get('ticket_id')}"}),
    ):
        try:
            await tools.call(name, **args)
        except Exception:
            log.exception("/payments handoff: %s failed while reporting a failure", name)


def mask_email(email: str) -> str:
    """"r***@example.com": enough for the customer to recognise it, not enough to read it off a screen."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"