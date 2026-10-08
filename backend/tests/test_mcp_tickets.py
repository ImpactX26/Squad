"""Smoke tests for the tickets MCP server (:8101), one or more per tool, over the MCP protocol in-process."""

import uuid

import pytest
from mcp import Client

from mcp_servers.common import db
from mcp_servers.common import settings as mcp_settings
from mcp_servers.tickets_server import mcp
from tests.mcp_support import error_json, published, pool, result_json, rows  # noqa: F401 (fixtures)


def ticket_args(rows, **overrides):
    args = {
        "customer_id": rows["customer_id"], "product_id": rows["product_id"], "category": "hardware",
        "issue_type": "battery", "title": "Battery won't charge",
        "description": "My laptop stopped charging yesterday.", "source_channel": "telegram",
        "conversation_id": rows["conversation_id"],
    }
    return {**args, **overrides}


async def create(client, rows, **overrides) -> dict:
    return result_json(await client.call_tool("create_ticket", ticket_args(rows, **overrides)))


# ---------- create_ticket ----------

async def test_create_ticket(rows, published, pool):
    async with Client(mcp) as client:
        out = await create(client, rows, flags=["out_of_warranty"], priority="high")
    assert out["ticket_number"].startswith("SR-")
    assert out["status"] == "new" and out["priority"] == "high" and out["flags"] == ["out_of_warranty"]
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT embedding, customer_id FROM tickets WHERE id = $1", uuid.UUID(out["ticket_id"]))
        linked = await conn.fetchval("SELECT ticket_id FROM conversations WHERE id = $1",
                                     uuid.UUID(rows["conversation_id"]))
        event_types = [r["type"] for r in await conn.fetch(
            "SELECT type FROM ticket_events WHERE ticket_id = $1", uuid.UUID(out["ticket_id"]))]
    assert t["embedding"] is None  # embeddings come in Block 2
    assert str(linked) == out["ticket_id"]
    assert event_types == ["created"]
    assert published[0][0] == "ticket.created" and published[0][1]["ticket_number"] == out["ticket_number"]


async def test_create_ticket_attaches_the_conversations_earlier_messages(rows, published, pool):
    conversation = uuid.UUID(rows["conversation_id"])

    async def say(sender, body):
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO messages (conversation_id, sender_type, channel, body) "
                               "VALUES ($1, $2, 'telegram', $3)", conversation, sender, body)

    # Intake's run up to the ticket: the problem, the serial asked for, the serial (§7.1).
    await say("customer", "My laptop won't charge")
    await say("ai", "What's the serial number?")
    await say("customer", rows["serial"])
    async with Client(mcp) as client:
        first = await create(client, rows)
        await say("customer", "The screen flickers too")
        second = await create(client, rows, title="Screen flickers")
        unlinked = await create(client, rows, conversation_id=None)
    async with pool.acquire() as conn:
        on = {r["body"]: str(r["ticket_id"]) for r in await conn.fetch(
            "SELECT body, ticket_id FROM messages WHERE conversation_id = $1", conversation)}
    assert (first["messages_attached"], second["messages_attached"], unlinked["messages_attached"]) == (3, 1, 0)
    assert on == {"My laptop won't charge": first["ticket_id"], "What's the serial number?": first["ticket_id"],
                  rows["serial"]: first["ticket_id"], "The screen flickers too": second["ticket_id"]}


async def test_create_ticket_defaults_and_unverified_product(rows, published):
    async with Client(mcp) as client:
        out = await create(client, rows, product_id=None, conversation_id=None, flags=["unverified_product"])
    assert out["priority"] == "medium" and out["flags"] == ["unverified_product"]


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"flags": ["vip"]}, "bad_flags"),
        ({"priority": "critical"}, "bad_priority"),
        ({"category": "network"}, "bad_category"),
        ({"source_channel": "sms"}, "bad_source_channel"),
    ],
)
async def test_create_ticket_rejects_unknown_values_without_a_database(overrides, error, published):
    fake = {"customer_id": str(uuid.uuid4()), "product_id": None, "conversation_id": None}
    async with Client(mcp) as client:
        result = await client.call_tool("create_ticket", ticket_args(fake, **overrides))
    assert error_json(result)["error"] == error
    assert published == []


