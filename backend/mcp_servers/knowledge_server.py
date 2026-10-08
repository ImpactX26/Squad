"""knowledge MCP server (:8103), ARCHITECTURE.md §5.3.

Block 1 tool: get_playbook. suggest_next_steps and search_kb (semantic search over the playbook
embeddings) come in Block 2.

Run: uv run python -m mcp_servers.knowledge_server
"""

from mcp.server import MCPServer

from mcp_servers import serve
from mcp_servers.common.db import connection
from mcp_servers.common.results import dumps, fail, parse_uuid

PORT = 8103

# kb_playbooks.category is the product category: a playbook with no model_id applies to every
# model in it (§8.1). issue_type uses intake's vocabulary (§4.4: battery, charging, display, ...).
PRODUCT_CATEGORIES = ("laptop", "desktop", "headphones", "accessory")

mcp = MCPServer("knowledge", instructions="Diagnostic playbooks: ordered troubleshooting steps per issue and product.")


@mcp.tool(structured_output=False)
async def get_playbook(issue_type: str, category: str, model_id: str | None = None) -> str:
    """Ordered diagnostic steps for an issue_type on a product category (laptop, desktop, headphones,
    accessory). With model_id, that model's own playbook wins over the category's.
    """
    issue = issue_type.strip().lower()
    product_category = category.strip().lower()
    if product_category not in PRODUCT_CATEGORIES:
        raise fail("bad_category", f"category must be a product category: {', '.join(PRODUCT_CATEGORIES)}",
                   field="category", value=category)
    model = parse_uuid(model_id, "model_id") if model_id else None

    async with connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, issue_type, category, model_id, title, steps FROM kb_playbooks
            WHERE issue_type = $1 AND category = $2 AND (model_id IS NULL OR model_id = $3::uuid)
            ORDER BY (model_id IS NULL), title
            LIMIT 1
            """,
            issue, product_category, model,
        )
    if row is None:
        return dumps({"found": False, "issue_type": issue, "category": product_category})
    return dumps({
        "found": True,
        "playbook_id": row["id"],
        "title": row["title"],
        "issue_type": row["issue_type"],
        "category": row["category"],
        "model_specific": row["model_id"] is not None,
        "steps": [{"number": n, **step} for n, step in enumerate(row["steps"], start=1)],
    })


if __name__ == "__main__":
    serve(mcp, PORT)
