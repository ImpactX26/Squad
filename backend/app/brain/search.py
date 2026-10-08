"""Natural-language ticket search (ARCHITECTURE.md §7.4).

    "open battery tickets from telegram this week that got escalated"
      -> MODEL_FAST (complete_json): {open_only, issue_type, source_channel, created_within_days,
         min_duplicate_count, text}
      -> every value clamped to the real §8.1 / §4.4 vocabularies in code (unknown ones dropped)
      -> tickets.search_tickets: SQL filters + full-text + pgvector, reciprocal-rank fusion
      -> each result with a one-line "why this matched", written in code

One model call per search, none for a ticket number ("SR-2026-00042" goes straight to
tickets.get_ticket). No model reachable: a plain text search of the words, and the agent is told.
Search reads tickets and nothing else: its tools are an allowlist checked in code (SEARCH_TOOLS).
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.brain.commands import CommandTools
from app.brain.decide import ISSUE_TYPE_OPTIONS, extract_serial
from app.brain.llm import LLM, LLMUnavailable, default_llm
from app.brain.prompts import load as load_prompt
from app.brain.runtime import AiRunRecord, RunLogger, log_ai_run, log_run_in_background
from app.core.events import EventBus

log = logging.getLogger(__name__)

ROLE = "copilot"
TRIGGER = "search"
SEARCH_TOOLS: frozenset[str] = frozenset({"tickets__search_tickets", "tickets__get_ticket"})
LIMIT = 20

STATUSES = ("new", "in_progress", "awaiting_customer", "awaiting_payment", "scheduled", "resolved", "closed")
PRIORITIES = ("urgent", "high", "medium", "low")
ISSUE_TYPES = tuple(ISSUE_TYPE_OPTIONS)
CATEGORIES = ("hardware", "software", "unknown")
CHANNELS = ("discord", "telegram", "email", "web")
MAX_DAYS = 365
MAX_TEXT = 200

TICKET_NUMBER = re.compile(r"\bSR-\d{4}-\d{5}\b", re.IGNORECASE)

_STATUS_LABEL = {"in_progress": "in progress", "awaiting_customer": "awaiting customer",
                 "awaiting_payment": "awaiting payment"}
_CHANNEL_LABEL = {"discord": "Discord", "telegram": "Telegram", "email": "Email", "web": "Web chat"}


class ModelFilters(BaseModel):
    """What MODEL_FAST returns. Lenient on purpose: every value is clamped in code afterwards."""

    model_config = ConfigDict(extra="ignore")

    status: list[str] = []
    open_only: bool = False
    priority: list[str] = []
    issue_type: list[str] = []
    category: list[str] = []
    source_channel: list[str] = []
    created_within_days: int | None = None
    min_duplicate_count: int | None = None
    text: str = ""

    @field_validator("status", "priority", "issue_type", "category", "source_channel", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        return [str(value)] if isinstance(value, str) else [str(v) for v in value]

    @field_validator("text", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return "" if value is None else str(value)


@dataclass(frozen=True)
class SearchPlan:
    """The query as code understood it: tickets.search_tickets' filters and text, and one line for the agent."""

    filters: dict[str, Any]
    text: str
    summary: str
    ai: bool
    notice: str | None = None
    filter_summary: str = ""  # just the filters, e.g. "open, battery, from Telegram"


@dataclass(frozen=True)
class SearchOutcome:
    plan: SearchPlan
    results: list[dict[str, Any]]  # search_tickets rows, each with "why"
    ranking: str                   # "rrf", "recency" or "ticket_number"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _keep(values: list[str], allowed: tuple[str, ...]) -> list[str]:
    wanted = {v.strip().lower().replace(" ", "_") for v in values}
    return [v for v in allowed if v in wanted]


def clamp(raw: ModelFilters, query: str, *, now: datetime) -> SearchPlan:
    """The model's filters, limited to the real vocabularies in code; anything else is dropped."""
    filters: dict[str, Any] = {}
    parts: list[str] = []
    statuses = _keep(raw.status, STATUSES)
    if raw.open_only and not statuses:
        filters["open_only"] = True
        parts.append("open")
    if statuses:
        filters["status"] = statuses
        parts.append(" or ".join(_STATUS_LABEL.get(s, s) for s in statuses))
    if priorities := _keep(raw.priority, PRIORITIES):
        filters["priority"] = priorities
        parts.append(" or ".join(priorities) + " priority")
    if issues := _keep(raw.issue_type, ISSUE_TYPES):
        filters["issue_type"] = issues
        parts.append(" or ".join(issues))
    if categories := _keep(raw.category, CATEGORIES):
        filters["category"] = categories
        parts.append(" or ".join(categories))
    if channels := _keep(raw.source_channel, CHANNELS):
        filters["source_channel"] = channels
        parts.append("from " + " or ".join(_CHANNEL_LABEL[c] for c in channels))
    if raw.created_within_days and raw.created_within_days > 0:
        days = min(raw.created_within_days, MAX_DAYS)
        filters["created_after"] = (now - timedelta(days=days)).isoformat()
        parts.append("today" if days == 1 else f"last {days} days")
    if raw.min_duplicate_count and raw.min_duplicate_count > 0:
        filters["min_duplicate_count"] = min(raw.min_duplicate_count, 50)
        parts.append("followed up" if filters["min_duplicate_count"] == 1
                     else f"{filters['min_duplicate_count']}+ follow-ups")
    text = " ".join(raw.text.split())[:MAX_TEXT]
    if not text and not filters:
        text = " ".join(query.split())[:MAX_TEXT]  # the model found nothing to filter on: search the words
    summary = ", ".join(parts)
    if text:
        summary = f"{summary}; matching “{text}”" if summary else f"matching “{text}”"
    return SearchPlan(filters=filters, text=text, summary=summary or "every ticket, newest first", ai=True,
                      filter_summary=", ".join(parts))


