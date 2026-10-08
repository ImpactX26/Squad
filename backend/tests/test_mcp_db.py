"""The MCP servers' shared pool (mcp_servers/common/db.py): connection failures are readable tool errors."""

import json
import uuid

import asyncpg
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.common import db
from mcp_servers.common.results import fail
from mcp_servers.messaging_server import mcp as messaging
from mcp_servers.tickets_server import mcp as tickets
from tests.mcp_support import error_json, pool  # noqa: F401 (fixture)


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
