"""GET /api/tickets against the real database, inside a rolled-back transaction.

Other tickets may exist in the shared dev database, so each test checks its own rows only.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.api_support import add_staff, app, bearer, client, session  # noqa: F401 (fixtures)


@pytest.fixture
async def inbox(session):
    """A customer with a laptop and four tickets of different priorities, times and channels."""
    tag = uuid.uuid4().hex[:6].upper()
    customer = await session.scalar(text(
        "INSERT INTO customers (full_name, email) VALUES ('Inbox Test', :email) RETURNING id"),
        {"email": f"inbox-{tag.lower()}@example.com"})
    model = await session.scalar(text(
        "INSERT INTO product_models (model_number, brand, name, category) "
        "VALUES (:n, 'Aurora', 'Aurora Test', 'laptop') RETURNING id"), {"n": f"IT{tag}"})
    product = await session.scalar(text(
        "INSERT INTO products (serial_number, model_id, color) VALUES (:s, :m, 'Silver') RETURNING id"),
        {"s": f"IT{tag}-ABC123", "m": model})
    now = datetime.now(UTC)

    async def ticket(title, priority, status, channel, updated_minutes_ago, product_id=None):
        return await session.scalar(text(
            """INSERT INTO tickets (customer_id, product_id, source_channel, category, title, description,
                                    priority, status, ai_summary, created_at, updated_at)
               VALUES (:c, :p, :ch, 'hardware', :title, 'x', :pr, :st, :summary, :t, :t) RETURNING id"""),
            {"c": customer, "p": product_id, "ch": channel, "title": f"{title} {tag}", "pr": priority, "st": status,
             "summary": f"Summary of {title}", "t": now - timedelta(minutes=updated_minutes_ago)})

    ids = {
        "medium_new": await ticket("Medium recent", "medium", "new", "telegram", 1, product),
        "urgent_old": await ticket("Urgent old", "urgent", "in_progress", "discord", 300),
        "medium_old": await ticket("Medium old", "medium", "awaiting_customer", "web", 60),
        "resolved": await ticket("Done", "high", "resolved", "email", 5),
    }
    # The urgent ticket's customer later wrote by email too.
    await session.execute(text(
        "INSERT INTO messages (ticket_id, sender_type, channel, body) VALUES (:t, 'customer', 'email', 'Any update?')"),
        {"t": ids["urgent_old"]})
    return {k: str(v) for k, v in ids.items()} | {"tag": tag}


def mine(body, inbox) -> list[str]:
    ours = {v: k for k, v in inbox.items() if k != "tag"}
    return [ours[t["id"]] for t in body["tickets"] if t["id"] in ours]


async def test_the_inbox_is_priority_first_then_most_recently_updated(app, session, inbox):
    agent = await add_staff(session)
    async with client(app) as c:
        response = await c.get("/api/tickets", params={"limit": 200}, headers=bearer(agent))
    assert response.status_code == 200
    body = response.json()
    assert mine(body, inbox) == ["urgent_old", "resolved", "medium_new", "medium_old"]
    assert body["total"] >= 4 and body["limit"] == 200 and body["offset"] == 0


async def test_a_row_has_what_the_inbox_shows(app, session, inbox):
    agent = await add_staff(session)
    async with client(app) as c:
        rows = {t["id"]: t for t in (await c.get("/api/tickets", params={"limit": 200}, headers=bearer(agent))).json()["tickets"]}
    recent, urgent = rows[inbox["medium_new"]], rows[inbox["urgent_old"]]
    assert recent["title"] == f"Medium recent {inbox['tag']}"
    assert recent["ticket_number"].startswith("SR-")
    assert recent["customer"]["name"] == "Inbox Test"
    assert recent["device"] == {"model_name": "Aurora Test", "serial_number": f"IT{inbox['tag']}-ABC123",
                                "color": "Silver", "category": "laptop"}
    assert (recent["status"], recent["priority"], recent["source_channel"]) == ("new", "medium", "telegram")
    assert recent["ai_summary"] == "Summary of Medium recent"
    assert urgent["device"] is None
    assert urgent["channels"] == ["discord", "email"]  # first channel first


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "open"}, ["urgent_old", "medium_new", "medium_old"]),
        ({"status": "resolved"}, ["resolved"]),
        ({"priority": "medium"}, ["medium_new", "medium_old"]),
        ({"channel": "email"}, ["urgent_old", "resolved"]),  # a message by email counts
        ({"channel": "web"}, ["medium_old"]),
        ({"category": "software"}, []),
        ({"assignee": "unassigned", "priority": "urgent"}, ["urgent_old"]),
    ],
)
async def test_filters(app, session, inbox, params, expected):
    agent = await add_staff(session)
    async with client(app) as c:
        body = (await c.get("/api/tickets", params={**params, "limit": 200}, headers=bearer(agent))).json()
    assert mine(body, inbox) == expected


async def test_assignee_me(app, session, inbox):
    agent = await add_staff(session)
    await session.execute(text("UPDATE tickets SET assigned_agent_id = :a WHERE id = :t"),
                          {"a": agent.id, "t": uuid.UUID(inbox["medium_old"])})
    async with client(app) as c:
        body = (await c.get("/api/tickets", params={"assignee": "me"}, headers=bearer(agent))).json()
    assert [t["id"] for t in body["tickets"]] == [inbox["medium_old"]] and body["total"] == 1


async def test_who_may_see_the_inbox(app, session):
    technician = await add_staff(session, role="technician")
    admin = await add_staff(session, role="admin")
    async with client(app) as c:
        anonymous = await c.get("/api/tickets")
        refused = await c.get("/api/tickets", headers=bearer(technician))
        allowed = await c.get("/api/tickets", params={"limit": 1}, headers=bearer(admin))
        bad = await c.get("/api/tickets", params={"status": "opened"}, headers=bearer(admin))
    assert (anonymous.status_code, refused.status_code, allowed.status_code, bad.status_code) == (401, 403, 200, 422)


# ---------- GET /api/tickets/{id}/timeline ----------


async def test_the_timeline_is_the_conversation_across_channels(app, session, inbox):
    agent = await add_staff(session, name="Arjun Agent")
    ticket = uuid.UUID(inbox["medium_new"])
    customer = await session.scalar(text("SELECT customer_id FROM tickets WHERE id = :t"), {"t": ticket})
    telegram = await session.scalar(text(
        "INSERT INTO conversations (customer_id, channel, external_thread_id, ticket_id) "
        "VALUES (:c, 'telegram', :thread, :t) RETURNING id"), {"c": customer, "thread": f"tl-{inbox['tag']}", "t": ticket})
    start = datetime.now(UTC) - timedelta(minutes=10)

    async def message(minute, sender, channel, body, conversation=None, staff=None, internal=False):
        await session.execute(text(
            """INSERT INTO messages (conversation_id, ticket_id, sender_type, sender_staff_id, channel, body,
                                     is_internal_note, created_at)
               VALUES (:conv, :t, :sender, :staff, :channel, :body, :internal, :at)"""),
            {"conv": conversation, "t": ticket, "sender": sender, "staff": staff, "channel": channel, "body": body,
             "internal": internal, "at": start + timedelta(minutes=minute)})

    await message(0, "customer", "telegram", "Battery stuck at 0%", telegram)
    await message(1, "ai", "telegram", "Your ticket is open", telegram)
    await message(2, "agent", "telegram", "Confirmed, replacing it", telegram, staff=agent.id)
    await message(3, "agent", "internal", "Check stock first", staff=agent.id, internal=True)
    await message(4, "system", "internal", "Customer says it is fixed")
    async with client(app) as c:
        response = await c.get(f"/api/tickets/{ticket}/timeline", headers=bearer(agent))
    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == str(ticket) and body["ticket_number"].startswith("SR-")
    assert [(m["sender_type"], m["author"], m["channel"], m["body"]) for m in body["messages"]] == [
        ("customer", "Inbox Test", "telegram", "Battery stuck at 0%"),
        ("ai", None, "telegram", "Your ticket is open"),
        ("agent", "Arjun Agent", "telegram", "Confirmed, replacing it"),
    ]
    assert set(body["messages"][0]) == {"id", "sender_type", "author", "channel", "body", "created_at"}


async def test_the_timeline_of_a_ticket_with_no_messages_is_empty(app, session, inbox):
    agent = await add_staff(session)
    async with client(app) as c:
        response = await c.get(f"/api/tickets/{inbox['medium_old']}/timeline", headers=bearer(agent))
    assert response.status_code == 200 and response.json()["messages"] == []


async def test_who_may_read_a_timeline(app, session, inbox):
    agent, admin, technician = [await add_staff(session, role=r) for r in ("agent", "admin", "technician")]
    async with client(app) as c:
        url = f"/api/tickets/{inbox['urgent_old']}/timeline"
        statuses = [(await c.get(url, headers=bearer(s))).status_code for s in (agent, admin, technician)]
        signed_out = (await c.get(url)).status_code
        missing = (await c.get(f"/api/tickets/{uuid.uuid4()}/timeline", headers=bearer(agent))).status_code
        not_an_id = (await c.get("/api/tickets/SR-2026-00001/timeline", headers=bearer(agent))).status_code
    assert statuses == [200, 200, 403] and signed_out == 401 and missing == 404 and not_an_id == 422
