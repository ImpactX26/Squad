"""inventory MCP server (ARCHITECTURE.md §5.7) — :8107.

Spare parts per warehouse: check stock, find the part for a model, reserve it when a repair is
booked, consume it when the job is completed, release it when the job is cancelled, and the
low-stock restock request (§7.8). available = qty_on_hand - qty_reserved.

Every stock change is one transaction: the inventory row is locked, the change itself is a single
guarded UPDATE that can never take a quantity below zero (the table's CHECK (qty >= 0) is the
backstop), and the movement is written to inventory_movements in the same transaction.

For reserve, release and consume, inventory_movements.change is what that movement did to the
ticket's reservation: reserve +qty, release -qty, consume -qty (a consume also takes qty off
qty_on_hand). Summed per ticket, part and warehouse they are what the ticket still holds, so a
ticket can only release or consume what it reserved.

reserve_part, consume_part, release_part and create_restock_request are in
router.MODEL_FORBIDDEN_TOOLS: only the workflows and the job API call them, in code (§4.2).
"""

import logging
import uuid
from typing import Any

import asyncpg
from mcp.server.mcpserver import MCPServer

from mcp_servers import run
from mcp_servers.common import db, events

log = logging.getLogger(__name__)
mcp = MCPServer(name="inventory", instructions=__doc__)

MAX_QTY = 10  # per call; a repair takes one part

_STOCK = """
SELECT i.part_id, i.warehouse_id, w.name AS warehouse_name, w.city AS warehouse_city,
       i.qty_on_hand AS on_hand, i.qty_reserved AS reserved, i.qty_on_hand - i.qty_reserved AS available,
       i.reorder_threshold, i.reorder_qty,
       i.qty_on_hand - i.qty_reserved <= i.reorder_threshold AS low
FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id
"""

# What a ticket still holds of a part in a warehouse (see the module docstring).
_HELD = """
SELECT COALESCE(SUM(change), 0) FROM inventory_movements
WHERE ticket_id = $1 AND part_id = $2 AND warehouse_id = $3 AND kind IN ('reserve', 'release', 'consume')
"""


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _refused(error: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, **extra}


def _bad_qty(qty: Any) -> bool:
    return isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY


