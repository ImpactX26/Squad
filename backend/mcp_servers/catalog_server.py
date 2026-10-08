"""catalog MCP server (ARCHITECTURE.md §5.2) — :8102.

Serial numbers, models, warranty, owners, and service prices. Read-only except
link_product_to_customer, which registers ownership on first contact (§7.1).
"""

from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.payments.money import PAYMENT_CURRENCY
from mcp_servers import run
from mcp_servers.common import db

mcp = MCPServer(name="catalog", instructions=__doc__)

_UNIT = """
SELECT p.id AS product_id, p.serial_number, p.color, p.config, p.purchase_date, p.warranty_until,
       p.customer_id, m.id AS model_id, m.model_number, m.brand, m.name AS model_name,
       m.category, m.specs, m.warranty_months,
       c.full_name AS owner_name, c.email AS owner_email
FROM products p
JOIN product_models m ON m.id = p.model_id
LEFT JOIN customers c ON c.id = p.customer_id
"""


def _with_warranty(unit: dict[str, Any]) -> dict[str, Any]:
    until = unit.get("warranty_until")
    unit["in_warranty"] = bool(until and date.fromisoformat(until) >= date.today())
    return unit


@mcp.tool()
async def lookup_serial(serial_number: str) -> dict[str, Any]:
    """Unit, model, color, warranty status, and owner for one serial number.

    Returns {"found": false} when the serial isn't in the catalog, so intake can ask again (§7.1).
    """
    row = await db.fetchrow(_UNIT + " WHERE upper(p.serial_number) = upper($1)", serial_number.strip())
    if row is None:
        return {"found": False, "serial_number": serial_number}
    return {"found": True, **_with_warranty(db.row_to_dict(row))}


@mcp.tool()
async def lookup_model(model_number: str) -> dict[str, Any]:
    """Model specs and the parts compatible with it."""
    model = await db.fetchrow(
        "SELECT id AS model_id, model_number, brand, name AS model_name, category, specs, warranty_months"
        " FROM product_models WHERE upper(model_number) = upper($1)",
        model_number.strip(),
    )
    if model is None:
        return {"found": False, "model_number": model_number}
    parts = await db.fetch(
        "SELECT p.id AS part_id, p.sku, p.name, p.part_type, p.unit_price"
        " FROM parts p JOIN part_compatibility pc ON pc.part_id = p.id"
        " WHERE pc.model_id = $1 ORDER BY p.part_type, p.sku",
        model["model_id"],
    )
    return {"found": True, **db.row_to_dict(model), "compatible_parts": db.rows_to_list(parts)}


@mcp.tool()
async def get_customer_products(customer_id: str) -> dict[str, Any]:
    """Every device registered to this customer."""
    rows = await db.fetch(_UNIT + " WHERE p.customer_id = $1 ORDER BY p.purchase_date DESC NULLS LAST", customer_id)
    return {"customer_id": customer_id, "products": [_with_warranty(u) for u in db.rows_to_list(rows)]}


@mcp.tool()
async def link_product_to_customer(product_id: str, customer_id: str) -> dict[str, Any]:
    """Register ownership of a unit on first contact (§7.1).

    Keeps the existing owner and reports ownership_mismatch when the unit belongs to
    someone else, so the agent sees it instead of the record being silently reassigned (§6.3).
    """
    current = await db.fetchrow("SELECT customer_id FROM products WHERE id = $1", product_id)
    if current is None:
        return {"ok": False, "error": "product not found", "product_id": product_id}
    owner = current["customer_id"]
    if owner is not None and str(owner) != str(customer_id):
        return {"ok": False, "ownership_mismatch": True, "product_id": product_id, "owner_customer_id": str(owner)}
    if owner is None:
        await db.execute("UPDATE products SET customer_id = $2 WHERE id = $1", product_id, customer_id)
    return {"ok": True, "product_id": product_id, "customer_id": customer_id, "already_linked": owner is not None}


@mcp.tool()
async def get_service_price(service_code: str, model_id: str | None = None) -> dict[str, Any]:
    """Part price plus labour fee for a service on a model, as payment line items (§7.6)."""
    service = await db.fetchrow(
        "SELECT code, name, part_type, labour_fee, requires_visit, required_skill"
        " FROM service_catalog WHERE upper(code) = upper($1)",
        service_code.strip(),
    )
    if service is None:
        return {"found": False, "service_code": service_code}
    out = {"found": True, **db.row_to_dict(service), "model_id": model_id}
    line_items = [{"label": f"Labour — {service['name']}", "amount": float(service["labour_fee"])}]
    part = None
    if service["part_type"] and model_id:
        part = await db.fetchrow(
            "SELECT p.id AS part_id, p.sku, p.name, p.unit_price FROM parts p"
            " JOIN part_compatibility pc ON pc.part_id = p.id"
            " WHERE pc.model_id = $1 AND p.part_type = $2 ORDER BY p.unit_price LIMIT 1",
            model_id, service["part_type"],
        )
        if part:
            line_items.insert(0, {"label": f"{part['name']} ({part['sku']})", "amount": float(part["unit_price"])})
    out["part"] = db.row_to_dict(part)
    out["line_items"] = line_items
    out["total"] = round(sum(item["amount"] for item in line_items), 2)
    out["currency"] = PAYMENT_CURRENCY
    return out


if __name__ == "__main__":
    run(mcp, "catalog")