def why(row: dict[str, Any], plan: SearchPlan, ranking: str) -> str:
    """One line saying why this ticket is in the results (§7.4), from what the search really did."""
    reasons = []
    matched = row.get("matched_by") or []
    if "full-text" in matched:
        reasons.append(f"its words match “{plan.text}”")
    if "similarity" in matched:
        similarity = row.get("similarity")
        reasons.append("similar in meaning" + (f" ({round(float(similarity) * 100)}%)" if similarity else ""))
    if ranking == "recency":
        reasons.append("newest first")
    if plan.filter_summary:
        reasons.append(plan.filter_summary)
    line = "; ".join(reasons)
    return line[:1].upper() + line[1:] if line else "Matches the search"


async def interpret(query: str, *, llm: LLM | None = None, now: datetime | None = None) -> tuple[SearchPlan, Any]:
    """The query as filters (one MODEL_FAST call), or a plain text search when no model answers."""
    now = now or datetime.now(UTC)
    messages = [{"role": "system", "content": load_prompt("search_filters")},
                {"role": "user", "content": query}]
    try:
        raw, result = await (llm or default_llm()).complete_json(messages, ModelFilters, tier="fast")
    except LLMUnavailable as e:
        log.warning("search: no model for the filters, searching the words: %s", e)
        text = " ".join(query.split())[:MAX_TEXT]
        return SearchPlan(filters={}, text=text, summary=f"matching “{text}”", ai=False,
                          notice="AI filters are unavailable right now, so this is a plain text search."), None
    return clamp(raw, query, now=now), result


async def search(
    query: str,
    *,
    call_tool=None,
    llm: LLM | None = None,
    events: EventBus | None = None,
    log_run: RunLogger | None = None,
    now: datetime | None = None,
    limit: int = LIMIT,
) -> SearchOutcome:
    """Run one natural-language search. Raises McpToolError when the tickets server can't answer."""
    tools = CommandTools(SEARCH_TOOLS, call_tool, events, trigger=TRIGGER)
    start = time.monotonic()
    model = tokens_in = tokens_out = error = None
    try:
        if (number := TICKET_NUMBER.search(query)) is not None:
            plan = SearchPlan(filters={}, text="", summary=f"ticket {number.group(0).upper()}", ai=False)
            found = await tools.call("tickets__get_ticket", ticket_number=number.group(0).upper())
            rows = [found] if isinstance(found, dict) and found.get("found") else []
            results = [{**row, "why": f"Ticket number {row['ticket_number']}"} for row in rows]
            return SearchOutcome(plan, results, "ticket_number", tools.records)

        if (serial := extract_serial(query)) is not None:
            # A device serial: that device's tickets, no model call (like a ticket number above).
            plan = SearchPlan(filters={}, text=serial, summary=f"device {serial}", ai=False)
            found = await tools.call("tickets__search_tickets", query=serial, filters={}, limit=limit)
            rows = found.get("results", []) if isinstance(found, dict) else []
            results = [{**row, "why": f"Device serial {serial}"} for row in rows]
            return SearchOutcome(plan, results, "serial", tools.records)

        plan, result = await interpret(query, llm=llm, now=now)
        if result is not None:
            model, tokens_in, tokens_out = f"{result.provider}:{result.model}", result.input_tokens, result.output_tokens
        found = await tools.call("tickets__search_tickets", query=plan.text, filters=plan.filters, limit=limit)
        rows = found.get("results", []) if isinstance(found, dict) else []
        ranking = str(found.get("ranking") or "rrf") if isinstance(found, dict) else "rrf"
        results = [{**row, "why": why(row, plan, ranking)} for row in rows]
        return SearchOutcome(plan, results, ranking, tools.records)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        log_run_in_background(log_run or log_ai_run, AiRunRecord(
            role=ROLE, trigger=TRIGGER, ticket_id=None, model=model, input_tokens=tokens_in,
            output_tokens=tokens_out, tool_calls=tools.records,
            latency_ms=round((time.monotonic() - start) * 1000), error=error))