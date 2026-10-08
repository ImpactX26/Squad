"""The dashboard copilot (ARCHITECTURE.md §4.3, §10): POST /api/copilot, streamed as server-sent events.

    router.pick_servers(text, "copilot") -> filter_tools + compact_tools -> runtime.run_tool_loop
    -> the loop's answer; llm.stream_text only when the loop ended without one (tool-call limit)

    event: servers  data: {"type":"servers","servers":["tickets","catalog"]}
    event: tool     data: {"type":"tool","tool":"tickets__search_tickets","ok":true,"ms":412}
    event: delta    data: {"type":"delta","text":"..."}
    event: done     data: {"type":"done","model":"groq:openai/gpt-oss-20b","tool_calls":[...],"hit_limit":false}
    event: error    data: {"type":"error","message":"..."}

Agents and admins. The tools are the copilot role's, narrowed by the router; filter_tools and
run_tool_loop drop router.MODEL_FORBIDDEN_TOOLS, so the copilot can never mark a payment paid, take
a UTR, move stock or create or move a job (§4.6). The conversation lives in the page: the client
sends its recent turns with each question, and nothing is stored but the ai_runs row.
"""

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.brain import router as brain_router
from app.brain import runtime
from app.brain.llm import LLMUnavailable, default_llm
from app.brain.mcp_hub import hub
from app.brain.prompts import load as load_prompt
from app.core.events import Event, bus
from app.core.security import require_roles
from app.models import StaffUser

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["copilot"])

ROLE = "copilot"
TRIGGER = "copilot"
MAX_HISTORY_TURNS = 8
MAX_TURN_CHARS = 2000

# The copilot's §15 fallback replies: a staff member is never left with an empty answer.
NO_MODEL_ANSWER = ("I can't reach a model right now, so I have no answer yet. Nothing was changed. "
                   "Try again in a minute; meanwhile the inbox filters and ⌘K search work without me.")
EMPTY_ANSWER = ("I looked this up but couldn't put an answer together. Nothing was changed. "
                "Try asking more specifically, e.g. name a ticket (SR-…), a part SKU or a part type.")

_running: set[asyncio.Task[None]] = set()


class CopilotTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class CopilotRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[CopilotTurn] = Field(default_factory=list, max_length=40,
                                       description="Earlier turns of this chat, oldest first; the last 8 are used")
    ticket_id: uuid.UUID | None = Field(default=None, description="The ticket the question is about, if any")


class _StreamedBus:
    """The event bus, plus each tool call as a `tool` event of this response's stream."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._queue = queue

    async def publish(self, type: Any, data: dict[str, Any]) -> Event:
        event = await bus.publish(type, data)
        if type == "agent.tool_called":
            await self._queue.put({"type": "tool", "tool": data.get("tool"), "ok": data.get("ok"), "ms": data.get("ms")})
        return event


def conversation(body: CopilotRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": load_prompt("copilot")}]
    if body.ticket_id:
        messages.append({"role": "system", "content": f"The staff member is looking at ticket id {body.ticket_id}."})
    for turn in body.history[-MAX_HISTORY_TURNS:]:
        messages.append({"role": turn.role, "content": turn.content[:MAX_TURN_CHARS]})
    messages.append({"role": "user", "content": body.message})
    return messages


async def answer(body: CopilotRequest, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
    """Run one question through the copilot role's tool loop, putting SSE events on `queue`."""
    servers = await brain_router.pick_servers(body.message, ROLE)
    if body.ticket_id and "tickets" not in servers:
        servers = ["tickets", *servers]
    # A stock question often names a device or model, whose parts the catalog lists.
    if "inventory" in servers and "catalog" not in servers:
        servers = [*servers, "catalog"]
    tools = brain_router.compact_tools(brain_router.filter_tools(hub.tools(servers), servers))
    await queue.put({"type": "servers", "servers": servers})
    llm = default_llm()
    result = await runtime.run_tool_loop(ROLE, TRIGGER, conversation(body), tools, hub.call_tool,
                                         ticket_id=body.ticket_id, llm=llm,
                                         events=_StreamedBus(queue))  # type: ignore[arg-type]
    said = False
    if result.text.strip():
        await queue.put({"type": "delta", "text": result.text.strip()})
        said = True
    else:
        # No answer (the tool-call limit, or an empty reply): one more call, no tools, streamed.
        closing = [*result.messages, {"role": "user", "content": "Answer me now from what the tools returned, briefly."}]
        async for delta in llm.stream_text(closing, tier="smart"):
            said = said or bool(delta.strip())
            await queue.put({"type": "delta", "text": delta})
    if not said:
        # Never silence (§15): the model came back empty twice.
        log.warning("copilot: empty answer after %d tool calls", len(result.tool_calls))
        await queue.put({"type": "delta", "text": EMPTY_ANSWER})
    await queue.put({"type": "done", "model": result.model, "tool_calls": result.tool_calls,
                     "hit_limit": result.hit_limit})


@router.post("/copilot", response_class=StreamingResponse)
async def copilot(
    body: CopilotRequest,
    staff: Annotated[StaffUser, Depends(require_roles("agent", "admin"))],
) -> StreamingResponse:
    """Ask the copilot; the answer and its tool calls stream back as server-sent events (§4.3 step 5)."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run() -> None:
        try:
            await answer(body, queue)
        except LLMUnavailable as e:
            log.warning("copilot: no model: %s", e)
            # The §15 fallback, then the error: the staff member always gets an answer, never silence.
            await queue.put({"type": "delta", "text": NO_MODEL_ANSWER})
            await queue.put({"type": "error", "message": "No model is reachable right now (rate limit or outage). "
                                                         "Try again in a minute."})
        except Exception as e:
            log.exception("copilot failed")
            await queue.put({"type": "error", "message": f"The copilot failed: {type(e).__name__}: {e}"[:300]})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run(), name="copilot")
    _running.add(task)
    task.add_done_callback(_running.discard)

    async def stream():
        while (event := await queue.get()) is not None:
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})