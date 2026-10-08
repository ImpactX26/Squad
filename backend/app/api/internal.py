"""POST /internal/events: events from the MCP servers onto the bus (ARCHITECTURE.md §9).

The MCP servers are separate processes; they POST {type, data} with the shared secret in
X-Internal-Key. Caddy answers 404 for /internal/* (§17.2), so only processes on the server reach it.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.events import bus
from app.schemas.events import InternalEventIn, StaffEvent

router = APIRouter()


def require_internal_key(
    x_internal_key: str | None = Header(default=None),
    settings: Settings = Depends(get_app_settings),
) -> None:
    # Runs before the body is read, so a caller without the key learns nothing about it.
    if not settings.internal_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="INTERNAL_API_KEY is empty in backend/.env.")
    if x_internal_key is None or not hmac.compare_digest(x_internal_key.encode(), settings.internal_api_key.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Wrong or missing X-Internal-Key.")


@router.post(
    "/internal/events",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StaffEvent,
    dependencies=[Depends(require_internal_key)],
    responses={401: {"description": "Wrong or missing X-Internal-Key"}, 503: {"description": "INTERNAL_API_KEY is not set"}},
)
async def internal_event(body: InternalEventIn) -> StaffEvent:
    """Publish an MCP server's event: handlers run and every /ws/staff connection gets it."""
    return bus.publish(body.type, body.data)
