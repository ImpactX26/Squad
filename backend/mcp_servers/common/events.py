"""Report events to the backend: POST {type, data} to BACKEND_URL/internal/events (ARCHITECTURE.md §9).

The MCP servers are separate processes, so this is how their events reach the event bus.
A failed POST is logged and swallowed: the tool call that emitted the event still succeeds.
"""

import logging
from typing import Any

import httpx

from mcp_servers.common.results import jsonable
from mcp_servers.common.settings import get_settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(3.0, connect=1.0)


async def publish(event_type: str, data: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> bool:
    """Return True when the backend accepted the event, False (with a warning) otherwise."""
    settings = get_settings()
    url = settings.backend_url.rstrip("/") + "/internal/events"
    body = {"type": event_type, "data": jsonable(data)}
    headers = {"X-Internal-Key": settings.internal_api_key}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own_client:
                response = await own_client.post(url, json=body, headers=headers)
        else:
            response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("event %s not delivered to %s: %s", event_type, url, exc.__class__.__name__)
        return False
    return True
