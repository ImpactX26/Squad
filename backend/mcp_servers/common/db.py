"""One asyncpg pool per MCP server process, built from DATABASE_URL (ARCHITECTURE.md §5).

The servers are separate processes from the FastAPI app, so they don't share app.core.db's
SQLAlchemy engine. They do reuse its connect args, so the Supabase Session pooler works here
too (asyncpg's prepared-statement cache has to be off against the pooler, §8).
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import asyncpg_connect_args

_pool: asyncpg.Pool | None = None


def dsn() -> str:
    """DATABASE_URL as a plain libpq DSN (asyncpg doesn't take SQLAlchemy's +asyncpg scheme)."""
    return make_url(get_settings().database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb in and out as Python objects, so tools don't hand-serialise every payload.
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    """The process-wide pool, created on first use."""
    global _pool
    if _pool is None:
        url = get_settings().database_url
        # Seven servers and the API share the Supabase Session pooler's 15 client slots, and a
        # client holds its slot while idle. So each server keeps no connection when idle, at most
        # two when busy (a third request waits here instead of failing at the pooler with
        # EMAXCONNSESSION), and gives an idle one back after a minute.
        _pool = await asyncpg.create_pool(
            dsn(),
            min_size=0,
            max_size=2,
            max_inactive_connection_lifetime=60,
            command_timeout=20,
            init=_init_connection,
            **asyncpg_connect_args(url),
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)


# ---------- helpers shared by the servers ----------


def row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return None if row is None else {k: _plain(v) for k, v in row.items()}


def rows_to_list(rows: Sequence[asyncpg.Record]) -> list[dict[str, Any]]:
    return [{k: _plain(v) for k, v in row.items()} for row in rows]


def _plain(value: Any) -> Any:
    """UUIDs, dates, and Decimals as JSON-friendly values; tools return compact JSON (§5)."""
    import datetime
    import decimal
    import uuid

    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def to_vector_literal(vector: Sequence[float]) -> str:
    """pgvector input as a string literal, cast with ::vector in SQL.

    Simpler than registering a type codec, and it keeps working with the pooler's
    prepared-statement cache disabled.
    """
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"