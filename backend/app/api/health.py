import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.schemas.health import HealthOut

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(response: Response, session: Annotated[AsyncSession, Depends(get_session)]) -> HealthOut:
    try:
        async with asyncio.timeout(5):
            await session.execute(text("SELECT 1"))
    except Exception:
        log.exception("health check: database unavailable")  # details stay in the server log
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthOut(status="degraded", db="unavailable")
    return HealthOut(status="ok", db="ok")