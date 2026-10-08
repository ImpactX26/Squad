"""Response models for the inventory page (ARCHITECTURE.md §7.8, §10, §11.2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RestockRef(BaseModel):
    id: uuid.UUID
    status: str
    qty: int
    created_at: datetime


class StockItem(BaseModel):
    """One part in one warehouse. available = on_hand - reserved (§5.7)."""

    part_id: uuid.UUID
    sku: str
    name: str
    part_type: str
    unit_price: str = Field(description='Two decimals, e.g. "5.40"')
    warehouse_id: uuid.UUID
    warehouse_name: str
    warehouse_city: str
    on_hand: int
    reserved: int
    available: int
    reorder_threshold: int
    reorder_qty: int
    low: bool = Field(description="available <= reorder_threshold (§7.8)")
    updated_at: datetime
    restock: RestockRef | None = Field(description="The open or ordered restock request for this part, if any")


class Reservation(BaseModel):
    """A part a ticket still holds: the sum of its reserve, release and consume movements (§5.7)."""

    ticket_id: uuid.UUID
    ticket_number: str
    ticket_status: str
    sku: str
    part_name: str
    warehouse_name: str
    qty: int
    since: datetime
    job_status: str | None


class UsageByType(BaseModel):
    part_type: str
    used: int


class InventoryResponse(BaseModel):
    items: list[StockItem]
    reservations: list[Reservation]
    usage_days: int
    usage: list[UsageByType] = Field(description="Parts consumed on completed jobs, by type, in the last usage_days")


class RestockRequestOut(BaseModel):
    id: uuid.UUID
    sku: str
    part_name: str
    part_type: str
    warehouse_name: str
    qty: int
    reason: str | None
    status: str
    created_at: datetime
    available_now: int | None


class RestockRequestList(BaseModel):
    requests: list[RestockRequestOut]