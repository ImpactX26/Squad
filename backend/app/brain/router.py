"""Send each request only the MCP tools it needs (ARCHITECTURE.md §4.3, §4.6).

Groq's free tier allows ~6-8K tokens per minute, and the tool schemas of all 7 MCP servers can
use that up in a single request. pick_servers chooses a few servers, filter_tools keeps their
tools, and compact_tools trims the schemas.

ROLE_SERVERS is enforced in code after any model has answered, so a customer message can never
reach payments, dispatch, or inventory, whatever the decision says.
"""

import logging
import re
from typing import Any, Literal

from app.brain.decide import Decider, Question, default_decider, extract_serial
from app.brain.llm import LLMUnavailable

log = logging.getLogger(__name__)

Role = Literal["intake", "copilot", "writer", "automation"]

# One line per server; also the text of the routing questions.
SERVERS: dict[str, str] = {
    "tickets": "create, find, update, and search support tickets",
    "catalog": "look up serial numbers, product models, warranty, owners, and service prices",
    "knowledge": "diagnostic playbooks and next troubleshooting steps",
    "messaging": "reply to the customer, send email, or notify staff",
    "payments": "create payment links and check payment status",
    "dispatch": "find and schedule a technician visit",
    "inventory": "check, reserve, and restock spare parts",
}

ROLE_SERVERS: dict[str, frozenset[str]] = {
    "intake": frozenset({"catalog", "tickets", "knowledge", "messaging"}),
    "copilot": frozenset(SERVERS),
    "writer": frozenset(),
    "automation": frozenset({"payments", "dispatch", "inventory", "tickets", "messaging"}),
}

# Word-start patterns per server, matched case-insensitively.
KEYWORDS: dict[str, re.Pattern[str]] = {
    server: re.compile(r"\b(?:" + "|".join(patterns) + ")", re.IGNORECASE)
    for server, patterns in {
        "payments": [r"pay", r"paid\b", r"pric", r"invoice", r"cost", r"refund", r"quote\b", r"bill"],
        "dispatch": [r"technician", r"visit", r"schedul", r"appointment", r"dispatch", r"on[- ]?site\b", r"slot"],
        "inventory": [r"stock", r"restock", r"parts?\b", r"spare", r"inventory", r"warehouse", r"reserve",
                      r"running low", r"low on\b", r"sku"],
        "catalog": [r"serial", r"warranty", r"model\b", r"model number", r"specs?\b", r"owner", r"registered"],
        "tickets": [r"ticket", r"sr-\d", r"status\b", r"escalat", r"priority", r"clos(?:e|ed|ing)\b", r"duplicate"],
        "knowledge": [r"diagnos", r"troubleshoot", r"playbook", r"fix", r"next steps?\b"],
        "messaging": [r"reply", r"message", r"email", r"notify", r"tell the customer", r"ask the customer"],
    }.items()
}

DEFAULT_SERVERS = ["tickets", "knowledge"]
MAX_DECIDED_SERVERS = 3

# The stock and dispatch writers. Only the fixed workflows (role automation, app/brain/workflows.py)
# and the job API call them, in code; no model is ever offered one (§4.2, §4.6).
WORKFLOW_ONLY_TOOLS: frozenset[str] = frozenset({
    "inventory__reserve_part",
    "inventory__consume_part",
    "inventory__release_part",
    "inventory__create_restock_request",
    "dispatch__create_job",
    "dispatch__update_job_status",
    "dispatch__reject_job",
})

# Creating and cancelling a payment link. Billing a customer is a staff action: only the fixed
# /payments pipeline calls create_payment_request, in code (commands.finish_payment_details), so
# neither the copilot nor a custom command may bill or cancel, whatever the model asks for. They
# are in MODEL_FORBIDDEN_TOOLS. commands.CommandTools lets that one pipeline keep its own
# create_payment_request call (BILLING_PIPELINE_TOOLS) and refuses both to every other gate,
# including the model-driven gate of a custom command; no code calls cancel_payment.
PAYMENT_LINK_TOOLS: frozenset[str] = frozenset({
    "payments__create_payment_request",
    "payments__cancel_payment",
})
BILLING_PIPELINE_TOOLS: frozenset[str] = frozenset({"payments__create_payment_request"})

# The payments page's manual path (§11.2): an admin's own writes, and the staff listing behind it.
# Only app/api/payments.py calls them, in code, with the signed-in admin's id; no gate offers them.
PAYMENT_ADMIN_TOOLS: frozenset[str] = frozenset({
    "payments__list_payments",
    "payments__correct_utr_manually",
    "payments__reject_payment",
    "payments__extend_payment",
})

