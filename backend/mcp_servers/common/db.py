"""The async connection pool every MCP server shares (ARCHITECTURE.md §5)."""

import asyncio
import json
import logging
from urllib.parse import urlsplit

import asyncpg

from mcp_servers.common.results import fail
from mcp_servers.common.settings import get_settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


def dsn(database_url: str) -> str:
    # asyncpg wants a plain postgresql:// URL, not SQLAlchemy's dialect form.
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb columns come back as Python objects, not strings.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    """The shared pool, opened on first use. An unreachable database is a `database_unavailable` tool error."""
    global _pool
    async with _lock:
        if _pool is None:
            url = dsn(get_settings().database_url)
            # The Supabase Transaction pooler (port 6543) does not support prepared statements (§8).
            kwargs = {"statement_cache_size": 0} if urlsplit(url).port == 6543 else {}
            try:
                _pool = await asyncpg.create_pool(url, min_size=1, max_size=5, init=_init_connection, **kwargs)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                log.warning("database unreachable at %s: %s", urlsplit(url).hostname, type(exc).__name__)
                raise fail("database_unavailable", "the database is unreachable; try again shortly") from exc
    return _pool


async def close_pool() -> None:
    global _pool
    async with _lock:
        if _pool is not None:
            await _pool.close()
            _pool = None