async def _part(conn: asyncpg.Connection, part_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT id, sku, name, part_type, unit_price FROM parts WHERE id = $1", part_id)


async def _stock_of(part_ids: list[Any]) -> dict[str, list[dict[str, Any]]]:
    rows = await db.fetch(_STOCK + " WHERE i.part_id = ANY($1::uuid[]) ORDER BY w.name", part_ids)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in db.rows_to_list(rows):
        out.setdefault(row.pop("part_id"), []).append(row)
    return out


def _part_out(part: dict[str, Any], stock: list[dict[str, Any]]) -> dict[str, Any]:
    return {**part, "unit_price": f"{part['unit_price']:.2f}", "stock": stock,
            "available": sum(s["available"] for s in stock)}


# ---------- §5.7 tools ----------


@mcp.tool()
async def check_stock(part_id: str | None = None, part_type: str | None = None,
                      model_id: str | None = None, sku: str | None = None) -> dict[str, Any]:
    """On hand, reserved and available per warehouse: for one part (by part_id or SKU such as BAT-AX14), or for the parts of a type that fit a model.

    part_id wins over sku when both are given.
    """
    if part_id:
        pid = _uuid(part_id)
        rows = await db.fetch("SELECT id AS part_id, sku, name, part_type, unit_price FROM parts WHERE id = $1", pid) \
            if pid else []
    elif sku and sku.strip():
        rows = await db.fetch(
            "SELECT id AS part_id, sku, name, part_type, unit_price FROM parts WHERE upper(sku) = upper($1)",
            sku.strip())
    elif part_type and model_id and _uuid(model_id):
        rows = await db.fetch(
            "SELECT p.id AS part_id, p.sku, p.name, p.part_type, p.unit_price FROM parts p"
            " JOIN part_compatibility pc ON pc.part_id = p.id"
            " WHERE pc.model_id = $1 AND p.part_type = $2 ORDER BY p.unit_price, p.sku", _uuid(model_id), part_type)
    else:
        return _refused("bad_request", "give part_id, sku, or part_type and model_id")
    parts = db.rows_to_list(rows)
    stock = await _stock_of([p["part_id"] for p in parts])
    return {"ok": True, "found": bool(parts), "parts": [_part_out(p, stock.get(p["part_id"], [])) for p in parts]}


@mcp.tool()
async def find_compatible_part(model_id: str, part_type: str) -> dict[str, Any]:
    """The part of this type that fits this model, with its stock per warehouse.

    The cheapest compatible part (by SKU on a tie): the same one a payment bills (§5.5), so the part
    a customer paid for is the part that is reserved.
    """
    mid = _uuid(model_id)
    if mid is None:
        return {"found": False, "error": "invalid_id"}
    row = await db.fetchrow(
        "SELECT p.id AS part_id, p.sku, p.name, p.part_type, p.unit_price FROM parts p"
        " JOIN part_compatibility pc ON pc.part_id = p.id"
        " WHERE pc.model_id = $1 AND p.part_type = $2 ORDER BY p.unit_price, p.sku LIMIT 1", mid, part_type)
    if row is None:
        return {"found": False, "model_id": mid, "part_type": part_type}
    part = db.row_to_dict(row)
    stock = await _stock_of([part["part_id"]])
    return {"found": True, **_part_out(part, stock.get(part["part_id"], []))}


@mcp.tool()
async def reserve_part(part_id: str, warehouse_id: str, qty: int, ticket_id: str) -> dict[str, Any]:
    """Hold qty of a part in a warehouse for a ticket when its repair is booked (§7.8).

    One guarded UPDATE: refused, with nothing changed, when fewer than qty are available. Then the
    low-stock check: `restock_due` is true when this reservation took available from above the part's
    reorder_threshold to at or below it, and the caller files the request with create_restock_request.
    """
    pid, wid, tid = _uuid(part_id), _uuid(warehouse_id), _uuid(ticket_id)
    if not (pid and wid and tid):
        return _refused("invalid_id", "part_id, warehouse_id and ticket_id must be UUIDs")
    if _bad_qty(qty):
        return _refused("invalid_qty", f"qty must be 1-{MAX_QTY}")
    async with db.acquire() as conn:
        async with conn.transaction():
            part = await _part(conn, pid)
            if part is None:
                return _refused("part_not_found", "no such part")
            if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tickets WHERE id = $1)", tid):
                return _refused("ticket_not_found", "no such ticket")
            row = await conn.fetchrow(
                "UPDATE inventory SET qty_reserved = qty_reserved + $3, updated_at = now()"
                " WHERE part_id = $1 AND warehouse_id = $2 AND qty_on_hand - qty_reserved >= $3"
                " RETURNING qty_on_hand, qty_reserved, reorder_threshold, reorder_qty", pid, wid, qty)
            if row is None:
                available = await conn.fetchval(
                    "SELECT qty_on_hand - qty_reserved FROM inventory WHERE part_id = $1 AND warehouse_id = $2", pid, wid)
                if available is None:
                    return _refused("not_stocked", f"{part['sku']} isn't stocked in that warehouse")
                return _refused("out_of_stock", f"only {available} of {part['sku']} available", available=available)
            movement = await conn.fetchval(
                "INSERT INTO inventory_movements (part_id, warehouse_id, change, kind, ticket_id)"
                " VALUES ($1, $2, $3, 'reserve', $4) RETURNING id", pid, wid, qty, tid)
    available = row["qty_on_hand"] - row["qty_reserved"]
    threshold = row["reorder_threshold"]
    # The low-stock alert fires once per drop: when this booking takes available from above the threshold
    # to at or below it. Further bookings while it's low don't repeat it, and once stock is back above the
    # threshold the next drop fires again (§7.8).
    crossed = available + qty > threshold >= available
    return {"ok": True, "part_id": pid, "sku": part["sku"], "name": part["name"], "warehouse_id": wid, "qty": qty,
            "ticket_id": tid, "on_hand": row["qty_on_hand"], "reserved": row["qty_reserved"],
            "available": available, "reorder_threshold": threshold, "reorder_qty": row["reorder_qty"],
            "low_stock": available <= threshold, "restock_due": crossed, "movement_id": str(movement)}


