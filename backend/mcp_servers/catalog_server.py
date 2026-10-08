"""catalog MCP server (:8102), ARCHITECTURE.md §5.2: devices, models, parts and service prices.

Run: uv run python -m mcp_servers.catalog_server
"""

from datetime import date

import asyncpg
from mcp.server import MCPServer

from mcp_servers import serve
from mcp_servers.common.db import get_pool
from mcp_servers.common.results import dumps, fail, parse_uuid

PORT = 8102

mcp = MCPServer("catalog", instructions="Product catalog: look up serials and models, customer devices, service prices.")


def warranty(warranty_until: date | None) -> dict:
    if warranty_until is None:
        return {"status": "unknown", "until": None}
    days_left = (warranty_until - date.today()).days
    if days_left < 0:
        return {"status": "out_of_warranty", "until": warranty_until}
    return {"status": "in_warranty", "until": warranty_until, "days_left": days_left}


def device(r: asyncpg.Record) -> dict:
    return {
        "product_id": r["product_id"],
        "serial_number": r["serial_number"],
        "model_id": r["model_id"],
        "model_number": r["model_number"],
        "brand": r["brand"],
        "name": r["name"],
        "category": r["category"],
        "color": r["color"],
        "config": r["config"],
        "purchase_date": r["purchase_date"],
        "warranty": warranty(r["warranty_until"]),
    }


DEVICE_SELECT = """
    SELECT p.id AS product_id, p.serial_number, p.color, p.config, p.purchase_date, p.warranty_until,
           p.customer_id, m.id AS model_id, m.model_number, m.brand, m.name, m.category
    FROM products p JOIN product_models m ON m.id = p.model_id
"""


@mcp.tool(structured_output=False)
async def lookup_serial(serial_number: str) -> str:
    """Find one device by its serial number: unit, model, color, warranty status and owner."""
    serial = serial_number.strip().upper()
    if not serial:
        raise fail("missing_text", "serial_number must not be empty")
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(DEVICE_SELECT + " WHERE upper(p.serial_number) = $1", serial)
    if r is None:
        return dumps({"found": False, "serial_number": serial})
    # Only the owner's id: intake compares it with the customer writing in (§6.3), and a stranger's
    # name or email never reaches a reply.
    return dumps({"found": True, **device(r), "owner_customer_id": r["customer_id"]})


@mcp.tool(structured_output=False)
async def lookup_model(model_number: str) -> str:
    """A model's specs and the parts compatible with it."""
    number = model_number.strip().upper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        m = await conn.fetchrow(
            """SELECT id, model_number, brand, name, category, specs, warranty_months
               FROM product_models WHERE upper(model_number) = $1""",
            number,
        )
        if m is None:
            return dumps({"found": False, "model_number": number})
        parts = await conn.fetch(
            """
            SELECT p.id, p.sku, p.name, p.part_type, p.unit_price
            FROM parts p JOIN part_compatibility c ON c.part_id = p.id
            WHERE c.model_id = $1 ORDER BY p.part_type, p.unit_price, p.sku
            """,
            m["id"],
        )
    return dumps({
        "found": True,
        "model_id": m["id"], "model_number": m["model_number"], "brand": m["brand"], "name": m["name"],
        "category": m["category"], "specs": m["specs"], "warranty_months": m["warranty_months"],
        "compatible_parts": [
            {"part_id": p["id"], "sku": p["sku"], "name": p["name"], "part_type": p["part_type"],
             "unit_price": p["unit_price"]}
            for p in parts
        ],
    })


@mcp.tool(structured_output=False)
async def get_customer_products(customer_id: str) -> str:
    """The devices registered to a customer."""
    customer = parse_uuid(customer_id, "customer_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(DEVICE_SELECT + " WHERE p.customer_id = $1 ORDER BY p.created_at", customer)
    return dumps({"customer_id": customer, "products": [device(r) for r in rows]})


@mcp.tool(structured_output=False)
async def link_product_to_customer(product_id: str, customer_id: str) -> str:
    """Register a device with no owner to a customer on first contact. A device someone else owns is refused."""
    product = parse_uuid(product_id, "product_id")
    customer = parse_uuid(customer_id, "customer_id")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM customers WHERE id = $1)", customer):
            raise fail("not_found", "no such customer", field="customer_id")
        owner = await conn.fetchrow("SELECT customer_id FROM products WHERE id = $1 FOR UPDATE", product)
        if owner is None:
            raise fail("not_found", "no such product", field="product_id")
        if owner["customer_id"] == customer:
            return dumps({"linked": True, "already_linked": True, "product_id": product, "customer_id": customer})
        if owner["customer_id"] is not None:
            # Never moved: the ticket is flagged ownership_mismatch instead (§6.3).
            raise fail("owned_by_another", "this device is registered to another customer", product_id=product)
        await conn.execute("UPDATE products SET customer_id = $2 WHERE id = $1", product, customer)
    return dumps({"linked": True, "already_linked": False, "product_id": product, "customer_id": customer})


@mcp.tool(structured_output=False)
async def get_service_price(service_code: str, model_id: str) -> str:
    """The price of a service on a model: the compatible part's price plus the labour fee."""
    code = service_code.strip().upper()
    model = parse_uuid(model_id, "model_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        service = await conn.fetchrow(
            "SELECT code, name, part_type, labour_fee, requires_visit FROM service_catalog WHERE code = $1", code
        )
        if service is None:
            raise fail("unknown_service", "no service with that code", service_code=code)
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM product_models WHERE id = $1)", model):
            raise fail("not_found", "no such model", field="model_id")
        part = None
        if service["part_type"] is not None:
            # The cheapest compatible part, by SKU on a tie: the same pick as payments (§5.5).
            part = await conn.fetchrow(
                """
                SELECT p.sku, p.name, p.unit_price
                FROM parts p JOIN part_compatibility c ON c.part_id = p.id
                WHERE c.model_id = $1 AND p.part_type = $2
                ORDER BY p.unit_price, p.sku LIMIT 1
                """,
                model, service["part_type"],
            )
            if part is None:
                raise fail("no_compatible_part", f"no {service['part_type']} part fits this model",
                           service_code=code, part_type=service["part_type"])
    total = service["labour_fee"] + (part["unit_price"] if part else 0)
    return dumps({
        "service_code": service["code"],
        "service_name": service["name"],
        "model_id": model,
        "requires_visit": service["requires_visit"],
        "part": {"sku": part["sku"], "name": part["name"], "price": part["unit_price"]} if part else None,
        "labour_fee": service["labour_fee"],
        "total": total,
    })


if __name__ == "__main__":
    serve(mcp, PORT)
