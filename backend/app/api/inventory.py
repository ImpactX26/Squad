"""The inventory page's reads (ARCHITECTURE.md §7.8, §10): GET /api/inventory and
GET /api/restock-requests. Warehouse staff and admins (§11.2).

Read-only on purpose. Stock changes only through the inventory MCP server's writers, which the
workflows call in code (§4.2, router.WORKFLOW_ONLY_TOOLS); nothing here can change a quantity or
a restock request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_roles
from app.models import StaffUser
from app.schemas.inventory import (
    InventoryResponse,
    Reservation,
    RestockRef,
    RestockRequestList,
    RestockRequestOut,
    StockItem,
    UsageByType,
)

router = APIRouter(prefix="/api", tags=["inventory"])

WarehouseStaff = Annotated[StaffUser, Depends(require_roles("warehouse", "admin"))]
USAGE_DAYS = 7

_ITEMS = text("""
SELECT p.id AS part_id, p.sku, p.name, p.part_type, p.unit_price,
       w.id AS warehouse_id, w.name AS warehouse_name, w.city AS warehouse_city,
       i.qty_on_hand AS on_hand, i.qty_reserved AS reserved, i.qty_on_hand - i.qty_reserved AS available,
       i.reorder_threshold, i.reorder_qty, i.qty_on_hand - i.qty_reserved <= i.reorder_threshold AS low,
       i.updated_at, r.id AS restock_id, r.status AS restock_status, r.qty AS restock_qty,
       r.created_at AS restock_created_at
FROM inventory i
JOIN parts p ON p.id = i.part_id
JOIN warehouses w ON w.id = i.warehouse_id
LEFT JOIN LATERAL (
  SELECT id, status, qty, created_at FROM restock_requests rr
  WHERE rr.part_id = i.part_id AND rr.warehouse_id = i.warehouse_id AND rr.status IN ('open', 'ordered')
  ORDER BY rr.created_at DESC LIMIT 1
) r ON TRUE
ORDER BY low DESC, available, p.sku
""")

# What each ticket still holds: reserve +qty, release -qty, consume -qty (§5.7).
_RESERVATIONS = text("""
SELECT m.ticket_id, t.ticket_number, t.status AS ticket_status, p.sku, p.name AS part_name,
       w.name AS warehouse_name, SUM(m.change) AS qty, max(m.created_at) AS since,
       (SELECT j.status FROM service_jobs j WHERE j.ticket_id = m.ticket_id
        ORDER BY j.created_at DESC LIMIT 1) AS job_status
FROM inventory_movements m
JOIN tickets t ON t.id = m.ticket_id
JOIN parts p ON p.id = m.part_id
JOIN warehouses w ON w.id = m.warehouse_id
WHERE m.kind IN ('reserve', 'release', 'consume')
GROUP BY m.ticket_id, t.ticket_number, t.status, m.part_id, p.sku, p.name, m.warehouse_id, w.name
HAVING SUM(m.change) > 0
ORDER BY since DESC
""")

_USAGE = text("""
SELECT p.part_type, -SUM(m.change) AS used FROM inventory_movements m JOIN parts p ON p.id = m.part_id
WHERE m.kind = 'consume' AND m.created_at > now() - make_interval(days => :days)
GROUP BY p.part_type ORDER BY used DESC, p.part_type
""")

_RESTOCK = text("""
SELECT r.id, p.sku, p.name AS part_name, p.part_type, w.name AS warehouse_name, r.qty, r.reason, r.status,
       r.created_at, i.qty_on_hand - i.qty_reserved AS available_now
FROM restock_requests r
JOIN parts p ON p.id = r.part_id
JOIN warehouses w ON w.id = r.warehouse_id
LEFT JOIN inventory i ON i.part_id = r.part_id AND i.warehouse_id = r.warehouse_id
ORDER BY r.status IN ('open', 'ordered') DESC, r.created_at DESC
LIMIT 100
""")


@router.get("/inventory", response_model=InventoryResponse)
async def inventory(staff: WarehouseStaff, session: Annotated[AsyncSession, Depends(get_session)]) -> InventoryResponse:
    """Stock by warehouse (running low first), what tickets hold, and parts used this week (§7.8)."""
    items = (await session.execute(_ITEMS)).mappings().all()
    held = (await session.execute(_RESERVATIONS)).mappings().all()
    usage = (await session.execute(_USAGE, {"days": USAGE_DAYS})).mappings().all()
    return InventoryResponse(
        items=[StockItem(
            **{k: row[k] for k in ("part_id", "sku", "name", "part_type", "warehouse_id", "warehouse_name",
                                   "warehouse_city", "on_hand", "reserved", "available", "reorder_threshold",
                                   "reorder_qty", "low", "updated_at")},
            unit_price=f"{row['unit_price']:.2f}",
            restock=RestockRef(id=row["restock_id"], status=row["restock_status"], qty=row["restock_qty"],
                               created_at=row["restock_created_at"]) if row["restock_id"] else None,
        ) for row in items],
        reservations=[Reservation(**dict(row)) for row in held],
        usage_days=USAGE_DAYS,
        usage=[UsageByType(part_type=row["part_type"], used=int(row["used"])) for row in usage],
    )


@router.get("/restock-requests", response_model=RestockRequestList)
async def restock_requests(staff: WarehouseStaff,
                           session: Annotated[AsyncSession, Depends(get_session)]) -> RestockRequestList:
    """Restock requests, open and ordered first (§7.8). Changing their status has no route in §10."""
    rows = (await session.execute(_RESTOCK)).mappings().all()
    return RestockRequestList(requests=[RestockRequestOut(**dict(row)) for row in rows])