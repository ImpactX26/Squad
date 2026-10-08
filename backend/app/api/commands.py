"""Slash commands from the ticket page (ARCHITECTURE.md §7.5, §10).

`POST /api/tickets/{id}/commands {name, args}` runs a built-in, or one of the signed-in agent's own
custom commands, and streams its progress as server-sent events:

    event: step   data: {"type":"step","step":"price","status":"ok","detail":"₹6,199.00 (...)"}
    event: done   data: {"type":"done","outcome":"awaiting_payment_details","message":"...",...}
    event: error  data: {"type":"error","message":"..."}

Staff only (agents and admins, §7.5). The command runs as its own task and the response reads its
progress from a queue, so a browser that closes the stream mid-way can't leave a command half done.
A name that is neither a built-in nor one of the agent's commands is a 404.

`GET /api/commands` lists the built-ins and the agent's own custom commands (the composer's `/`
menu), with the tools and template variables a custom command may use. `POST /api/commands`,
`PATCH` and `DELETE /api/commands/{id}` manage the agent's own: a template may use only the listed
variables, and allowed_tools only tools the MCP servers offer that a model may be given (never one
of MODEL_FORBIDDEN_TOOLS). Each agent sees and changes only their own (§7.5: per agent).
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain import commands, suggestions
from app.brain.mcp_hub import hub
from app.brain.router import MODEL_FORBIDDEN_TOOLS, ROLE_SERVERS, first_sentence
from app.core.db import get_session
from app.core.security import require_roles
from app.models import StaffUser
from app.schemas.commands import (
    CommandCreate,
    CommandListResponse,
    CommandOut,
    CommandPatch,
    CommandToolOut,
    SuggestionChip,
    SuggestionsResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["commands"])

# Commands still running after their stream closed: kept referenced until they finish.
_running: set[asyncio.Task[None]] = set()

StaffOnly = Annotated[StaffUser, Depends(require_roles("agent", "admin"))]


@dataclass(frozen=True)
class StaffRef:
    """The staff member running the command, detached from the request's database session."""

    id: uuid.UUID
    name: str
    role: str


class CommandRequest(BaseModel):
    name: str = Field(min_length=1, max_length=41, description='"payments" or "/payments"')
    args: str = Field(default="", max_length=500, description='The text after the command, e.g. "battery replacement"')


@router.post("/tickets/{ticket_id}/commands", response_class=StreamingResponse)
async def run_ticket_command(
    ticket_id: uuid.UUID,
    body: CommandRequest,
    staff: StaffOnly,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """Run a slash command on a ticket; the response is an SSE stream of its progress (§7.5)."""
    name = commands.normalize_name(body.name)
    exists = (await session.execute(text("SELECT 1 FROM tickets WHERE id = :id"), {"id": ticket_id})).scalar()
    custom = None if name in commands.BUILTINS else (await session.execute(text(
        "SELECT 1 FROM slash_commands WHERE name = :n AND owner_id = :o AND NOT is_builtin"),
        {"n": name, "o": staff.id})).scalar()
    # Copied out first: the rollback below expires the ORM object, and reading it afterwards would
    # be lazy IO outside the request's session.
    staff_ref = StaffRef(id=staff.id, name=staff.name, role=staff.role)
    # The login check left a transaction open on this session; don't hold it for the whole stream.
    await session.rollback()
    if name not in commands.BUILTINS and not custom:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No command named /{name[:40]}. Built in: "
                                   + ", ".join(f"/{c}" for c in commands.BUILTINS) + ".")
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run() -> None:
        try:
            result = await commands.run_command(ticket_id, name, body.args, staff_ref, progress=queue.put)
            await queue.put({"type": "done", "outcome": result.outcome, "message": result.message, **result.data})
        except commands.CommandError as e:
            await queue.put({"type": "error", "message": str(e)})
        except Exception as e:  # a tool or the database failed; the agent sees it, the log has the trace
            log.exception("/%s on ticket %s failed", name, ticket_id)
            await queue.put({"type": "error", "message": f"/{name} failed: {type(e).__name__}: {e}"[:300]})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run(), name=f"command-{name}-{ticket_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)

    async def stream():
        while (event := await queue.get()) is not None:
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- the command list (§7.5, §10) ----------

_CUSTOM = text("""
SELECT id, name, description, prompt_template, allowed_tools, created_at
FROM slash_commands WHERE owner_id = :owner AND NOT is_builtin ORDER BY name
""")


