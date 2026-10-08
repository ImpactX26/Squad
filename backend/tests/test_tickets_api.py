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
