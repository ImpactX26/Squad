"""Which MCP servers and tools each brain role may reach (ARCHITECTURE.md §4, §4.3, §5.5).

Tool names are the hub's "<server>__<tool>". pick_servers, filter_tools and compact_tools for the
copilot's tool loop come in Block 5.
"""

ROLE_SERVERS: dict[str, frozenset[str]] = {
    # Customer text is untrusted: intake never reaches payments, dispatch or inventory (§4.1).
    "intake": frozenset({"catalog", "tickets", "knowledge", "messaging"}),
    "copilot": frozenset({"tickets", "catalog", "knowledge", "messaging", "payments", "dispatch", "inventory"}),
    "writer": frozenset(),
    "automation": frozenset({"payments", "dispatch", "inventory", "tickets", "messaging"}),
}

# Billing a customer is a staff action (§5.5).
PAYMENT_LINK_TOOLS = frozenset({"payments__create_payment_request", "payments__cancel_payment"})
# The payments page's own tools: every write needs an admin's id and a note, and only the API calls them.
PAYMENT_ADMIN_TOOLS = frozenset({
    "payments__list_payments", "payments__correct_utr_manually", "payments__reject_payment", "payments__extend_payment",
})
# Stock and dispatch writers: only the workflows and the job API call them, in code (§5.6, §5.7).
WORKFLOW_ONLY_TOOLS = frozenset({
    "dispatch__create_job", "dispatch__update_job_status", "dispatch__reject_job",
    "inventory__reserve_part", "inventory__consume_part", "inventory__release_part", "inventory__create_restock_request",
})

# Never offered to a model, whatever its role or a command's allowed_tools.
MODEL_FORBIDDEN_TOOLS = frozenset({"payments__submit_utr", "payments__mark_paid_manually"}) \
    | PAYMENT_LINK_TOOLS | PAYMENT_ADMIN_TOOLS | WORKFLOW_ONLY_TOOLS


def server_of(tool_name: str) -> str:
    return tool_name.split("__", 1)[0]


def role_allows(role: str, tool_names: frozenset[str]) -> frozenset[str]:
    """The tools a role may call: those whose server is in ROLE_SERVERS[role]."""
    servers = ROLE_SERVERS[role]
    return frozenset(name for name in tool_names if server_of(name) in servers)