@mcp.tool()
async def release_part(part_id: str, warehouse_id: str, qty: int, ticket_id: str,
                       job_id: str | None = None) -> dict[str, Any]:
    """Give back a ticket's reservation, e.g. when its job is cancelled (§7.8).

    Only what this ticket holds can be released; refused, with nothing changed, otherwise.
    """
    pid, wid, tid = _uuid(part_id), _uuid(warehouse_id), _uuid(ticket_id)
    jid = _uuid(job_id) if job_id else None
    if not (pid and wid and tid) or (job_id and jid is None):
        return _refused("invalid_id", "part_id, warehouse_id, ticket_id (and job_id) must be UUIDs")
    if _bad_qty(qty):
        return _refused("invalid_qty", f"qty must be 1-{MAX_QTY}")
    async with db.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT id FROM inventory WHERE part_id = $1 AND warehouse_id = $2 FOR UPDATE", pid, wid)
            if locked is None:
                return _refused("not_stocked", "that part isn't stocked in that warehouse")
            held = await conn.fetchval(_HELD, tid, pid, wid)
            if held < qty:
                return _refused("not_reserved", f"the ticket holds {held} of this part here", held=held)
            row = await conn.fetchrow(
                "UPDATE inventory SET qty_reserved = qty_reserved - $2, updated_at = now()"
                " WHERE id = $1 AND qty_reserved >= $2 RETURNING qty_on_hand, qty_reserved", locked["id"], qty)
            if row is None:
                return _refused("not_reserved", "nothing reserved to release")
            await conn.execute(
                "INSERT INTO inventory_movements (part_id, warehouse_id, change, kind, ticket_id, job_id)"
                " VALUES ($1, $2, $3, 'release', $4, $5)", pid, wid, -qty, tid, jid)
    return {"ok": True, "part_id": pid, "warehouse_id": wid, "qty": qty, "ticket_id": tid,
            "on_hand": row["qty_on_hand"], "reserved": row["qty_reserved"],
            "available": row["qty_on_hand"] - row["qty_reserved"]}


@mcp.tool()
async def consume_part(part_id: str, warehouse_id: str, qty: int, job_id: str) -> dict[str, Any]:
    """Use the part a completed job reserved: on hand and reserved both go down by qty (§7.8).

    Then the low-stock check: `restock_due` is true when available is at or below the reorder
    threshold and no restock request for the part is open, and the caller files one with
    create_restock_request.
    """
    pid, wid, jid = _uuid(part_id), _uuid(warehouse_id), _uuid(job_id)
    if not (pid and wid and jid):
        return _refused("invalid_id", "part_id, warehouse_id and job_id must be UUIDs")
    if _bad_qty(qty):
        return _refused("invalid_qty", f"qty must be 1-{MAX_QTY}")
    async with db.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT ticket_id FROM service_jobs WHERE id = $1", jid)
            if job is None:
                return _refused("job_not_found", "no such job")
            locked = await conn.fetchrow(
                "SELECT id FROM inventory WHERE part_id = $1 AND warehouse_id = $2 FOR UPDATE", pid, wid)
            if locked is None:
                return _refused("not_stocked", "that part isn't stocked in that warehouse")
            held = await conn.fetchval(_HELD, job["ticket_id"], pid, wid)
            if held < qty:
                return _refused("not_reserved", f"the job's ticket holds {held} of this part here", held=held)
            row = await conn.fetchrow(
                "UPDATE inventory SET qty_on_hand = qty_on_hand - $2, qty_reserved = qty_reserved - $2,"
                " updated_at = now() WHERE id = $1 AND qty_reserved >= $2 AND qty_on_hand >= $2"
                " RETURNING qty_on_hand, qty_reserved, reorder_threshold, reorder_qty", locked["id"], qty)
            if row is None:
                return _refused("not_reserved", "nothing reserved to consume")
            await conn.execute(
                "INSERT INTO inventory_movements (part_id, warehouse_id, change, kind, ticket_id, job_id)"
                " VALUES ($1, $2, $3, 'consume', $4, $5)", pid, wid, -qty, job["ticket_id"], jid)
            part = await _part(conn, pid)
            open_request = await conn.fetchval(
                "SELECT id FROM restock_requests WHERE part_id = $1 AND status IN ('open', 'ordered') LIMIT 1", pid)
    available = row["qty_on_hand"] - row["qty_reserved"]
    low = available <= row["reorder_threshold"]
    return {"ok": True, "part_id": pid, "sku": part["sku"], "name": part["name"], "warehouse_id": wid, "qty": qty,
            "job_id": jid, "ticket_id": str(job["ticket_id"]), "on_hand": row["qty_on_hand"],
            "reserved": row["qty_reserved"], "available": available, "reorder_threshold": row["reorder_threshold"],
            "reorder_qty": row["reorder_qty"], "low_stock": low, "restock_due": low and open_request is None,
            "open_restock_request_id": str(open_request) if open_request else None}


