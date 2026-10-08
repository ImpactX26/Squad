"""FastAPI app. Run from backend/: uv run uvicorn app.main:app --port 8000."""

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, health
from app.core.config import Settings, get_settings
from app.core.db import DB_ERRORS

log = logging.getLogger(__name__)


async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    # Registered as an exception handler, so the response passes back through CORSMiddleware
    # and keeps its CORS headers; an unhandled 500 would skip them (§18.2).
    log.error("database error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "The database is unavailable. Try again shortly."})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name)
    app.state.settings = settings  # read by routes through app.api.deps.get_app_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for error in DB_ERRORS:
        app.add_exception_handler(error, database_unavailable)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app


app = create_app()