async def test_create_ticket_rejection_creates_nothing(rows, published, pool):
    async with Client(mcp) as client:
        bad_flag = await client.call_tool("create_ticket", ticket_args(rows, flags=["out_of_warranty", "vip"]))
        # A conversation of another customer is refused after the insert: the transaction rolls it back.
        wrong_conversation = await client.call_tool(
            "create_ticket", ticket_args(rows, customer_id=rows["other_customer_id"], product_id=None))
    assert error_json(bad_flag)["unknown"] == ["vip"]
    assert error_json(wrong_conversation)["field"] == "conversation_id"
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM tickets WHERE customer_id = ANY($1::uuid[])",
            [uuid.UUID(rows["customer_id"]), uuid.UUID(rows["other_customer_id"])],
        )
    assert count == 0
    assert published == []


async def test_create_ticket_unknown_customer(rows, published):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_ticket", ticket_args(rows, customer_id=str(uuid.uuid4()), product_id=None, conversation_id=None))
    assert error_json(result) == {"error": "not_found", "message": "no such customer", "field": "customer_id"}


async def test_unreachable_database_is_a_readable_error(monkeypatch, published):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    mcp_settings.get_settings.cache_clear()
    await db.close_pool()
    try:
        fake = {"customer_id": str(uuid.uuid4()), "product_id": None, "conversation_id": None}
        async with Client(mcp) as client:
            result = await client.call_tool("create_ticket", ticket_args(fake))
    finally:
        await db.close_pool()
        mcp_settings.get_settings.cache_clear()
    assert error_json(result)["error"] == "database_unavailable"
    assert published == []


async def test_create_ticket_succeeds_when_the_backend_is_down(rows, monkeypatch):
    # The real events.publish, against a port nothing listens on: a warning, and the tool still succeeds.
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:1")
    mcp_settings.get_settings.cache_clear()
    try:
        async with Client(mcp) as client:
            out = await create(client, rows, conversation_id=None)
    finally:
        mcp_settings.get_settings.cache_clear()
    assert out["ticket_number"].startswith("SR-")


# ---------- get_ticket ----------

async def test_get_ticket_by_id_and_number(rows, published):
    async with Client(mcp) as client:
        created = await create(client, rows)
        by_id = result_json(await client.call_tool("get_ticket", {"ticket_id": created["ticket_id"]}))
        by_number = result_json(await client.call_tool(
            "get_ticket", {"ticket_number": created["ticket_number"].lower()}))
    assert by_id == by_number
    assert by_id["ticket"]["ticket_number"] == created["ticket_number"]
    assert by_id["customer"]["customer_id"] == rows["customer_id"]
    assert by_id["product"]["serial_number"] == rows["serial"]
    assert [i["type"] for i in by_id["timeline"]] == ["created"]


async def test_get_ticket_needs_exactly_one_key():
    async with Client(mcp) as client:
        result = await client.call_tool("get_ticket", {})
    assert error_json(result)["error"] == "bad_arguments"


async def test_get_ticket_not_found(pool):
    async with Client(mcp) as client:
        result = await client.call_tool("get_ticket", {"ticket_number": "SR-1999-99999"})
    assert error_json(result)["error"] == "not_found"


# ---------- add_message ----------

