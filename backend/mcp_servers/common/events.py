"""MCP servers report events to the backend over POST /internal/events (ARCHITECTURE.md §9).

The servers are separate processes, so they can't use the in-process bus in app.core.events.
A failed post is logged and swallowed: the backend being down must never fail an MCP tool,
because the database write the tool just made is the thing that matters.
"""

import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.events import EventType

log = logging.getLogger(__name__)

POST_TIMEOUT_SECONDS = 3.0

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish(type: EventType, data: dict[str, Any]) -> bool:
    """POST one §9 event to the backend. Returns whether it was accepted; never raises."""
    settings = get_settings()
    url = settings.backend_url.rstrip("/") + "/internal/events"
    try:
        response = await _http().post(
            url,
            json={"type": type, "data": data},
            headers={"X-Internal-Key": settings.internal_api_key},
        )
    except Exception as e:
        log.warning("event %s not delivered to %s: %s: %s", type, url, type_name(e), e)
        return False
    if response.status_code >= 400:
        # Never log the response body: a 401 here means the key is wrong, and bodies can echo headers.
        log.warning("event %s rejected by %s: HTTP %d", type, url, response.status_code)
        return False
    return True


def type_name(e: BaseException) -> str:
    return type(e).__name__