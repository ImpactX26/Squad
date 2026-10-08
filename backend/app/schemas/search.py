"""Request and response models for natural-language search (ARCHITECTURE.md §7.4, §10)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.tickets import TicketListItem


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300,
                       description='Plain words, e.g. "open battery tickets from telegram this week"')


class SearchInterpretation(BaseModel):
    """How the query was read, so the agent can see what was searched."""

    filters: dict[str, Any] = Field(description="The filters tickets.search_tickets ran with, clamped in code")
    text: str = Field(description="The words matched by full text and meaning")
    summary: str = Field(description='One line, e.g. "open, battery, from Telegram; matching “battery”"')
    ai: bool = Field(description="Whether a model turned the words into filters")


class SearchResult(BaseModel):
    ticket: TicketListItem
    why: str = Field(description="One line: why this ticket matched (§7.4)")
    matched_by: list[str] = Field(description='"full-text" and/or "similarity"')


class SearchResponse(BaseModel):
    query: str
    interpretation: SearchInterpretation
    results: list[SearchResult]
    ranking: Literal["rrf", "recency", "ticket_number", "serial"]
    notice: str | None = Field(description="Set when the search fell back, e.g. no model reachable")