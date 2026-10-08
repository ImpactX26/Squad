"""Customer intake (ARCHITECTURE.md §4.1, §4.4, §7.1).

Customer text is untrusted, so intake is a fixed pipeline over a small tool set. INTAKE_TOOLS is
checked before the hub is called: payments, dispatch and inventory are out of reach in code.

Intake v1 (Block 1) is §7.1 without duplicates and without slots other than the serial:

    identity.resolve (a redelivered message stops here)
    → context.awaiting == serial_number? → the serial slot
    → extract_serial (regex) + one complete_json on MODEL_FAST (§4.4)
    → a new issue with no serial: ask for it, context.awaiting = serial_number; stop
    → catalog.lookup_serial: unknown → ask again once; after 2 misses a ticket flagged unverified_product
    → tickets.create_ticket → knowledge.get_playbook → MODEL_FAST writes the summary and picks the plan
    → messaging.send_reply on the conversation's own channel

The replies are fixed templates filled in code, so the ticket number, the device and its serial
are always right and a model can promise nothing. No model answering (LLMUnavailable) is never
silence: a serial is still enough to open the ticket, and the reply is the §15 fallback.

Not built yet: duplicate detection and follow-ups onto an open ticket (§7.2, Block 2; a follow-up
gets an acknowledgement only), decide.py (Block 2), the payment_details and diagnostic_feedback
slots (§7.6, §7.1), linking a customer across channels by serial (§6.3, Block 2), ai_runs rows.
"""

import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.brain.llm import LLM, LLMUnavailable, get_llm
from app.brain.mcp_hub import MCPHub, MCPUnavailable, ToolNotAllowed, ToolResult, get_hub
from app.brain.router import MODEL_FORBIDDEN_TOOLS, role_allows
from app.channels import identity
from app.channels.base import InboundMessage
from app.channels.inbound import FALLBACK_REPLY_NO_TICKET
from app.core.config import get_settings

log = logging.getLogger(__name__)

INTAKE_TOOLS: frozenset[str] = role_allows("intake", frozenset({
    "catalog__lookup_serial", "catalog__lookup_model", "catalog__get_customer_products",
    # A fixed pipeline step, only when lookup_serial reports no owner (§4.1, §6.3).
    "catalog__link_product_to_customer",
    "tickets__create_ticket", "tickets__find_similar_tickets", "tickets__add_followup", "tickets__add_message",
    "tickets__update_summary", "tickets__set_diagnostic_plan", "tickets__record_diagnostic_results",
    "knowledge__get_playbook", "knowledge__suggest_next_steps", "knowledge__search_kb",
    "messaging__send_reply",
}))

if INTAKE_TOOLS & MODEL_FORBIDDEN_TOOLS:  # a hard stop, not an assert: asserts vanish under python -O
    raise RuntimeError(f"intake must not reach {sorted(INTAKE_TOOLS & MODEL_FORBIDDEN_TOOLS)}")


async def call(name: str, arguments: dict[str, Any], hub: MCPHub | None = None) -> ToolResult:
    """Call a tool for intake. Anything outside INTAKE_TOOLS is refused before the hub is called."""
    if name not in INTAKE_TOOLS:
        raise ToolNotAllowed(f"intake may not call {name}")
    return await (hub or get_hub()).call_tool(name, arguments, allowed=INTAKE_TOOLS)


# ---------- the serial (§7.1) ----------

# "<model number>-<6 characters>", e.g. AX14-7F3K92; the model number is letters then digits.
SERIAL = re.compile(r"\b([A-Z]+\d+[A-Z0-9]*)-([A-Z0-9]{6})\b", re.IGNORECASE)


def extract_serial(text: str) -> str | None:
    """The first serial number in the text, upper-cased; the model number is the part before '-'."""
    match = SERIAL.search(text)
    return f"{match[1]}-{match[2]}".upper() if match else None


