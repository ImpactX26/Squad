import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, uuid_pk


class ProductModel(Base):
    __tablename__ = "product_models"

    id: Mapped[uuid.UUID] = uuid_pk()
    model_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)  # laptop | desktop | headphones | accessory
    specs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    warranty_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("12"))
    image_url: Mapped[str | None] = mapped_column(Text)


class Product(Base):
    """One physical unit."""

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = uuid_pk()
    serial_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product_models.id"), nullable=False)
    color: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    warranty_until: Mapped[date | None] = mapped_column(Date)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    created_at: Mapped[datetime] = created_at()


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    part_type: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    specs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))


class PartCompatibility(Base):
    __tablename__ = "part_compatibility"

    part_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parts.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_models.id", ondelete="CASCADE"), primary_key=True
    )


class ServiceCatalog(Base):
    __tablename__ = "service_catalog"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    part_type: Mapped[str | None] = mapped_column(Text)  # null for software-only services
    labour_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    requires_visit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    required_skill: Mapped[str | None] = mapped_column(Text)