@mcp.tool()
async def create_restock_request(part_id: str, warehouse_id: str, qty: int, reason: str) -> dict[str, Any]:
    """The low-stock report (§7.8): a restock request and the stock.low event.

    At most one open restock request per part: while one is open (or ordered), it is returned and
    nothing new is created. The workflow emails WAREHOUSE_ALERT_EMAIL and notifies admins when one is.
    """
    pid, wid = _uuid(part_id), _uuid(warehouse_id)
    if not (pid and wid):
        return _refused("invalid_id", "part_id and warehouse_id must be UUIDs")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 1000:
        return _refused("invalid_qty", "qty must be 1-1000")
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('restock:' || $1))", pid)
            existing = await conn.fetchrow(
                "SELECT id, qty, status, created_at FROM restock_requests"
                " WHERE part_id = $1 AND status IN ('open', 'ordered') ORDER BY created_at LIMIT 1", pid)
            if existing is not None:
                return {"ok": True, "created": False, "restock_request_id": str(existing["id"]),
                        "status": existing["status"], "qty": existing["qty"]}
            stock = await conn.fetchrow(
                _STOCK + " WHERE i.part_id = $1 AND i.warehouse_id = $2", pid, wid)
            part = await _part(conn, pid)
            if stock is None or part is None:
                return _refused("not_stocked", "that part isn't stocked in that warehouse")
            request = await conn.fetchrow(
                "INSERT INTO restock_requests (part_id, warehouse_id, qty, reason) VALUES ($1, $2, $3, $4)"
                " RETURNING id, created_at", pid, wid, qty, (reason or "")[:500] or None)
    out = {
        "ok": True, "created": True, "restock_request_id": str(request["id"]), "status": "open",
        "part_id": pid, "sku": part["sku"], "name": part["name"], "warehouse_id": wid,
        "warehouse_name": stock["warehouse_name"], "qty": qty, "on_hand": stock["on_hand"],
        "reserved": stock["reserved"], "available": stock["available"],
        "reorder_threshold": stock["reorder_threshold"], "reason": reason,
        "created_at": request["created_at"].isoformat(),
    }
    await events.publish("stock.low", {k: out[k] for k in (
        "restock_request_id", "part_id", "sku", "name", "warehouse_id", "warehouse_name", "qty", "on_hand",
        "reserved", "available", "reorder_threshold", "reason")})
    return out


PART_TYPES = ("battery", "cmos_battery", "ram", "ssd", "charger", "keyboard", "display", "fan", "ear_cushion",
              "cable", "other")
MAX_STOCK_ROWS = 60