def builtin_out(spec: commands.Builtin) -> CommandOut:
    return CommandOut(id=None, name=spec.name, usage=spec.usage, description=spec.description, args=spec.args,
                      example=spec.example or None, is_builtin=True, prompt_template=None,
                      allowed_tools=sorted(spec.tools), created_at=None)


def custom_out(row: Any) -> CommandOut:
    return CommandOut(id=row["id"], name=row["name"], usage=f"/{row['name']} [words]",
                      description=row["description"], args="optional", example=None, is_builtin=False,
                      prompt_template=row["prompt_template"], allowed_tools=list(row["allowed_tools"] or []),
                      created_at=row["created_at"])


def offered_tools() -> tuple[list[CommandToolOut], list[str]]:
    """What a custom command may list: the copilot's tools the hub has registered, minus
    MODEL_FORBIDDEN_TOOLS; and the copilot servers whose tools aren't registered right now."""
    tools = [
        CommandToolOut(name=t["function"]["name"], server=t["function"]["name"].split("__", 1)[0],
                       description=first_sentence(t["function"].get("description") or ""))
        for t in hub.tools(ROLE_SERVERS[commands.ROLE]) if t["function"]["name"] not in MODEL_FORBIDDEN_TOOLS
    ]
    listed = {t.server for t in tools}
    return sorted(tools, key=lambda t: t.name), sorted(ROLE_SERVERS[commands.ROLE] - listed)


@router.get("/commands", response_model=CommandListResponse)
async def list_commands(staff: StaffOnly, session: Annotated[AsyncSession, Depends(get_session)]) -> CommandListResponse:
    """The built-ins, then the signed-in agent's own custom commands (§7.5: per agent)."""
    rows = (await session.execute(_CUSTOM, {"owner": staff.id})).mappings().all()
    tools, unreachable = offered_tools()
    return CommandListResponse(
        commands=[*(builtin_out(spec) for spec in commands.BUILTINS.values()), *(custom_out(r) for r in rows)],
        tools=tools, unreachable_servers=unreachable, variables=commands.TEMPLATE_VARIABLES)


# ---------- the agent's own commands: create, edit, delete (§7.5) ----------


def known_tools() -> set[str]:
    """Every tool the hub has registered from the MCP servers (tests replace this)."""
    return set(hub.tool_names())


def _check(name: str, template: str, tools: list[str]) -> None:
    """Refuse, with every reason at once, a command the runner would have to second-guess."""
    if name in commands.BUILTINS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"/{name} is a built-in command.")
    problems = []
    if unknown := commands.unknown_variables(template):
        problems.append("unknown template variables: " + ", ".join("{{" + v + "}}" for v in unknown)
                        + ". You can use " + ", ".join("{{" + v + "}}" for v in commands.TEMPLATE_VARIABLES))
    problems += commands.check_allowed_tools(tools, known_tools())
    if problems:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems) + ".")


_ONE = text("""
SELECT id, name, description, prompt_template, allowed_tools, created_at
FROM slash_commands WHERE id = :id AND owner_id = :owner AND NOT is_builtin
""")


async def _save(session: AsyncSession, sql: str, params: dict[str, Any], name: str) -> Any:
    try:
        row = (await session.execute(text(sql), params)).mappings().one()
        await session.commit()
    except IntegrityError as e:  # UNIQUE (owner_id, name)
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"You already have a /{name} command.") from e
    return row


@router.post("/commands", response_model=CommandOut, status_code=status.HTTP_201_CREATED)
async def create_command(body: CommandCreate, staff: StaffOnly,
                         session: Annotated[AsyncSession, Depends(get_session)]) -> CommandOut:
    """A new custom command, owned by the signed-in agent."""
    tools = sorted(set(body.allowed_tools))
    _check(body.name, body.prompt_template, tools)
    row = await _save(session, "INSERT INTO slash_commands (owner_id, name, description, prompt_template,"
                               " allowed_tools, is_builtin) VALUES (:owner, :name, :description, :template,"
                               " CAST(:tools AS text[]), FALSE)"
                               " RETURNING id, name, description, prompt_template, allowed_tools, created_at",
                      {"owner": staff.id, "name": body.name, "description": body.description.strip(),
                       "template": body.prompt_template, "tools": tools}, body.name)
    return custom_out(row)


