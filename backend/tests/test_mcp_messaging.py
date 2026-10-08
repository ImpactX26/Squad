"""Smoke tests for the messaging MCP server (:8104) over the MCP protocol in-process."""

import uuid

import pytest
from mcp import Client

from mcp_servers.messaging_server import mcp
from tests.mcp_support import error_json, pool, published, result_json, rows  # noqa: F401 (fixtures)


@pytest.fixture
async def ticket(rows, pool):
    """A ticket on the test customer, linked to the test telegram conversation."""
    async with pool.acquire() as conn:
        ticket_id = await conn.fetchval(
            """INSERT INTO tickets (customer_id, source_channel, title, description)
               VALUES ($1, 'telegram', 'Battery', 'Won''t charge') RETURNING id""",
            uuid.UUID(rows["customer_id"]),
        )
        await conn.execute("UPDATE conversations SET ticket_id = $1 WHERE id = $2",
                           ticket_id, uuid.UUID(rows["conversation_id"]))
    return str(ticket_id)


@pytest.fixture
async def staff(pool):
    """A test staff user, and every notification made for anyone during the test, removed afterwards."""
    made: list[str] = []
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            """INSERT INTO staff_users (name, email, password_hash, role)
               VALUES ('Test Warehouse', $1, 'x', 'warehouse') RETURNING id""",
            f"test-warehouse-{uuid.uuid4().hex[:8]}@example.com",
        )
    yield {"user_id": str(user_id), "made": made}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM notifications WHERE id = ANY($1::uuid[])", [uuid.UUID(n) for n in made])
        await conn.execute("DELETE FROM staff_users WHERE id = $1", user_id)


# ---------- send_reply ----------

async def test_send_reply_queues_the_reply_on_the_conversations_own_channel(rows, ticket, published, pool):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("send_reply", {
            "conversation_id": rows["conversation_id"], "text": "  Thanks, we're on it.  "}))
    assert out["channel"] == "telegram" and out["status"] == "queued" and out["ticket_id"] == ticket
    async with pool.acquire() as conn:
        message = await conn.fetchrow("SELECT sender_type, channel, body, ticket_id FROM messages WHERE id = $1",
                                      uuid.UUID(out["message_id"]))
        outbox = await conn.fetchrow("SELECT conversation_id, message_id, status, attempts, payload FROM outbox WHERE id = $1",
                                     uuid.UUID(out["outbox_id"]))
    assert (message["sender_type"], message["channel"], message["body"]) == ("ai", "telegram", "Thanks, we're on it.")
    assert str(message["ticket_id"]) == ticket
    assert str(outbox["conversation_id"]) == rows["conversation_id"]
    assert str(outbox["message_id"]) == out["message_id"]
    assert (outbox["status"], outbox["attempts"]) == ("pending", 0)
    assert outbox["payload"] == {"kind": "reply", "text": "Thanks, we're on it."}
    assert published == [("ticket.updated", {"ticket_id": uuid.UUID(ticket), "reason": "reply_queued",
                                             "message_id": uuid.UUID(out["message_id"]), "channel": "telegram"})]


async def test_send_reply_before_a_ticket_exists(rows, published):
    # Intake asks for the serial before there is a ticket: the reply still goes out, no ticket event.
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("send_reply", {
            "conversation_id": rows["conversation_id"], "text": "What's the serial number?"}))
    assert out["ticket_id"] is None and out["status"] == "queued"
    assert published == []


async def test_send_reply_refuses_an_unknown_conversation_or_empty_text(rows, published, pool):
    async with Client(mcp) as client:
        unknown = await client.call_tool("send_reply", {"conversation_id": str(uuid.uuid4()), "text": "Hi"})
        empty = await client.call_tool("send_reply", {"conversation_id": rows["conversation_id"], "text": "   "})
    assert error_json(unknown)["error"] == "not_found"
    assert error_json(empty)["error"] == "missing_text"
    async with pool.acquire() as conn:
        queued = await conn.fetchval("SELECT count(*) FROM outbox WHERE conversation_id = $1",
                                     uuid.UUID(rows["conversation_id"]))
    assert queued == 0 and published == []


# ---------- notify_staff ----------

async def test_notify_staff_one_user(staff, published, pool):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("notify_staff", {
            "user_id": staff["user_id"], "title": "Restock BAT-AX14", "body": "4 → 3 on hand",
            "link": "/inventory", "type": "stock_low"}))
    staff["made"].extend(n["notification_id"] for n in out["notifications"])
    assert out["notified"] == 1 and out["notifications"][0]["user_id"] == staff["user_id"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT type, title, body, link, read_at FROM notifications WHERE id = $1",
                                  uuid.UUID(out["notifications"][0]["notification_id"]))
    assert dict(row) == {"type": "stock_low", "title": "Restock BAT-AX14", "body": "4 → 3 on hand",
                         "link": "/inventory", "read_at": None}
    assert [e for e, _ in published] == ["notification.created"]
    assert published[0][1]["user_id"] == uuid.UUID(staff["user_id"])


async def test_notify_staff_by_role_reaches_everyone_with_it(staff, published, pool):
    async with pool.acquire() as conn:
        warehouse = await conn.fetchval("SELECT count(*) FROM staff_users WHERE role = 'warehouse'")
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("notify_staff", {"role": "warehouse", "title": "Stock check"}))
    staff["made"].extend(n["notification_id"] for n in out["notifications"])
    assert out["notified"] == warehouse >= 1
    assert staff["user_id"] in {n["user_id"] for n in out["notifications"]}
    assert len(published) == warehouse


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"title": "x"}, "bad_arguments"),
        ({"title": "x", "role": "admin", "user_id": "00000000-0000-0000-0000-000000000000"}, "bad_arguments"),
        ({"title": "x", "role": "customer"}, "bad_role"),
        ({"title": "x", "role": "admin", "type": "Stock Low!"}, "bad_type"),
        ({"title": "x", "role": "admin", "link": "javascript:alert(1)"}, "bad_link"),
        ({"title": "   ", "role": "admin"}, "missing_text"),
    ],
)
async def test_notify_staff_refusals(args, error, published):
    async with Client(mcp) as client:
        result = await client.call_tool("notify_staff", args)
    assert error_json(result)["error"] == error
    assert published == []


async def test_notify_staff_unknown_user(pool, published):
    async with Client(mcp) as client:
        result = await client.call_tool("notify_staff", {"user_id": str(uuid.uuid4()), "title": "x"})
    assert error_json(result)["error"] == "not_found"