@mcp.tool()
async def list_stock(part_type: str | None = None, low_only: bool = False, limit: int = 25) -> dict[str, Any]:
    """The whole warehouse, or one part type: every part's stock with totals; running-low rows first. Read-only.

    part_type: battery, cmos_battery, ram, ssd, charger, keyboard, display, fan, ear_cushion, cable, other.
    """
    if part_type is not None and part_type.strip():
        part_type = _part_type(part_type)
        if part_type is None:
            return _refused("bad_part_type", f"part_type must be one of {', '.join(PART_TYPES)}")
    else:
        part_type = None
    limit = max(1, min(int(limit), MAX_STOCK_ROWS))
    rows = db.rows_to_list(await db.fetch(
        "SELECT p.sku, p.name, p.part_type, w.name AS warehouse, i.qty_on_hand AS on_hand,"
        " i.qty_reserved AS reserved, i.qty_on_hand - i.qty_reserved AS available, i.reorder_threshold AS threshold,"
        " i.qty_on_hand - i.qty_reserved <= i.reorder_threshold AS low,"
        " EXISTS (SELECT 1 FROM restock_requests r WHERE r.part_id = p.id AND r.status IN ('open','ordered'))"
        "   AS restock_requested"
        " FROM inventory i JOIN parts p ON p.id = i.part_id JOIN warehouses w ON w.id = i.warehouse_id"
        " WHERE ($1::text IS NULL OR p.part_type = $1) AND (NOT $2 OR i.qty_on_hand - i.qty_reserved <= i.reorder_threshold)"
        " ORDER BY low DESC, available, p.sku", part_type, low_only))
    by_type: dict[str, dict[str, int]] = {}
    for r in rows:
        t = by_type.setdefault(r["part_type"], {"parts": 0, "available": 0, "low": 0})
        t["parts"] += 1
        t["available"] += r["available"]
        t["low"] += int(r["low"])
    totals = {"parts": len(rows), "on_hand": sum(r["on_hand"] for r in rows),
              "reserved": sum(r["reserved"] for r in rows), "available": sum(r["available"] for r in rows),
              "low": sum(int(r["low"]) for r in rows)}
    # Compact rows, so the whole warehouse fits one tool result (runtime.MAX_TOOL_RESULT_CHARS).
    shown = [{"sku": r["sku"], "name": r["name"], "on_hand": r["on_hand"], "reserved": r["reserved"],
              "available": r["available"], "threshold": r["threshold"], "low": r["low"],
              **({"restock_requested": True} if r["restock_requested"] else {})} for r in rows[:limit]]
    return {"ok": True, "warehouses": sorted({r["warehouse"] for r in rows}), "part_type": part_type,
            "low_only": low_only, "totals": totals,
            "by_part_type": [{"part_type": k, **v} for k, v in sorted(by_type.items())],
            "rows": shown, "rows_total": len(rows), "rows_shown": len(shown)}


def _part_type(text: str) -> str | None:
    """"Batteries", "ear cushions", "SSD" -> battery, ear_cushion, ssd; None when it isn't a part type."""
    word = "_".join(text.strip().lower().replace("-", " ").split())
    for candidate in (word, word[:-3] + "y" if word.endswith("ies") else word, word.removesuffix("s")):
        if candidate in PART_TYPES:
            return candidate
    return None


@mcp.tool()
async def usage_report(days: int = 7) -> dict[str, Any]:
    """Parts used (consumed) in the last N days, by type and by part, and what is running low."""
    days = max(1, min(int(days), 365))
    by_type = await db.fetch(
        "SELECT p.part_type, -SUM(m.change) AS used FROM inventory_movements m JOIN parts p ON p.id = m.part_id"
        " WHERE m.kind = 'consume' AND m.created_at > now() - make_interval(days => $1)"
        " GROUP BY p.part_type ORDER BY used DESC, p.part_type", days)
    by_part = await db.fetch(
        "SELECT p.sku, p.name, p.part_type, -SUM(m.change) AS used FROM inventory_movements m"
        " JOIN parts p ON p.id = m.part_id"
        " WHERE m.kind = 'consume' AND m.created_at > now() - make_interval(days => $1)"
        " GROUP BY p.sku, p.name, p.part_type ORDER BY used DESC, p.sku", days)
    low = await db.fetch(
        "SELECT p.sku, p.name, w.name AS warehouse_name, i.qty_on_hand AS on_hand, i.qty_reserved AS reserved,"
        " i.qty_on_hand - i.qty_reserved AS available, i.reorder_threshold"
        " FROM inventory i JOIN parts p ON p.id = i.part_id JOIN warehouses w ON w.id = i.warehouse_id"
        " WHERE i.qty_on_hand - i.qty_reserved <= i.reorder_threshold ORDER BY available, p.sku")
    return {"days": days, "by_part_type": db.rows_to_list(by_type), "by_part": db.rows_to_list(by_part),
            "running_low": db.rows_to_list(low)}


if __name__ == "__main__":
    run(mcp, "inventory")