@router.patch("/commands/{command_id}", response_model=CommandOut)
async def edit_command(command_id: uuid.UUID, body: CommandPatch, staff: StaffOnly,
                       session: Annotated[AsyncSession, Depends(get_session)]) -> CommandOut:
    """Change one of the signed-in agent's own commands. Another agent's is a 404."""
    current = (await session.execute(_ONE, {"id": command_id, "owner": staff.id})).mappings().one_or_none()
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such command of yours.")
    name = body.name or current["name"]
    template = body.prompt_template if body.prompt_template is not None else current["prompt_template"]
    tools = sorted(set(body.allowed_tools if body.allowed_tools is not None else current["allowed_tools"] or []))
    _check(name, template, tools)
    row = await _save(session, "UPDATE slash_commands SET name = :name, description = :description,"
                               " prompt_template = :template, allowed_tools = CAST(:tools AS text[])"
                               " WHERE id = :id AND owner_id = :owner"
                               " RETURNING id, name, description, prompt_template, allowed_tools, created_at",
                      {"id": command_id, "owner": staff.id, "name": name, "template": template, "tools": tools,
                       "description": (body.description or current["description"]).strip()}, name)
    return custom_out(row)


@router.delete("/commands/{command_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_command(command_id: uuid.UUID, staff: StaffOnly,
                         session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """Delete one of the signed-in agent's own commands. Built-ins can't be deleted."""
    deleted = (await session.execute(text(
        "DELETE FROM slash_commands WHERE id = :id AND owner_id = :owner AND NOT is_builtin RETURNING id"),
        {"id": command_id, "owner": staff.id})).scalar()
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such command of yours.")
    await session.commit()


# ---------- suggested chips (§7.5) ----------

_FACTS = text("""
SELECT t.id, t.ticket_number, t.title, t.status, t.priority, t.issue_type, t.category, t.ai_summary,
       t.product_id IS NOT NULL AS has_product,
       coalesce(p.warranty_until >= current_date, FALSE) AS in_warranty,
       EXISTS (SELECT 1 FROM payments pay WHERE pay.ticket_id = t.id
               AND (pay.status = 'verifying' OR (pay.status = 'pending' AND pay.expires_at > now()))) AS open_payment,
       EXISTS (SELECT 1 FROM service_jobs j WHERE j.ticket_id = t.id
               AND j.status NOT IN ('completed', 'cancelled')) AS open_job,
       (SELECT count(*) FROM diagnostic_steps d WHERE d.ticket_id = t.id AND d.result = 'pending') AS pending_steps,
       (SELECT array_agg(d.step || ' (' || d.result || ')' ORDER BY d.position) FROM diagnostic_steps d
        WHERE d.ticket_id = t.id AND d.result <> 'pending') AS tried,
       (SELECT m.body FROM messages m WHERE m.ticket_id = t.id AND m.sender_type = 'customer'
        ORDER BY m.created_at DESC LIMIT 1) AS last_customer_message,
       (SELECT m.id::text FROM messages m WHERE m.ticket_id = t.id ORDER BY m.created_at DESC LIMIT 1) AS last_message
FROM tickets t LEFT JOIN products p ON p.id = t.product_id
WHERE t.id = :id
""")


@router.get("/tickets/{ticket_id}/suggestions", response_model=SuggestionsResponse)
async def ticket_suggestions(ticket_id: uuid.UUID, staff: StaffOnly,
                             session: Annotated[AsyncSession, Depends(get_session)]) -> SuggestionsResponse:
    """3-4 suggested commands for this ticket (§7.5): one cached decide call ranks what code offers."""
    row = (await session.execute(_FACTS, {"id": ticket_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    custom = [dict(r) for r in (await session.execute(_CUSTOM, {"owner": staff.id})).mappings().all()]
    staff_id = str(staff.id)
    await session.rollback()  # don't hold the transaction across the model call
    facts = suggestions.TicketFacts(
        ticket_id=str(row["id"]), ticket_number=row["ticket_number"], title=row["title"], status=row["status"],
        priority=row["priority"], issue_type=row["issue_type"], category=row["category"],
        ai_summary=row["ai_summary"], has_product=row["has_product"], in_warranty=row["in_warranty"],
        open_payment=row["open_payment"], open_job=row["open_job"], pending_steps=int(row["pending_steps"]),
        tried=list(row["tried"] or []), last_customer_message=row["last_customer_message"],
        version=":".join(str(row[k]) for k in ("last_message", "status", "priority", "open_payment", "open_job",
                                                "pending_steps", "has_product")),
    )
    result = await suggestions.suggest(facts, custom, staff_id=staff_id)
    return SuggestionsResponse(
        ticket_id=ticket_id, source=result.source, cached=result.cached,  # type: ignore[arg-type]
        chips=[SuggestionChip(name=c.name, args=c.args, label=c.label, needs_args=c.needs_args) for c in result.chips])