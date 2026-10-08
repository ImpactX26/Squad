"""knowledge MCP server (ARCHITECTURE.md §5.3) — :8103.

Diagnostic playbooks and next troubleshooting steps. Loads fastembed (app.brain.embeddings)
for semantic playbook search.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from app.brain.embeddings import aembed_texts
from mcp_servers import run
from mcp_servers.common import db

mcp = MCPServer(name="knowledge", instructions=__doc__)

_COLS = "id AS playbook_id, issue_type, category, model_id, title, steps"
_PLAYBOOK = f"SELECT {_COLS} FROM kb_playbooks"


@mcp.tool()
async def get_playbook(issue_type: str, category: str | None = None, model_id: str | None = None) -> dict[str, Any]:
    """Ordered diagnostic steps for an issue type (§5.3).

    `category` is the product category — laptop, desktop, headphones, accessory — because a
    playbook without a model_id applies to every model in that category (§8.1). It is not the
    ticket's hardware/software category. Prefers a playbook written for this exact model.
    """
    row = await db.fetchrow(
        _PLAYBOOK + """
        WHERE lower(issue_type) = lower($1)
          AND ($2::text IS NULL OR lower(category) = lower($2::text))
          AND (model_id IS NULL OR $3::uuid IS NULL OR model_id = $3::uuid)
        ORDER BY (model_id IS NOT NULL AND model_id = $3::uuid) DESC, model_id NULLS LAST
        LIMIT 1""",
        issue_type, category, model_id,
    )
    if row is None:
        return {"found": False, "issue_type": issue_type, "category": category}
    return {"found": True, **db.row_to_dict(row)}


@mcp.tool()
async def suggest_next_steps(ticket_id: str, limit: int = 5) -> dict[str, Any]:
    """Playbook steps for this ticket minus the ones already tried, with what worked and what failed (§5.3)."""
    ticket = await db.fetchrow(
        # m.category (laptop/desktop/headphones), not t.category (hardware/software): that is
        # what kb_playbooks.category holds (§8.1).
        "SELECT t.id, t.issue_type, m.category AS product_category, p.model_id FROM tickets t"
        " LEFT JOIN products p ON p.id = t.product_id"
        " LEFT JOIN product_models m ON m.id = p.model_id WHERE t.id = $1",
        ticket_id,
    )
    if ticket is None:
        return {"found": False, "error": "ticket not found", "ticket_id": ticket_id}

    tried = await db.fetch(
        "SELECT step, result, notes FROM diagnostic_steps WHERE ticket_id = $1 ORDER BY position", ticket_id)
    already = {r["step"].strip().lower() for r in tried}

    playbook = await get_playbook(
        ticket["issue_type"], ticket["product_category"],
        str(ticket["model_id"]) if ticket["model_id"] else None)
    next_steps: list[dict[str, Any]] = []
    if playbook.get("found"):
        for entry in playbook["steps"]:
            step = entry.get("step", "") if isinstance(entry, dict) else str(entry)
            if step.strip().lower() not in already:
                next_steps.append(entry if isinstance(entry, dict) else {"step": step})

    return {
        "found": True,
        "ticket_id": ticket_id,
        "issue_type": ticket["issue_type"],
        "playbook_id": playbook.get("playbook_id"),
        "playbook_title": playbook.get("title"),
        "next_steps": next_steps[:limit],
        "worked": [r["step"] for r in tried if r["result"] == "worked"],
        "failed": [r["step"] for r in tried if r["result"] == "failed"],
        "pending": [r["step"] for r in tried if r["result"] == "pending"],
    }


@mcp.tool()
async def search_kb(query: str, limit: int = 5) -> dict[str, Any]:
    """Semantic search over the playbooks, by cosine similarity on the playbook embedding (§5.3)."""
    if not query.strip():
        return {"query": query, "results": []}
    vector = db.to_vector_literal((await aembed_texts([query]))[0])
    rows = await db.fetch(
        f"SELECT {_COLS}, 1 - (embedding <=> $1::vector) AS similarity FROM kb_playbooks"
        " WHERE embedding IS NOT NULL ORDER BY embedding <=> $1::vector LIMIT $2",
        vector, max(1, min(limit, 20)),
    )
    return {"query": query, "results": db.rows_to_list(rows)}


if __name__ == "__main__":
    run(mcp, "knowledge")