# Tools no model is ever offered, whatever its role or a command's allowed_tools say. Marking a
# payment paid is an admin's action and submitting a UTR is the customer's, never a model's
# choice (§4.6: decisions never authorize payment, dispatch or stock changes), and the payment-link
# tools above are staff actions too. filter_tools drops them in code, and runtime.run_tool_loop
# refuses them again.
MODEL_FORBIDDEN_TOOLS: frozenset[str] = frozenset({
    "payments__mark_paid_manually",
    "payments__submit_utr",
}) | WORKFLOW_ONLY_TOOLS | PAYMENT_LINK_TOOLS | PAYMENT_ADMIN_TOOLS


async def pick_servers(text: str, role: Role, *, decider: Decider | None = None) -> list[str]:
    """Servers for this request, in order: Jev (if enabled), keywords, an LLM decision, then the default.

    Every result is intersected with ROLE_SERVERS[role] here, after the model has answered.
    """
    allowed = ROLE_SERVERS[role]
    if not allowed:
        return []
    decider = decider or default_decider()
    provider = decider.settings.decision_provider
    picked: list[str] = []
    if provider == "jev":
        picked = _only_allowed(await _ask(decider, text, allowed), allowed)[:MAX_DECIDED_SERVERS]
    if not picked:
        picked = _only_allowed(keyword_servers(text), allowed)
    if not picked and provider == "llm":
        # With jev, step 1 already asked (and decide() falls back to the LLM by itself).
        picked = _only_allowed(await _ask(decider, text, allowed), allowed)[:MAX_DECIDED_SERVERS]
    if not picked:
        picked = _only_allowed(DEFAULT_SERVERS, allowed)
    return picked


def keyword_servers(text: str) -> list[str]:
    found = [server for server, pattern in KEYWORDS.items() if pattern.search(text)]
    if "catalog" not in found and extract_serial(text):
        found.append("catalog")
    return found


async def _ask(decider: Decider, text: str, allowed: frozenset[str]) -> list[str]:
    """Servers the decision model says yes to (p >= 0.5), most likely first."""
    questions = {s: Question.noul(f"Does this request need {s}: {SERVERS[s]}?") for s in sorted(allowed)}
    try:
        answers = await decider.decide(text, questions)
    except LLMUnavailable as e:
        log.warning("server routing decision unavailable, using the next step: %s", e)
        return []
    yes = [(answer.p_yes or 0.0, server) for server, answer in answers.items() if (answer.p_yes or 0.0) >= 0.5]
    return [server for _, server in sorted(yes, key=lambda pair: (-pair[0], pair[1]))]


def _only_allowed(servers: list[str], allowed: frozenset[str]) -> list[str]:
    return [s for s in dict.fromkeys(servers) if s in allowed]


# ---------- tools ----------


def tool_name(tool: dict[str, Any]) -> str:
    """The `<server>__<tool>` name of an OpenAI function tool."""
    return tool["function"]["name"]


def filter_tools(
    tools: list[dict[str, Any]], servers: list[str] | frozenset[str], allowed_tools: list[str] | None = None
) -> list[dict[str, Any]]:
    """Tools of `servers`, further limited to `allowed_tools` when given (slash commands, §7.5).

    MODEL_FORBIDDEN_TOOLS never pass, even when a command lists them.
    """
    prefixes = tuple(f"{server}__" for server in servers)
    wanted = set(allowed_tools) if allowed_tools is not None else None
    return [
        t for t in tools
        if tool_name(t).startswith(prefixes)
        and (wanted is None or tool_name(t) in wanted)
        and tool_name(t) not in MODEL_FORBIDDEN_TOOLS
    ]


def compact_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each description cut to its first sentence; schema "title" and "examples" dropped."""
    compacted = []
    for tool in tools:
        function = dict(tool["function"])
        if function.get("description"):
            function["description"] = first_sentence(function["description"])
        if "parameters" in function:
            function["parameters"] = _compact_schema(function["parameters"])
        compacted.append({**tool, "function": function})
    return compacted


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+|\n", text.strip(), maxsplit=1)[0]


def _compact_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_compact_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            # Keys here are property names (a tool may well have a "title" argument), not schema keywords.
            out[key] = {name: _compact_schema(schema) for name, schema in value.items()}
        elif key in ("title", "examples"):
            continue
        elif key == "description" and isinstance(value, str):
            out[key] = first_sentence(value)
        else:
            out[key] = _compact_schema(value)
    return out