"""Customer intake (ARCHITECTURE.md §4.1, §7.1).

Customer text is untrusted, so intake is a fixed pipeline over a small tool set. INTAKE_TOOLS is
checked before the hub is called: payments, dispatch and inventory are out of reach in code.
"""

from typing import Any

from app.brain.mcp_hub import MCPHub, ToolNotAllowed, ToolResult, get_hub
from app.brain.router import MODEL_FORBIDDEN_TOOLS, role_allows

INTAKE_TOOLS: frozenset[str] = role_allows("intake", frozenset({
    "catalog__lookup_serial", "catalog__lookup_model", "catalog__get_customer_products",
    # A fixed pipeline step, only when lookup_serial reports no owner (§4.1, §6.3).
    "catalog__link_product_to_customer",
    "tickets__create_ticket", "tickets__find_similar_tickets", "tickets__add_followup", "tickets__add_message",
    "tickets__update_summary", "tickets__set_diagnostic_plan", "tickets__record_diagnostic_results",
    "knowledge__get_playbook", "knowledge__suggest_next_steps", "knowledge__search_kb",
    "messaging__send_reply",
}))

if INTAKE_TOOLS & MODEL_FORBIDDEN_TOOLS:  # a hard stop, not an assert: asserts vanish under python -O
    raise RuntimeError(f"intake must not reach {sorted(INTAKE_TOOLS & MODEL_FORBIDDEN_TOOLS)}")


async def call(name: str, arguments: dict[str, Any], hub: MCPHub | None = None) -> ToolResult:
    """Call a tool for intake. Anything outside INTAKE_TOOLS is refused before the hub is called."""
    if name not in INTAKE_TOOLS:
        raise ToolNotAllowed(f"intake may not call {name}")
    return await (hub or get_hub()).call_tool(name, arguments, allowed=INTAKE_TOOLS)
