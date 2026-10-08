"""Agent runtime: the §4.3 tool loop in OpenAI function-tool format, plus ai_runs logging.

call_tool is injected (async (name, args) -> result), so the loop works before mcp_hub.py exists
and in tests. `tools` must already be filtered for the role (router.filter_tools); any tool name
the model invents outside that list is rejected, never executed.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.brain.llm import LLM, ToolCall, default_llm
from app.brain.router import MODEL_FORBIDDEN_TOOLS, tool_name
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.events import EventBus, bus
from app.models import AiRun

log = logging.getLogger(__name__)

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

# One tool result can't use up Groq's per-minute token budget on its own.
MAX_TOOL_RESULT_CHARS = 4000
AI_RUN_INSERT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class AiRunRecord:
    """One ai_runs row."""

    role: str
    trigger: str
    ticket_id: uuid.UUID | str | None
    model: str | None  # "<provider>:<model>", comma-separated if a fallback served part of the run
    input_tokens: int | None
    output_tokens: int | None
    tool_calls: list[dict[str, Any]]  # [{"tool": "inventory__reserve_part", "ok": true, "ms": 42}]
    latency_ms: int
    error: str | None


RunLogger = Callable[[AiRunRecord], Awaitable[None]]


@dataclass(frozen=True)
class LoopResult:
    text: str  # may be empty when hit_limit is true
    messages: list[dict[str, Any]]  # the whole conversation, tool turns included
    tool_calls: list[dict[str, Any]]  # as stored in ai_runs
    iterations: int  # tool rounds executed
    hit_limit: bool  # stopped at AI_MAX_TOOL_ITERATIONS with tool calls still pending
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


async def run_tool_loop(
    role: str,
    trigger: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    call_tool: ToolCaller,
    *,
    ticket_id: uuid.UUID | str | None = None,
    llm: LLM | None = None,
    log_run: RunLogger | None = None,
    events: EventBus | None = None,
    max_iterations: int | None = None,
) -> LoopResult:
    """Call MODEL_SMART; while it asks for tools, run them all (concurrently) and call it again.

    Raises LLMUnavailable when no provider can answer; the ai_runs row is written either way.
    """
    llm = llm or default_llm()
    log_run = log_run or log_ai_run
    events = events or bus
    max_rounds = max_iterations if max_iterations is not None else get_settings().ai_max_tool_iterations
    # router.filter_tools already drops these; a caller that skipped it still can't offer them (§4.6).
    tools = [t for t in tools if tool_name(t) not in MODEL_FORBIDDEN_TOOLS]
    allowed = {tool_name(t) for t in tools}

    convo = list(messages)
    records: list[dict[str, Any]] = []
    models: list[str] = []
    tokens_in: int | None = None
    tokens_out: int | None = None
    rounds = 0
    error: str | None = None
    start = time.monotonic()
    try:
        while True:
            result = await llm.complete(convo, tier="smart", tools=tools or None)
            models.append(f"{result.provider}:{result.model}")
            tokens_in, tokens_out = _add(tokens_in, result.input_tokens), _add(tokens_out, result.output_tokens)
            # The tool calls decide, not finish_reason: Ollama can report "stop" alongside tool calls.
            if not result.tool_calls or rounds >= max_rounds:
                break
            rounds += 1
            convo.append(result.assistant_message())
            outcomes = await asyncio.gather(*(
                _execute(tc, allowed, call_tool, events, role=role, trigger=trigger, ticket_id=ticket_id)
                for tc in result.tool_calls
            ))
            for tc, (content, record) in zip(result.tool_calls, outcomes):
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": content})
                records.append(record)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        _log_in_background(log_run, AiRunRecord(
            role=role,
            trigger=trigger,
            ticket_id=ticket_id,
            model=",".join(dict.fromkeys(models)) or None,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            tool_calls=records,
            latency_ms=_elapsed_ms(start),
            error=error,
        ))
    if result.tool_calls:
        log.warning("%s %s stopped at AI_MAX_TOOL_ITERATIONS=%d with tool calls pending", role, trigger, max_rounds)
    return LoopResult(
        text=result.text,
        messages=convo,
        tool_calls=records,
        iterations=rounds,
        hit_limit=bool(result.tool_calls),
        model=",".join(dict.fromkeys(models)),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=_elapsed_ms(start),
    )


async def _execute(
    tc: ToolCall,
    allowed: set[str],
    call_tool: ToolCaller,
    events: EventBus,
    *,
    role: str,
    trigger: str,
    ticket_id: uuid.UUID | str | None,
) -> tuple[str, dict[str, Any]]:
    """Run one tool call. Failures go back to the model as {"error": ...} so it can recover."""
    start = time.monotonic()
    ok = False
    if tc.name not in allowed:
        log.warning("%s %s: model asked for tool %r, which it wasn't given; rejected", role, trigger, tc.name[:100])
        content = _compact({"error": f"unknown tool {tc.name[:100]!r}; use only the tools provided"})
    elif tc.arguments is None:
        content = _compact({"error": "arguments must be a JSON object"})
    else:
        try:
            content = _compact(await call_tool(tc.name, tc.arguments))
            ok = True
        except Exception as e:
            log.warning("%s %s: tool %s failed: %s: %s", role, trigger, tc.name, type(e).__name__, e)
            content = _compact({"error": f"{type(e).__name__}: {e}"[:300]})
    record = {"tool": tc.name[:100], "ok": ok, "ms": _elapsed_ms(start)}
    await events.publish("agent.tool_called", {
        "role": role, "trigger": trigger, "ticket_id": str(ticket_id) if ticket_id else None, **record,
    })
    return content, record


def _compact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + "...(truncated)"


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


def _elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


# ---------- ai_runs logging: fire-and-forget, never fails the request ----------

_pending_logs: set[asyncio.Task[None]] = set()


def log_run_in_background(log_run: RunLogger, record: AiRunRecord) -> None:
    """Write one ai_runs row without making the caller wait, and never fail them for it.

    Used by the tool loop above and by the fixed pipelines (intake, workflows), so every role
    logs its run the same way (ARCHITECTURE.md §4.3 step 4).
    """
    async def safe() -> None:
        try:
            await log_run(record)
        except Exception as e:
            log.warning("ai_runs insert failed (ignored): %s: %s", type(e).__name__, e)

    task = asyncio.create_task(safe())
    _pending_logs.add(task)  # keep a reference until it's done
    task.add_done_callback(_pending_logs.discard)


_log_in_background = log_run_in_background  # the name the tool loop above already uses


async def drain_pending_logs(timeout: float = 5.0) -> int:
    """Wait for the ai_runs writes still in flight. Returns how many there were.

    Logging is fire-and-forget (§4.3 step 4), so nothing waits on it in normal operation. Shutdown
    calls this so the last rows are not lost, and tests call it to assert on what was written.
    """
    pending = [task for task in _pending_logs if not task.done()]
    if not pending:
        return 0
    await asyncio.wait(pending, timeout=timeout)
    return len(pending)


async def log_ai_run(record: AiRunRecord) -> None:
    row = asdict(record)
    if isinstance(row["ticket_id"], str):
        row["ticket_id"] = uuid.UUID(row["ticket_id"])
    async with asyncio.timeout(AI_RUN_INSERT_TIMEOUT_SECONDS):
        async with SessionLocal() as session:
            session.add(AiRun(**row))
            await session.commit()


async def no_op_logger(record: AiRunRecord) -> None:
    """For tests and scripts that shouldn't touch the database."""