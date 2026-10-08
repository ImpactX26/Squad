import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.core.config import get_settings
from app.core.events import Event, bus
from app.schemas.events import InternalEventIn

router = APIRouter(prefix="/internal", tags=["internal"])


def require_internal_key(x_internal_key: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().internal_api_key
    # Fail closed: an unset INTERNAL_API_KEY rejects every call.
    if not expected or x_internal_key is None or not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal key")


@router.post("/events", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_internal_key)])
async def post_event(body: InternalEventIn) -> Event:
    """MCP servers report events here (§9); they're pushed to /ws/staff clients."""
    return await bus.publish(body.type, body.data)