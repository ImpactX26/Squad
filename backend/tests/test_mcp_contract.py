"""The tool surface of every MCP server built so far, against ARCHITECTURE.md §5. No database needed."""

import pytest
from mcp import Client

from mcp_servers import catalog_server, knowledge_server, messaging_server, tickets_server

# Block 1's tools, with exactly the argument names of §5.
TOOLS = {
    tickets_server: {
        "create_ticket": {"customer_id", "product_id", "category", "issue_type", "title", "description",
                          "source_channel", "conversation_id", "flags", "priority"},
        "get_ticket": {"ticket_id", "ticket_number"},
        "add_message": {"ticket_id", "conversation_id", "sender_type", "body", "body_original"},
        "update_status": {"ticket_id", "status", "note"},
        "update_summary": {"ticket_id", "summary"},
        "set_diagnostic_plan": {"ticket_id", "steps", "suggested_by"},
    },
    catalog_server: {
        "lookup_serial": {"serial_number"},
        "lookup_model": {"model_number"},
        "get_customer_products": {"customer_id"},
        "link_product_to_customer": {"product_id", "customer_id"},
        "get_service_price": {"service_code", "model_id"},
    },
    knowledge_server: {
        "get_playbook": {"issue_type", "category", "model_id"},
    },
    messaging_server: {
        "send_reply": {"conversation_id", "text"},
        "notify_staff": {"user_id", "role", "title", "body", "link", "type"},
    },
}


async def tool_args(module) -> dict[str, set[str]]:
    async with Client(module.mcp) as client:
        listed = await client.list_tools()
    return {t.name: set(t.input_schema.get("properties", {})) for t in listed.tools}


@pytest.mark.parametrize("module", list(TOOLS), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
async def test_each_server_has_exactly_its_block_1_tools(module):
    assert await tool_args(module) == TOOLS[module]


@pytest.mark.parametrize("module", list(TOOLS), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
async def test_no_tool_takes_an_amount(module):
    # Amounts are computed in code from the catalog (§5.5): no tool takes one.
    for tool, args in (await tool_args(module)).items():
        assert not args & {"amount", "price", "total", "unit_price", "labour_fee"}, tool


async def test_no_messaging_tool_takes_a_channel():
    # A reply goes on the conversation's own channel; a model never chooses it (§6.1).
    for tool, args in (await tool_args(messaging_server)).items():
        assert not args & {"channel", "source_channel", "platform"}, tool


def test_each_server_has_its_own_port():
    assert [m.PORT for m in TOOLS] == [8101, 8102, 8103, 8104]
