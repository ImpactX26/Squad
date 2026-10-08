import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[uuid.UUID] = uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("parts.id"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    qty_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    qty_reserved: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reorder_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    reorder_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))