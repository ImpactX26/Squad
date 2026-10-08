"""The MCP hub, the router's forbidden tools and intake's allowlist (§4.1, §4.3, §5.5).

Most tests talk to the real servers in-process; the last one uses them over HTTP when they run.
"""

import pytest
from mcp.server import MCPServer

from app.brain import intake
from app.brain.mcp_hub import MCPHub, MCPUnavailable, ToolNotAllowed, server_urls
from app.brain.router import MODEL_FORBIDDEN_TOOLS, ROLE_SERVERS
from app.core.config import get_settings
from mcp_servers import catalog_server, knowledge_server, messaging_server, run_all, tickets_server
from tests.mcp_support import pool  # noqa: F401 (fixture)

DEAD = "http://127.0.0.1:8199/mcp"  # nothing listens here

# A stand-in payments server: only its tool names matter here.
payments = MCPServer("payments")


@payments.tool()
async def submit_utr(token: str, utr: str) -> str:
    return "never called"


@payments.tool()
async def get_payment_status(payment_id: str) -> str:
    return "never called"


def in_process_hub(**extra) -> MCPHub:
    return MCPHub({"tickets": tickets_server.mcp, "catalog": catalog_server.mcp,
                   "knowledge": knowledge_server.mcp, "messaging": messaging_server.mcp, **extra})


async def test_refresh_registers_every_tool_as_server__tool():
    hub = in_process_hub()
    assert await hub.refresh() == {"tickets": True, "catalog": True, "knowledge": True, "messaging": True}
    assert {"tickets__create_ticket", "catalog__lookup_serial", "knowledge__get_playbook",
            "messaging__send_reply"} <= set(hub.tools)
    spec = hub.tools["catalog__lookup_serial"].openai()
    assert spec["type"] == "function" and spec["function"]["name"] == "catalog__lookup_serial"
    assert "serial_number" in spec["function"]["parameters"]["properties"]


async def test_a_model_is_never_offered_a_forbidden_tool():
    hub = in_process_hub(payments=payments)
    await hub.refresh()
    offered = {t["function"]["name"] for t in hub.openai_tools(frozenset(hub.tools))}
    assert "payments__get_payment_status" in offered
    assert "payments__submit_utr" in hub.tools and "payments__submit_utr" not in offered


async def test_a_tool_outside_the_allowlist_is_refused_before_anything_is_sent():
    hub = MCPHub({"tickets": DEAD, "payments": DEAD})  # a sent call would be MCPUnavailable instead
    with pytest.raises(ToolNotAllowed):
        await hub.call_tool("payments__submit_utr", {}, allowed=frozenset({"tickets__get_ticket"}))


async def test_a_refusal_is_a_result_not_an_exception():
    result = await in_process_hub().call_tool("tickets__get_ticket", {}, allowed=frozenset({"tickets__get_ticket"}))
    assert result.ok is False and result.error == "bad_arguments"


async def test_a_tool_answer_comes_back_as_json(pool):
    result = await in_process_hub().call_tool(
        "catalog__lookup_serial", {"serial_number": "NOPE-000000"}, allowed=frozenset({"catalog__lookup_serial"}))
    assert result.ok and result.data == {"found": False, "serial_number": "NOPE-000000"} and result.ms >= 0


async def test_a_down_server_is_mcp_unavailable():
    hub = MCPHub({"tickets": DEAD})
    with pytest.raises(MCPUnavailable, match="tickets unreachable"):
        await hub.call_tool("tickets__get_ticket", {"ticket_number": "SR-2026-00001"},
                            allowed=frozenset({"tickets__get_ticket"}))
    assert await hub.refresh() == {"tickets": False}


# ---------- the allowlists ----------

def test_intake_reaches_only_its_four_servers_and_no_forbidden_tool():
    assert {name.split("__")[0] for name in intake.INTAKE_TOOLS} <= ROLE_SERVERS["intake"]
    assert ROLE_SERVERS["intake"].isdisjoint({"payments", "dispatch", "inventory"})
    assert not intake.INTAKE_TOOLS & MODEL_FORBIDDEN_TOOLS
    assert {"catalog__lookup_serial", "tickets__create_ticket", "knowledge__get_playbook",
            "messaging__send_reply"} <= intake.INTAKE_TOOLS


@pytest.mark.parametrize("name", ["payments__create_payment_request", "dispatch__create_job",
                                  "inventory__reserve_part", "tickets__update_status"])
async def test_intake_cannot_call_anything_else(name):
    with pytest.raises(ToolNotAllowed):
        await intake.call(name, {}, hub=MCPHub({"tickets": DEAD, "payments": DEAD}))


def test_the_forbidden_tools_of_section_5_5():
    assert {"payments__submit_utr", "payments__mark_paid_manually", "payments__create_payment_request",
            "payments__cancel_payment", "payments__list_payments", "payments__correct_utr_manually",
            "payments__reject_payment", "payments__extend_payment", "dispatch__create_job",
            "dispatch__update_job_status", "dispatch__reject_job", "inventory__reserve_part",
            "inventory__consume_part", "inventory__release_part",
            "inventory__create_restock_request"} == MODEL_FORBIDDEN_TOOLS


# ---------- over HTTP, when make mcp runs ----------

async def test_the_hub_reaches_the_four_servers_over_http():
    urls = server_urls(get_settings())
    down = [name for name in run_all.SERVERS if not run_all.listening(run_all.port_of(name))]
    if down:
        pytest.skip(f"MCP servers not running ({', '.join(down)}): start make mcp to run this test")
    hub = MCPHub(urls)
    assert all((await hub.refresh()).values())
    result = await hub.call_tool("catalog__lookup_serial", {"serial_number": "VX15-Q8M2D5"},
                                 allowed=intake.INTAKE_TOOLS)
    assert result.ok and result.data["found"] in (True, False)