# ---------- the model's parts (§4.4) ----------

PROMPTS = Path(__file__).parent / "prompts"
EXTRACT_PROMPT = (PROMPTS / "intake_extract.md").read_text(encoding="utf-8").strip()
NOTES_PROMPT = (PROMPTS / "intake_notes.md").read_text(encoding="utf-8").strip()

MAX_MODEL_TEXT = 4000  # characters of customer text a model is given
MAX_TITLE = 120
MAX_SUMMARY = 600
MAX_PLAN_STEPS = 6


class ExtractedFields(BaseModel):
    email: str | None = None
    full_name: str | None = None
    phone: str | None = None
    address: str | None = None


class IntakeExtraction(BaseModel):
    """The structured record of §4.4."""

    intent: Literal["new_issue", "follow_up", "provide_info", "payment_details", "smalltalk", "other"]
    category: Literal["hardware", "software", "unknown"] = "unknown"
    issue_type: Literal["battery", "charging", "display", "keyboard", "audio", "overheating", "boot", "os", "driver",
                        "performance", "connectivity", "other"] = "other"
    summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    serial_number: str | None = None
    model_number: str | None = None
    urgency: Literal["low", "medium", "high"] = "medium"
    language: str = "en"
    extracted_fields: ExtractedFields = Field(default_factory=ExtractedFields)


class TicketNotes(BaseModel):
    """The agent's summary, and the first diagnostic steps picked from the playbook."""

    summary: str = ""
    plan: list[str] = Field(default_factory=list)


# ---------- the replies (fixed templates; §7.1, §15) ----------

ASK_SERIAL = ("{thanks}To open a ticket I need your device's serial number. Find it on the sticker under the laptop, "
              "or run `wmic bios get serialnumber` on Windows.")
SERIAL_UNKNOWN = ("I couldn't find the serial number {serial} in our records. Could you check it and send it again? "
                  "It looks like AX14-7F3K92: find it on the sticker under the laptop, or run "
                  "`wmic bios get serialnumber` on Windows.")
SERIAL_MISSING = ("I couldn't see a serial number in that. It looks like AX14-7F3K92: find it on the sticker under the "
                  "laptop, or run `wmic bios get serialnumber` on Windows.")
TICKET_OPENED = ("{thanks}Your ticket is {ticket_number}, for your {device} (serial {serial}): {title}. "
                 "An agent will look into it and reply to you here.")
TICKET_OPENED_UNVERIFIED = ("{thanks}Your ticket is {ticket_number}: {title}. I couldn't match a serial number to one "
                            "of our devices, so an agent will confirm the device with you here.")
TIPS = "\n\nWhile you wait, you could try:\n{steps}"
GREETING = ("Hi{name}! I'm the {company} support assistant. Tell me what's wrong with your device, with its serial "
            "number if you have it, and I'll open a ticket for you.")
FOLLOW_UP = "Thanks for the update. An agent working on your ticket will reply to you here."
# §15, with the number the customer can quote.
FALLBACK_REPLY_TICKET = ("We've received your message and created a ticket; an agent will follow up. "
                         "Your ticket number is {ticket_number}.")

MAX_SERIAL_MISSES = 2  # §7.1: after 2 misses the ticket is created, flagged unverified_product
SLOT_TTL = timedelta(hours=24)
MAX_TIPS = 2
PRIORITY = {"low": "low", "medium": "medium", "high": "high"}


class IntakeError(RuntimeError):
    """A step intake can't go on without was refused. channels.inbound sends the fallback reply."""


