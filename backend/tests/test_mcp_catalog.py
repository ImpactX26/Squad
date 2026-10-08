"""Smoke tests for the catalog MCP server (:8102), one or more per tool, over the MCP protocol in-process."""

import uuid

from mcp import Client

from mcp_servers.catalog_server import mcp
from tests.mcp_support import error_json, pool, result_json, rows  # noqa: F401 (fixtures)


# ---------- lookup_serial ----------

async def test_lookup_serial(rows):
    async with Client(mcp) as client:
        found = result_json(await client.call_tool("lookup_serial", {"serial_number": f" {rows['serial'].lower()} "}))
        unowned = result_json(await client.call_tool("lookup_serial", {"serial_number": rows["unowned_serial"]}))
        missing = result_json(await client.call_tool("lookup_serial", {"serial_number": "NOPE-000000"}))
    assert found["found"] is True and found["product_id"] == rows["product_id"]
    assert found["model_number"] == rows["model_number"] and found["color"] == "silver"
    assert found["warranty"]["status"] == "in_warranty"
    assert found["owner_customer_id"] == rows["customer_id"]
    assert unowned["owner_customer_id"] is None and unowned["warranty"]["status"] == "out_of_warranty"
    assert missing == {"found": False, "serial_number": "NOPE-000000"}


# ---------- lookup_model ----------

async def test_lookup_model(rows):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("lookup_model", {"model_number": rows["model_number"]}))
        missing = result_json(await client.call_tool("lookup_model", {"model_number": "NOPE"}))
    assert out["found"] is True and out["model_id"] == rows["model_id"] and out["specs"] == {"ram_gb": 16}
    assert [p["unit_price"] for p in out["compatible_parts"]] == ["5.40", "7.10"]
    assert missing["found"] is False


# ---------- get_customer_products ----------

async def test_get_customer_products(rows):
    async with Client(mcp) as client:
        out = result_json(await client.call_tool("get_customer_products", {"customer_id": rows["customer_id"]}))
        none = result_json(await client.call_tool("get_customer_products",
                                                  {"customer_id": rows["other_customer_id"]}))
        bad = await client.call_tool("get_customer_products", {"customer_id": "not-a-uuid"})
    assert [p["serial_number"] for p in out["products"]] == [rows["serial"]]
    assert none["products"] == []
    assert error_json(bad)["error"] == "bad_id"


# ---------- link_product_to_customer ----------

async def test_link_product_to_customer(rows, pool):
    async with Client(mcp) as client:
        linked = result_json(await client.call_tool("link_product_to_customer", {
            "product_id": rows["unowned_id"], "customer_id": rows["other_customer_id"]}))
        again = result_json(await client.call_tool("link_product_to_customer", {
            "product_id": rows["unowned_id"], "customer_id": rows["other_customer_id"]}))
        taken = await client.call_tool("link_product_to_customer", {
            "product_id": rows["product_id"], "customer_id": rows["other_customer_id"]})
        no_customer = await client.call_tool("link_product_to_customer", {
            "product_id": rows["unowned_id"], "customer_id": str(uuid.uuid4())})
    assert linked["already_linked"] is False and again["already_linked"] is True
    assert error_json(taken)["error"] == "owned_by_another"
    assert error_json(no_customer)["error"] == "not_found"
    async with pool.acquire() as conn:
        owner = await conn.fetchval("SELECT customer_id FROM products WHERE id = $1", uuid.UUID(rows["product_id"]))
    assert str(owner) == rows["customer_id"]  # a device someone else owns is never moved


# ---------- get_service_price ----------

async def test_get_service_price(rows):
    async with Client(mcp) as client:
        battery = result_json(await client.call_tool("get_service_price", {
            "service_code": rows["battery_service"].lower(), "model_id": rows["model_id"]}))
        software = result_json(await client.call_tool("get_service_price", {
            "service_code": rows["os_service"], "model_id": rows["model_id"]}))
        no_part = await client.call_tool("get_service_price", {
            "service_code": rows["ram_service"], "model_id": rows["model_id"]})
        unknown = await client.call_tool("get_service_price", {
            "service_code": "NOPE", "model_id": rows["model_id"]})
    assert battery["part"] == {"sku": rows["cheap_sku"], "name": "Battery 70Wh", "price": "5.40"}
    assert battery["labour_fee"] == "1.50" and battery["total"] == "6.90"
    assert software["part"] is None and software["total"] == "2.00"
    assert error_json(no_part)["error"] == "no_compatible_part"
    assert error_json(unknown)["error"] == "unknown_service"
