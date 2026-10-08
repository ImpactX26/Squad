"""GET /api/health: liveness + DB check, 503 when the DB is unreachable (ARCHITECTURE.md §10)."""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import DB_ERRORS, get_engine
from app.schemas.health import HealthOut

log = logging.getLogger(__name__)
router = APIRouter()

DB_CHECK_TIMEOUT_SECONDS = 5


async def _ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get(
    "/api/health",
    response_model=HealthOut,
    responses={503: {"model": HealthOut, "description": "The database is unreachable"}},
)
async def health(engine: AsyncEngine = Depends(get_engine)) -> HealthOut | JSONResponse:
    try:
        await asyncio.wait_for(_ping(engine), timeout=DB_CHECK_TIMEOUT_SECONDS)
    except (*DB_ERRORS, TimeoutError) as exc:
        log.warning("health: database unreachable: %s", type(exc).__name__)
        body = HealthOut(status="error", db="unreachable")
        return JSONResponse(status_code=503, content=body.model_dump())
    return HealthOut(status="ok", db="ok")
