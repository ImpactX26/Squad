"""Shared fixtures for the MCP server smoke tests.

The tests don't rely on the seed (db/seed), which anyone may re-run: each test inserts the rows
it needs, with names no seed row uses, and deletes them afterwards. Database tests skip, with the
reason, when the database is unreachable.
"""

import json
import uuid
from datetime import date, timedelta
from urllib.parse import urlsplit

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.common import db, events
from mcp_servers.common.settings import get_settings


def result_json(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def error_json(result) -> dict:
    assert result.is_error, result.content[0].text
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


@pytest.fixture
async def pool():
    try:
        p = await db.get_pool()
    except ToolError as exc:  # database_unavailable
        await db.close_pool()
        host = urlsplit(db.dsn(get_settings().database_url)).hostname
        pytest.skip(f"database unreachable at {host} ({type(exc.__cause__).__name__})")
    yield p
    await db.close_pool()


@pytest.fixture
def published(monkeypatch):
    """Record events instead of POSTing them."""
    sent: list[tuple[str, dict]] = []

    async def fake_publish(event_type, data, **_):
        sent.append((event_type, data))
        return True

    monkeypatch.setattr(events, "publish", fake_publish)
    return sent


@pytest.fixture
async def rows(pool):
    """A customer with one owned laptop, an unowned laptop, a telegram conversation, parts and services."""
    tag = uuid.uuid4().hex[:6].upper()
    async with pool.acquire() as conn:
        customer = await conn.fetchval(
            "INSERT INTO customers (full_name, email) VALUES ($1, $2) RETURNING id",
            f"Smoke Test {tag}", f"smoke-{tag.lower()}@example.test",
        )
        other_customer = await conn.fetchval(
            "INSERT INTO customers (full_name) VALUES ($1) RETURNING id", f"Other {tag}"
        )
        model = await conn.fetchval(
            """INSERT INTO product_models (model_number, brand, name, category, specs, warranty_months)
               VALUES ($1, 'Aurora', $2, 'laptop', $3, 12) RETURNING id""",
            f"T{tag}", f"Aurora Test {tag}", {"ram_gb": 16},
        )
        product = await conn.fetchval(
            """INSERT INTO products (serial_number, model_id, color, purchase_date, warranty_until, customer_id)
               VALUES ($1, $2, 'silver', $3, $4, $5) RETURNING id""",
            f"T{tag}-OWNED1", model, date.today() - timedelta(days=100), date.today() + timedelta(days=265), customer,
        )
        unowned = await conn.fetchval(
            """INSERT INTO products (serial_number, model_id, warranty_until)
               VALUES ($1, $2, $3) RETURNING id""",
            f"T{tag}-FREE01", model, date.today() - timedelta(days=1),
        )
        cheap = await conn.fetchval(
            """INSERT INTO parts (sku, name, part_type, unit_price) VALUES ($1, 'Battery 70Wh', 'battery', 5.40)
               RETURNING id""", f"BAT-{tag}-A",
        )
        dear = await conn.fetchval(
            """INSERT INTO parts (sku, name, part_type, unit_price) VALUES ($1, 'Battery 90Wh', 'battery', 7.10)
               RETURNING id""", f"BAT-{tag}-B",
        )
        await conn.executemany(
            "INSERT INTO part_compatibility (part_id, model_id) VALUES ($1, $2)", [(cheap, model), (dear, model)]
        )
        await conn.execute(
            """INSERT INTO service_catalog (code, name, part_type, labour_fee, requires_visit, required_skill)
               VALUES ($1, 'Battery replacement', 'battery', 1.50, TRUE, 'battery'),
                      ($2, 'OS reinstall', NULL, 2.00, TRUE, NULL),
                      ($3, 'RAM upgrade', 'ram', 1.00, TRUE, 'ram')""",
            f"BAT_{tag}", f"OS_{tag}", f"RAM_{tag}",
        )
        conversation = await conn.fetchval(
            """INSERT INTO conversations (customer_id, channel, external_thread_id)
               VALUES ($1, 'telegram', $2) RETURNING id""",
            customer, f"smoke-{tag}",
        )
    data = {
        "tag": tag, "customer_id": str(customer), "other_customer_id": str(other_customer),
        "model_id": str(model), "model_number": f"T{tag}", "product_id": str(product),
        "serial": f"T{tag}-OWNED1", "unowned_id": str(unowned), "unowned_serial": f"T{tag}-FREE01",
        "conversation_id": str(conversation), "cheap_sku": f"BAT-{tag}-A",
        "battery_service": f"BAT_{tag}", "os_service": f"OS_{tag}", "ram_service": f"RAM_{tag}",
    }
    yield data

    customers = [customer, other_customer]
    async with pool.acquire() as conn, conn.transaction():
        tickets = [r["id"] for r in await conn.fetch(
            "SELECT id FROM tickets WHERE customer_id = ANY($1::uuid[])", customers)]
        await conn.execute(
            "DELETE FROM messages WHERE ticket_id = ANY($1::uuid[]) OR conversation_id = $2", tickets, conversation
        )
        await conn.execute("DELETE FROM conversations WHERE customer_id = ANY($1::uuid[])", customers)
        await conn.execute("DELETE FROM tickets WHERE id = ANY($1::uuid[])", tickets)  # cascades events and steps
        await conn.execute("DELETE FROM products WHERE model_id = $1", model)
        await conn.execute("DELETE FROM parts WHERE id = ANY($1::uuid[])", [cheap, dear])  # cascades compatibility
        await conn.execute("DELETE FROM service_catalog WHERE code LIKE $1", f"%_{tag}")
        await conn.execute("DELETE FROM product_models WHERE id = $1", model)
        await conn.execute("DELETE FROM customers WHERE id = ANY($1::uuid[])", customers)
