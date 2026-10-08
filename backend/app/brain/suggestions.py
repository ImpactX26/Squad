"""Suggested action chips on the ticket page (ARCHITECTURE.md §7.5).

    which commands can run on this ticket now   code: status, device, warranty, open payment / job
    -> one decide call: one yes/no question per command (built-ins + the agent's custom commands)
    -> the yeses, most likely first, filled up to 3 from a fixed order in code, at most 4
    -> chips; a command that needs the agent's words (/escalate, /close) only fills the composer

The model only ranks. Which commands are offered, and the words /ask asks for, are decided in code,
and a chip runs the command through the same route and gates as typing it (§4.6). Cached per ticket
until the next message (or a status, payment or job change). No model reachable: the code order alone.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.brain.commands import BUILTINS
from app.brain.decide import Decider, Question, default_decider
from app.brain.llm import LLMUnavailable

log = logging.getLogger(__name__)

MIN_CHIPS = 3
MAX_CHIPS = 4
MAX_CUSTOM = 5
CACHE_MAX_ENTRIES = 256

# What /ask asks for, by the §4.4 issue type. Chosen in code: the model never writes the words.
ASK_FOR: dict[str, str] = {
    "battery": "a screenshot of the battery health report",
    "charging": "a photo of the charger and the charging light",
    "display": "a photo of the screen showing the problem",
    "keyboard": "a photo of the keyboard and which keys fail",
    "audio": "a short recording of the sound",
    "overheating": "a photo of where it gets hot and what was running",
    "boot": "a photo of the screen when it fails to start",
    "os": "a photo of the error message on screen",
    "driver": "the exact error message and the device name",
    "performance": "a screenshot of Task Manager while it is slow",
    "connectivity": "the Wi-Fi or Bluetooth device it fails with",
}
DEFAULT_ASK = "photos of the problem"


@dataclass(frozen=True)
class TicketFacts:
    """What the chips are picked from, read by the API in one query."""

    ticket_id: str
    ticket_number: str
    title: str
    status: str
    priority: str
    issue_type: str
    category: str
    ai_summary: str | None
    has_product: bool
    in_warranty: bool
    open_payment: bool
    open_job: bool
    pending_steps: int
    tried: list[str] = field(default_factory=list)
    last_customer_message: str | None = None
    version: str = ""  # changes with each new message, status, priority, payment or job


@dataclass(frozen=True)
class Chip:
    name: str
    args: str
    label: str
    needs_args: bool  # fill the composer with "/name " instead of running


@dataclass(frozen=True)
class Suggestions:
    chips: list[Chip]
    source: str  # "ai" or "rules"
    cached: bool = False


def candidates(facts: TicketFacts, custom: list[dict[str, Any]]) -> list[tuple[Chip, str]]:
    """The commands that can run on this ticket now, in the code's own order, each with what it does."""
    closed = facts.status in ("resolved", "closed")
    issue = facts.issue_type if facts.issue_type not in ("", "other") else ""
    what = ASK_FOR.get(facts.issue_type, DEFAULT_ASK)
    offers: list[tuple[str, Chip, bool]] = [
        ("summary", Chip("summary", "", "Summarize", False), not facts.ai_summary),
        # Only when nothing on the checklist is left to try: otherwise /diagnose would add nothing new.
        ("diagnose", Chip("diagnose", "", f"Run {issue} diagnostics".replace("  ", " "), False),
         not closed and facts.pending_steps == 0),
        ("diagnose-send", Chip("diagnose-send", "", "Send the steps to the customer", False),
         not closed and facts.pending_steps > 0),
        ("payments", Chip("payments", "", "/payments", False),
         not closed and facts.has_product and not facts.in_warranty and not facts.open_payment and not facts.open_job),
        ("schedule", Chip("schedule", "", "Schedule the warranty repair", False),
         not closed and facts.has_product and facts.in_warranty and not facts.open_job),
        ("ask", Chip("ask", what, f"Ask for {what}", False), not closed),
        ("parts", Chip("parts", "", "Check parts", False), facts.has_product and facts.category != "software"),
        ("summary", Chip("summary", "", "Summarize", False), bool(facts.ai_summary)),
        ("escalate", Chip("escalate", "", "Escalate…", True), not closed and facts.priority != "urgent"),
        ("close", Chip("close", "", "Close…", True), not closed and not facts.open_job and not facts.open_payment),
    ]
    chosen: list[tuple[Chip, str]] = []
    for name, chip, ok in offers:
        if ok and name not in {c.name for c, _ in chosen}:
            chosen.append((chip, BUILTINS[name].description))
    if not closed:
        for command in custom[:MAX_CUSTOM]:
            chosen.append((Chip(command["name"], "", f"/{command['name']}", False), command["description"]))
    return chosen


def ticket_state(facts: TicketFacts) -> str:
    """What the decision model reads."""
    lines = [
        f"Ticket {facts.ticket_number}: {facts.title}",
        f"Status {facts.status}, priority {facts.priority}, issue type {facts.issue_type}, {facts.category}.",
        "Device verified, " + ("in warranty." if facts.in_warranty else "out of warranty.")
        if facts.has_product else "No verified device.",
        f"Open payment: {'yes' if facts.open_payment else 'no'}. Technician job booked: {'yes' if facts.open_job else 'no'}.",
        f"Diagnostic steps still to try: {facts.pending_steps}.",
    ]
    if facts.tried:
        lines.append("Already tried: " + "; ".join(facts.tried)[:400])
    if facts.ai_summary:
        lines.append(f"Summary: {facts.ai_summary[:400]}")
    if facts.last_customer_message:
        lines.append(f"Customer's last message: {facts.last_customer_message[:400]}")
    return "\n".join(lines)


_cache: OrderedDict[tuple, Suggestions] = OrderedDict()


def clear_cache() -> None:
    _cache.clear()


async def suggest(facts: TicketFacts, custom: list[dict[str, Any]], *, staff_id: str,
                  decider: Decider | None = None) -> Suggestions:
    """3-4 chips for this ticket: one decide call ranks what code offers; cached until the ticket changes."""
    offered = candidates(facts, custom)
    key = (facts.ticket_id, staff_id, facts.version, tuple((c.name, c.args) for c, _ in offered))
    if (hit := _cache.get(key)) is not None:
        _cache.move_to_end(key)
        return Suggestions(hit.chips, hit.source, cached=True)

    order = {chip.name: i for i, (chip, _) in enumerate(offered)}
    chips: list[Chip] = []
    source = "rules"
    if offered:
        decider = decider or default_decider()
        questions = {chip.name: Question.noul(f"Would running /{chip.name} ({description}) be a useful next step "
                                              "for the support agent on this ticket?")
                     for chip, description in offered}
        try:
            answers = await decider.decide(ticket_state(facts), questions)
            source = "ai"
            yes = sorted(((answers[c.name].p_yes or 0.0, c) for c, _ in offered
                          if c.name in answers and (answers[c.name].p_yes or 0.0) >= 0.5),
                         key=lambda pair: (-pair[0], order[pair[1].name]))
            chips = [chip for _, chip in yes][:MAX_CHIPS]
        except LLMUnavailable as e:
            log.info("suggestions: no decision model, using the code order: %s", e)
    for chip, _ in offered:  # fill from the code's order
        if len(chips) >= MIN_CHIPS:
            break
        if chip not in chips:
            chips.append(chip)
    result = Suggestions(chips[:MAX_CHIPS], source)
    _cache[key] = result
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
    return result