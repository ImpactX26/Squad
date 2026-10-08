"""Natural-language ticket search (ARCHITECTURE.md §7.4, §10): POST /api/search.

Agents and admins. The brain (app/brain/search.py) turns the words into filters and runs
tickets.search_tickets; this returns the results as the inbox's ticket cards, each with its
one-line "why this matched".
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tickets import load_items
from app.brain import search as brain_search
from app.brain.mcp_hub import McpToolError
from app.core.db import get_session
from app.core.security import require_roles
from app.models import StaffUser
from app.schemas.search import SearchInterpretation, SearchRequest, SearchResponse, SearchResult

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search", response_model=SearchResponse, responses={503: {"description": "Tickets server down"}})
async def search_tickets(
    body: SearchRequest,
    staff: Annotated[StaffUser, Depends(require_roles("agent", "admin"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SearchResponse:
    """Search tickets in plain words: one MODEL_FAST call for the filters, none for a ticket number."""
    # The login check left a transaction open; don't hold it across the model and tool calls.
    await session.rollback()
    try:
        outcome = await brain_search.search(body.query)
    except McpToolError as e:
        log.error("search unavailable: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Search is unavailable: the tickets server isn't reachable.") from e
    items = await load_items(session, [str(row["ticket_id"]) for row in outcome.results])
    plan = outcome.plan
    return SearchResponse(
        query=body.query,
        interpretation=SearchInterpretation(filters=plan.filters, text=plan.text, summary=plan.summary, ai=plan.ai),
        results=[SearchResult(ticket=items[str(row["ticket_id"])], why=row["why"],
                              matched_by=list(row.get("matched_by") or []))
                 for row in outcome.results if str(row["ticket_id"]) in items],
        ranking=outcome.ranking,  # type: ignore[arg-type]
        notice=plan.notice,
    )