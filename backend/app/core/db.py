"""Async SQLAlchemy engine and sessions (ARCHITECTURE.md §8)."""

from collections.abc import AsyncIterator
from functools import lru_cache

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

# What a lost or refused database connection can raise: SQLAlchemy wraps most errors,
# but asyncpg's connect can surface OSError (refused, DNS, timeout) as is.
DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, asyncpg.PostgresError, OSError)


def make_engine(database_url: str) -> AsyncEngine:
    connect_args = {}
    # The Supabase Transaction pooler (port 6543) does not support prepared statements (§8, §18.1).
    if make_url(database_url).port == 6543:
        connect_args["statement_cache_size"] = 0
    return create_async_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_engine() -> AsyncEngine:
    return make_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with get_sessionmaker()() as session:
        yield session
