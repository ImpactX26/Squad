import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, uuid_pk


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = uuid_pk()
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    service_code: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    line_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    # pending | verifying | paid | failed | expired | cancelled | refunded
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'upi_utr'"))
    provider_ref: Mapped[str | None] = mapped_column(Text)
    public_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, unique=True)
    utr: Mapped[str | None] = mapped_column(Text, unique=True)
    utr_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    utr_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[str | None] = mapped_column(Text)  # 'bank_alert' or a staff user id
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("addresses.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class BankAlert(Base):
    """One forwarded bank SMS (§7.6). The SMS text itself is never stored, only its hash."""

    __tablename__ = "bank_alerts"

    id: Mapped[uuid.UUID] = uuid_pk()
    gmail_message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    utr: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    raw_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    matched_payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("payments.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    