@dataclass
class Issue:
    """What the customer reported, kept in context.issue while the serial is asked for."""

    category: str  # hardware | software | unknown
    issue_type: str
    title: str
    text: str  # the customer's own words, every message about it
    urgency: str  # low | medium | high

    @classmethod
    def from_extraction(cls, extraction: IntakeExtraction, text: str) -> "Issue":
        return cls(extraction.category, extraction.issue_type, _title(extraction.summary) or _title(text), text,
                   extraction.urgency)

    @classmethod
    def from_text(cls, text: str) -> "Issue":
        """No model answered: the customer's words alone."""
        return cls("unknown", "other", _title(text) or "Customer message", text, "medium")

    @classmethod
    def load(cls, stored: dict[str, Any]) -> "Issue":
        return cls(stored.get("category") or "unknown", stored.get("issue_type") or "other",
                   stored.get("title") or "Customer message", stored.get("text") or "", stored.get("urgency") or "medium")


def _title(text: str) -> str:
    line = next((line for line in text.splitlines() if line.strip()), "")
    line = " ".join(line.split())
    return line if len(line) <= MAX_TITLE else line[:MAX_TITLE - 1].rstrip() + "…"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _expired(context: dict[str, Any]) -> bool:
    try:
        asked = datetime.fromisoformat(context["asked_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(UTC) - asked > SLOT_TTL


def _norm(step: str) -> str:
    return " ".join(step.split()).casefold()


def pick_plan(model_plan: list[str], playbook: list[str]) -> tuple[list[str], str]:
    """The plan to store and who suggested it. The model only picks and orders playbook steps:
    a step that isn't one of them is dropped, so a model can invent nothing.
    """
    by_text = {_norm(step): step for step in playbook}
    picked = list(dict.fromkeys(by_text[_norm(s)] for s in model_plan if _norm(s) in by_text))
    if picked:
        return picked[:MAX_PLAN_STEPS], "ai"
    return playbook[:MAX_PLAN_STEPS], "playbook"


class _Intake:
    def __init__(self, inbound: InboundMessage, resolved: identity.Resolved, hub: MCPHub, llm: LLM) -> None:
        self.inbound = inbound
        self.resolved = resolved
        self.hub = hub
        self.llm = llm
        self.context = dict(resolved.context)
        self.model_down = False  # set once a call raised LLMUnavailable: no second wait on a dead provider

    # ---------- the pipeline ----------

    async def run(self) -> None:
        awaiting = self.context.get("awaiting")
        if awaiting == "serial_number" and not _expired(self.context):
            await self._serial_slot()
            return
        if awaiting:  # a stale slot (or one that comes in a later block) is dropped
            await self._set_context({})

        text = self.inbound.text.strip()
        serial = extract_serial(text)
        extraction = await self._extract(text)
        if extraction is None:  # no model: a serial is enough to go on, anything else waits for an agent
            if serial is None:
                await self._reply(FALLBACK_REPLY_NO_TICKET)
            else:
                await self._with_serial(Issue.from_text(text), serial, misses=0)
            return

        intent = extraction.intent
        if intent != "new_issue" and self.resolved.ticket_id is not None:
            # Not built yet: §7.2 puts this message on that ticket (add_followup, Block 2).
            await self._reply(FOLLOW_UP)
            return
        if intent in ("smalltalk", "other", "payment_details"):
            await self._reply(GREETING.format(name=f" {self._first_name}" if self._first_name else "",
                                              company=get_settings().company_name))
            return
        # A new issue, or a follow-up with no ticket to follow: a new issue after all (§7.2).
        issue = Issue.from_extraction(extraction, text)
        if serial is None:
            await self._set_context({"awaiting": "serial_number", "issue": asdict(issue), "misses": 0,
                                     "asked_at": _now()})
            await self._reply(ASK_SERIAL.format(thanks=self._thanks))
            return
        await self._with_serial(issue, serial, misses=0)

    async def _serial_slot(self) -> None:
        issue = Issue.load(self.context.get("issue") or {})
        misses = int(self.context.get("misses") or 0)
        text = self.inbound.text.strip()
        serial = extract_serial(text)
        if len(SERIAL.sub(" ", text).split()) >= 2:  # more than the serial: it adds to the issue
            issue.text = f"{issue.text}\n\n{text}".strip()
            extraction = await self._extract(issue.text)
            if extraction is not None:
                issue = Issue.from_extraction(extraction, issue.text)
        if serial is None:
            misses += 1
            if misses >= MAX_SERIAL_MISSES:
                await self._open_ticket(issue, None)
                return
            await self._set_context({**self.context, "issue": asdict(issue), "misses": misses})
            await self._reply(SERIAL_MISSING)
            return
        await self._with_serial(issue, serial, misses)

    async def _with_serial(self, issue: Issue, serial: str, misses: int) -> None:
        device = await self._call("catalog__lookup_serial", {"serial_number": serial})
        if device.get("found"):
            await self._open_ticket(issue, device)
            return
        misses += 1
        if misses >= MAX_SERIAL_MISSES:
            await self._open_ticket(issue, None)
            return
        await self._set_context({"awaiting": "serial_number", "issue": asdict(issue), "misses": misses,
                                 "asked_at": self.context.get("asked_at") or _now()})
        await self._reply(SERIAL_UNKNOWN.format(serial=serial))

    async def _open_ticket(self, issue: Issue, device: dict[str, Any] | None) -> None:
        customer_id = str(self.resolved.customer_id)
        flags: list[str] = []
        if device is None:
            flags.append("unverified_product")
        else:
            owner = device.get("owner_customer_id")
            if owner is None:
                linked = await self._try("catalog__link_product_to_customer",
                                         {"product_id": device["product_id"], "customer_id": customer_id})
                if linked is None:  # someone registered it meanwhile, or catalog is down: the agent checks
                    flags.append("ownership_mismatch")
            elif owner != customer_id:
                # Never moved to this customer (§6.3); the reply reveals nothing about the owner.
                flags.append("ownership_mismatch")
            if (device.get("warranty") or {}).get("status") == "out_of_warranty":
                flags.append("out_of_warranty")

        created = await self._call("tickets__create_ticket", {
            "customer_id": customer_id,
            "product_id": device["product_id"] if device else None,
            "category": issue.category,
            "issue_type": issue.issue_type,
            "title": issue.title,
            "description": issue.text or issue.title,
            "source_channel": self.resolved.channel,
            "conversation_id": str(self.resolved.conversation_id),
            "flags": flags,
            "priority": PRIORITY.get(issue.urgency, "medium"),
        })
        ticket_number = created["ticket_number"]
        log.info("intake: %s created on %s", ticket_number, self.resolved.channel)
        await self._set_context({})
        plan = await self._notes_and_plan(created["ticket_id"], issue, device)

        if self.model_down:
            text = FALLBACK_REPLY_TICKET.format(ticket_number=ticket_number)
        elif device is None:
            text = TICKET_OPENED_UNVERIFIED.format(thanks=self._thanks, ticket_number=ticket_number,
                                                   title=issue.title)
        else:
            text = TICKET_OPENED.format(thanks=self._thanks, ticket_number=ticket_number, device=device["name"],
                                        serial=device["serial_number"], title=issue.title)
            if issue.category == "software" and plan:  # safe self-help tips for software issues only (§7.1)
                text += TIPS.format(steps="\n".join(f"{n}. {s}" for n, s in enumerate(plan[:MAX_TIPS], start=1)))
        await self._reply(text)

    async def _notes_and_plan(self, ticket_id: str, issue: Issue, device: dict[str, Any] | None) -> list[str]:
        """Playbook → the AI summary and the first diagnostic plan (§7.1). A failure here costs the
        notes, never the ticket or the reply.
        """
        playbook: list[str] = []
        if device is not None:
            found = await self._try("knowledge__get_playbook", {"issue_type": issue.issue_type,
                                                                "category": device["category"],
                                                                "model_id": device["model_id"]})
            if found and found.get("found"):
                playbook = [s["step"] for s in found.get("steps", []) if s.get("step")]

        notes = await self._notes(issue, device, playbook)
        if notes is not None and notes.summary.strip():
            await self._try("tickets__update_summary", {"ticket_id": ticket_id,
                                                        "summary": notes.summary.strip()[:MAX_SUMMARY]})
        plan, suggested_by = pick_plan(notes.plan if notes else [], playbook)
        if plan:
            await self._try("tickets__set_diagnostic_plan", {"ticket_id": ticket_id, "steps": plan,
                                                             "suggested_by": suggested_by})
        return plan

    # ---------- the model ----------

    async def _extract(self, text: str) -> IntakeExtraction | None:
        if self.model_down:
            return None
        try:
            extraction, _ = await self.llm.complete_json(
                [{"role": "system", "content": EXTRACT_PROMPT}, {"role": "user", "content": text[:MAX_MODEL_TEXT]}],
                IntakeExtraction, tier="fast")
        except LLMUnavailable as exc:
            log.warning("intake: no model for the extraction (%s); going on without it", exc)
            self.model_down = True
            return None
        return extraction

    async def _notes(self, issue: Issue, device: dict[str, Any] | None, playbook: list[str]) -> TicketNotes | None:
        if self.model_down:
            return None
        about = f"{device['name']} ({device['category']}, serial {device['serial_number']})" if device else "not verified"
        steps = "\n".join(f"- {s}" for s in playbook) or "(none)"
        prompt = (f"Customer wrote:\n{issue.text[:MAX_MODEL_TEXT]}\n\nIssue: {issue.issue_type} ({issue.category})\n"
                  f"Device: {about}\n\nPlaybook steps:\n{steps}")
        try:
            notes, _ = await self.llm.complete_json(
                [{"role": "system", "content": NOTES_PROMPT}, {"role": "user", "content": prompt}], TicketNotes,
                tier="fast")
        except LLMUnavailable as exc:
            log.warning("intake: no model for the ticket notes (%s); the playbook is the plan", exc)
            self.model_down = True
            return None
        return notes

    # ---------- tools, context, replies ----------

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A step intake can't go on without: a refusal raises IntakeError, a down server MCPUnavailable."""
        result = await call(name, arguments, self.hub)
        if not result.ok or not isinstance(result.data, dict):
            raise IntakeError(f"{name} refused: {result.error or 'unexpected result'}")
        return result.data

    async def _try(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """A step intake can do without: a refusal or a down server is logged and gives None."""
        try:
            result = await call(name, arguments, self.hub)
        except MCPUnavailable as exc:
            log.warning("intake: %s", exc)
            return None
        if not result.ok or not isinstance(result.data, dict):
            log.warning("intake: %s refused: %s", name, result.error or "unexpected result")
            return None
        return result.data

    async def _set_context(self, context: dict[str, Any]) -> None:
        if context != self.context:
            await identity.set_context(self.resolved.conversation_id, context)
            self.context = context

    async def _reply(self, text: str) -> None:
        # The conversation decides the channel (§6.1): no argument here can choose it.
        await self._call("messaging__send_reply", {"conversation_id": str(self.resolved.conversation_id),
                                                   "text": text})

    @property
    def _first_name(self) -> str | None:
        name = (self.resolved.customer_name or self.inbound.display_name or "").strip()
        return name.split()[0] if name else None

    @property
    def _thanks(self) -> str:
        return f"Thanks, {self._first_name}. " if self._first_name else "Thanks. "


async def handle_inbound(inbound: InboundMessage, *, hub: MCPHub | None = None, llm: LLM | None = None) -> None:
    """Intake for one inbound message (§7.1). channels.inbound.run_intake runs it for every adapter."""
    resolved = await identity.resolve(inbound)
    if resolved.repeat:  # the channel redelivered a message already handled
        return
    await _Intake(inbound, resolved, hub or get_hub(), llm or get_llm()).run()
