"""The async connection pool every MCP server shares (ARCHITECTURE.md §5)."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

import asyncpg
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.common.results import fail
from mcp_servers.common.settings import get_settings

log = logging.getLogger(__name__)

# What a refused or exhausted connection raises: refused or timed out (OSError), or turned away by
# the server or the Supabase pooler (a PostgresError such as EMAXCONNSESSION).
UNREACHABLE = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)
# Inside a tool, only a lost connection is the database's fault; other errors are the tool's own.
# (A transaction's rollback on a dropped connection raises InterfaceError: the connection is closed.)
LOST = (OSError, asyncpg.PostgresConnectionError)

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
            except UNREACHABLE as exc:
                raise _unavailable(exc) from exc
    return _pool


def _unavailable(exc: BaseException) -> ToolError:
    host = urlsplit(dsn(get_settings().database_url)).hostname
    log.warning("database unreachable at %s: %s", host, type(exc).__name__)
    return fail("database_unavailable", "the database is unreachable; try again shortly")


def _closed(conn: asyncpg.Connection) -> bool:
    try:
        return conn.is_closed()
    except asyncpg.InterfaceError:  # the pool already took back a connection that was terminated
        return True


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    """A pooled connection for one tool call.

    A connection the pool can't open (the pooler full, the network down) or one lost during the
    call is a `database_unavailable` tool error, never a bare "Error executing tool".
    """
    pool = await get_pool()
    try:
        conn = await pool.acquire()
    except UNREACHABLE as exc:
        raise _unavailable(exc) from exc
    try:
        yield conn
    except ToolError:
        raise
    except Exception as exc:
        if isinstance(exc, LOST) or _closed(conn):
            raise _unavailable(exc) from exc
        raise
    finally:
        with suppress(*UNREACHABLE):  # a broken connection is dropped by the pool, not given back
            await pool.release(conn)


async def close_pool() -> None:
    global _pool
    async with _lock:
        if _pool is not None:
            await _pool.close()
            _pool = None