async def test_add_message(rows, published):
    async with Client(mcp) as client:
        created = await create(client, rows)
        on_channel = result_json(await client.call_tool("add_message", {
            "ticket_id": created["ticket_id"], "conversation_id": rows["conversation_id"],
            "sender_type": "ai", "body": "Thanks, we're on it."}))
        internal = result_json(await client.call_tool("add_message", {
            "ticket_id": created["ticket_id"], "conversation_id": None, "sender_type": "agent",
            "body": "Battery swap likely.", "body_original": "bat swap prob"}))
        bad = await client.call_tool("add_message", {
            "ticket_id": created["ticket_id"], "conversation_id": None, "sender_type": "robot", "body": "x"})
        ticket = result_json(await client.call_tool("get_ticket", {"ticket_id": created["ticket_id"]}))
    assert on_channel["channel"] == "telegram" and internal["channel"] == "internal"
    assert error_json(bad)["error"] == "bad_sender_type"
    assert [i.get("body") for i in ticket["timeline"] if i["kind"] == "message"] == [
        "Thanks, we're on it.", "Battery swap likely."]
    assert [e for e, d in published].count("ticket.updated") == 2


# ---------- update_status ----------

async def test_update_status(rows, published, pool):
    async with Client(mcp) as client:
        created = await create(client, rows)
        changed = result_json(await client.call_tool("update_status", {
            "ticket_id": created["ticket_id"], "status": "resolved", "note": "Battery replaced"}))
        same = result_json(await client.call_tool("update_status", {
            "ticket_id": created["ticket_id"], "status": "resolved"}))
        bad = await client.call_tool("update_status", {"ticket_id": created["ticket_id"], "status": "open"})
    assert changed == {"ticket_number": created["ticket_number"], "status": "resolved", "previous": "new",
                       "changed": True}
    assert same["changed"] is False
    assert error_json(bad)["error"] == "bad_status"
    async with pool.acquire() as conn:
        resolved_at = await conn.fetchval("SELECT resolved_at FROM tickets WHERE id = $1",
                                          uuid.UUID(created["ticket_id"]))
        payload = await conn.fetchval(
            "SELECT payload FROM ticket_events WHERE ticket_id = $1 AND type = 'status_changed'",
            uuid.UUID(created["ticket_id"]))
    assert resolved_at is not None
    assert payload == {"from": "new", "to": "resolved", "note": "Battery replaced"}


# ---------- update_summary ----------

async def test_update_summary(rows, published):
    async with Client(mcp) as client:
        created = await create(client, rows)
        out = result_json(await client.call_tool("update_summary", {
            "ticket_id": created["ticket_id"], "summary": "Battery not charging; in warranty."}))
        ticket = result_json(await client.call_tool("get_ticket", {"ticket_id": created["ticket_id"]}))
        missing = await client.call_tool("update_summary", {"ticket_id": str(uuid.uuid4()), "summary": "x"})
    assert out["updated"] is True
    assert ticket["ticket"]["ai_summary"] == "Battery not charging; in warranty."
    assert error_json(missing)["error"] == "not_found"


# ---------- set_diagnostic_plan ----------

async def test_set_diagnostic_plan_skips_steps_already_there(rows, published, pool):
    steps = ["Try a different charger", "Check the battery health report"]
    async with Client(mcp) as client:
        created = await create(client, rows)
        first = result_json(await client.call_tool("set_diagnostic_plan", {
            "ticket_id": created["ticket_id"], "steps": steps}))
        again = result_json(await client.call_tool("set_diagnostic_plan", {
            "ticket_id": created["ticket_id"], "steps": [*steps, "Reset the SMC"], "suggested_by": "playbook"}))
        bad = await client.call_tool("set_diagnostic_plan", {
            "ticket_id": created["ticket_id"], "steps": ["x"], "suggested_by": "customer"})
    assert [s["position"] for s in first["added"]] == [1, 2]
    assert again["skipped"] == steps and [s["step"] for s in again["added"]] == ["Reset the SMC"]
    assert again["added"][0]["position"] == 3
    assert error_json(bad)["error"] == "bad_suggested_by"
    async with pool.acquire() as conn:
        rows_ = await conn.fetch(
            "SELECT step, suggested_by, result FROM diagnostic_steps WHERE ticket_id = $1 ORDER BY position",
            uuid.UUID(created["ticket_id"]))
    assert [(r["suggested_by"], r["result"]) for r in rows_] == [("ai", "pending"), ("ai", "pending"),
                                                                 ("playbook", "pending")]
