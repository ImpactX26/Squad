"""Smoke tests for the knowledge MCP server (:8103) over the MCP protocol in-process.

The playbooks here are the test's own (an issue type no seed row uses), deleted afterwards.
"""

import uuid

import pytest
from mcp import Client

from mcp_servers.knowledge_server import mcp
from tests.mcp_support import error_json, pool, result_json  # noqa: F401 (fixtures)


@pytest.fixture
async def playbooks(pool):
    issue = f"test_{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        model = await conn.fetchval(
            """INSERT INTO product_models (model_number, brand, name, category)
               VALUES ($1, 'Aurora', 'Playbook Test', 'laptop') RETURNING id""",
            f"PB{uuid.uuid4().hex[:6].upper()}",
        )
        await conn.executemany(
            "INSERT INTO kb_playbooks (issue_type, category, model_id, title, steps) VALUES ($1, $2, $3, $4, $5)",
            [
                (issue, "laptop", None, "Every laptop",
                 [{"step": "Try another adapter", "expected": "It charges", "resolves_if": "It charges"},
                  {"step": "Replace the battery", "expected": "Holds charge", "resolves_if": "Fixed"}]),
                (issue, "laptop", model, "This model only",
                 [{"step": "Update the model's firmware", "expected": "Latest", "resolves_if": "Fixed"}]),
                (issue, "headphones", None, "Every headphone",
                 [{"step": "Check the balance slider", "expected": "Centred", "resolves_if": "Fixed"}]),
            ],
        )
    yield {"issue": issue, "model_id": str(model)}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM kb_playbooks WHERE issue_type = $1", issue)
        await conn.execute("DELETE FROM product_models WHERE id = $1", model)


async def test_get_playbook_for_a_category(playbooks):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("get_playbook", {
            "issue_type": playbooks["issue"].upper(), "category": " Laptop "}))
    assert out["found"] is True and out["model_specific"] is False
    assert out["title"] == "Every laptop"
    assert [(s["number"], s["step"]) for s in out["steps"]] == [(1, "Try another adapter"), (2, "Replace the battery")]
    assert set(out["steps"][0]) == {"number", "step", "expected", "resolves_if"}


async def test_a_models_own_playbook_wins(playbooks):
    other_model = str(uuid.uuid4())
    async with Client(mcp) as client:
        own = result_json(await client.call_tool("get_playbook", {
            "issue_type": playbooks["issue"], "category": "laptop", "model_id": playbooks["model_id"]}))
        fallback = result_json(await client.call_tool("get_playbook", {
            "issue_type": playbooks["issue"], "category": "laptop", "model_id": other_model}))
    assert own["title"] == "This model only" and own["model_specific"] is True
    assert fallback["title"] == "Every laptop"


async def test_no_playbook_is_found_false_not_an_error(playbooks):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("get_playbook", {"issue_type": playbooks["issue"], "category": "desktop"}))
        unknown = result_json(await client.call_tool("get_playbook", {"issue_type": "teleportation", "category": "laptop"}))
    assert out == {"found": False, "issue_type": playbooks["issue"], "category": "desktop"}
    assert unknown["found"] is False


async def test_get_playbook_refuses_a_ticket_category():
    # hardware / software is a ticket's category; playbooks are filed by product category.
    async with Client(mcp) as client:
        result = await client.call_tool("get_playbook", {"issue_type": "battery", "category": "hardware"})
    assert error_json(result)["error"] == "bad_category"
