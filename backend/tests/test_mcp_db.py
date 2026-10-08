"""The MCP servers' shared pool (mcp_servers/common/db.py): connection failures are readable tool errors."""

import asyncio
import json
import uuid

import asyncpg
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.common import db
from mcp_servers.common import settings as mcp_settings
from mcp_servers.common.results import fail
from mcp_servers.messaging_server import mcp as messaging
from mcp_servers.tickets_server import mcp as tickets
from tests.mcp_support import error_json, pool  # noqa: F401 (fixture)


async def test_the_pool_opens_nothing_up_front_and_holds_at_most_two(monkeypatch):
    # Nothing listens on port 1: making the pool must not need a connection.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    mcp_settings.get_settings.cache_clear()
    await db.close_pool()
    try:
        pool = await db.get_pool()
        assert (pool.get_min_size(), pool.get_max_size(), pool.get_size()) == (0, 2, 0)
        assert pool._max_inactive_connection_lifetime == 30
    finally:
        await db.close_pool()
        mcp_settings.get_settings.cache_clear()


async def test_a_third_call_waits_and_idle_connections_are_closed(monkeypatch):
    monkeypatch.setattr(db, "IDLE_SECONDS", 0.5)  # 30 s in the servers
    await db.close_pool()
    try:
        pool = await db.get_pool()
        try:
            async with db.connection():
                pass
        except ToolError as exc:
            pytest.skip(f"database unreachable ({type(exc.__cause__).__name__})")
        async def third_call():
            async with db.connection() as conn:
                return await conn.fetchval("SELECT 3")

        async with db.connection() as first, db.connection() as second:
            assert pool.get_size() == 2
            third = asyncio.create_task(third_call())
            await asyncio.sleep(1)
            assert not third.done()  # no third connection is opened
            assert [await first.fetchval("SELECT 1"), await second.fetchval("SELECT 2")] == [1, 2]
        assert await asyncio.wait_for(third, 10) == 3  # it ran on one given back
        assert pool.get_size() == 2
        await asyncio.sleep(1.5)
        assert pool.get_size() == 0  # idle ones were closed: the pooler has its slots back
    finally:
        await db.close_pool()


async def full_pooler(*args, **kwargs):
    raise asyncpg.InternalServerError("(EMAXCONNSESSION) max clients reached in session mode")


@pytest.mark.parametrize(("server", "tool", "args"), [
    (tickets, "get_ticket", {"ticket_number": "SR-2026-00001"}),
    (messaging, "send_reply", {"conversation_id": str(uuid.uuid4()), "text": "Hello"}),
])
async def test_a_connection_refused_inside_an_open_pool_is_database_unavailable(server, tool, args):
    await db.close_pool()
    # An open pool whose next connection the Supabase pooler turns away, as when it is full.
    db._pool = await asyncpg.create_pool("postgresql://nobody@127.0.0.1:1/none", min_size=0, max_size=1,
                                         connect=full_pooler)
    try:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
    finally:
        await db.close_pool()
    assert error_json(result) == {"error": "database_unavailable",
                                  "message": "the database is unreachable; try again shortly"}


async def test_a_connection_lost_during_a_call_is_database_unavailable(pool):  # noqa: F811
    with pytest.raises(ToolError) as caught:
        async with db.connection() as conn, conn.transaction():
            await conn.execute("SELECT 1")
            conn.terminate()  # the pooler dropping it mid-call
            await conn.execute("SELECT 1")
    assert json.loads(str(caught.value))["error"] == "database_unavailable"
    async with db.connection() as conn:  # the pool replaces the lost connection
        assert await conn.fetchval("SELECT 1") == 1


async def test_a_tools_own_errors_are_not_database_unavailable(pool):  # noqa: F811
    with pytest.raises(asyncpg.PostgresSyntaxError):
        async with db.connection() as conn:
            await conn.execute("SELEC 1")
    with pytest.raises(ToolError, match="not_found"):
        async with db.connection():
            raise fail("not_found", "no such ticket")
