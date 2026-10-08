"""Request and response models for the payments page (ARCHITECTURE.md §7.6, §10, §11.2)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

NOTE = Field(min_length=3, max_length=500, description="What the admin checked, e.g. 'UTR seen on the bank statement'")


class PaymentRow(BaseModel):
    """One payment, as the list and the drawer show it. Money as two-decimal strings."""

    payment_id: uuid.UUID
    ticket_id: uuid.UUID
    ticket_number: str
    invoice_number: str | None
    customer_id: uuid.UUID
    customer_name: str | None
    customer_email: str | None
    service_code: str
    service_name: str | None
    amount: str = Field(description='Two decimals, e.g. "6.90"')
    currency: str
    status: str = Field(description="pending | verifying | paid | failed | expired | cancelled | refunded")
    utr: str | None
    utr_submitted_at: datetime | None
    utr_attempts_left: int
    needs_review: bool
    verified_by: str | None = Field(description="'bank_alert', or the id of the admin who verified it")
    verified_by_label: str | None = Field(description='"bank alert", or the admin\'s name')
    pay_url: str
    expires_at: datetime
    created_at: datetime
    paid_at: datetime | None


class ServiceOption(BaseModel):
    code: str
    name: str


class PaymentListResponse(BaseModel):
    payments: list[PaymentRow]
    total: int
    limit: int
    offset: int
    services: list[ServiceOption] = Field(description="The service catalog, for the New payment request form")


class PaymentEvent(BaseModel):
    """A ticket timeline event about this payment."""

    id: uuid.UUID
    type: str
    actor: str
    actor_name: str | None = Field(description="The staff member's name when the actor is one")
    payload: dict[str, Any]
    created_at: datetime


class BankAlertOut(BaseModel):
    """One bank_alerts row (never the SMS text, §7.6) and the payment it was reconciled against."""

    id: uuid.UUID
    sender: str
    added_by: str | None = Field(description="The admin who pasted it on the payments page; null for a forwarded mail")
    utr: str | None
    amount: str | None
    parsed_ok: bool
    reject_reason: str | None
    matched_payment_id: uuid.UUID | None
    matched_invoice_number: str | None
    matched_ticket_id: uuid.UUID | None
    matched_ticket_number: str | None
    received_at: datetime
    processed_at: datetime | None


class BankAlertList(BaseModel):
    alerts: list[BankAlertOut]


class LineItem(BaseModel):
    kind: str
    label: str
    amount: str


class PaymentDetail(PaymentRow):
    """The drawer: the row, its line items, its timeline events, and the bank alerts for its UTR."""

    line_items: list[LineItem]
    events: list[PaymentEvent]
    bank_alerts: list[BankAlertOut]


class PaymentCreate(BaseModel):
    """The ticket by id or by number (SR-2026-00042), one of the two."""

    ticket_id: uuid.UUID | None = None
    ticket_number: str | None = Field(default=None, max_length=40)
    service_code: str = Field(min_length=2, max_length=40, description="A service_catalog code, e.g. BATTERY_REPLACE")
    note: str = NOTE

    @model_validator(mode="after")
    def one_ticket(self) -> "PaymentCreate":
        if (self.ticket_id is None) == (not (self.ticket_number or "").strip()):
            raise ValueError("give ticket_id or ticket_number")
        return self


class PaymentCreated(BaseModel):
    payment: PaymentRow
    told_customer_on: str | None = Field(description="The channel the link was sent on; null when there is no chat")
    emailed_to: str | None


class PaymentPatch(BaseModel):
    utr: str | None = Field(default=None, max_length=40, description="The corrected 12-digit UTR")
    expires_in_minutes: int | None = Field(default=None, description="Give the link this long from now (5-10080)")
    note: str = NOTE


class PaymentNote(BaseModel):
    note: str = NOTE


class BankAlertCreate(BaseModel):
    sms: str = Field(min_length=10, max_length=2000, description="The bank's SMS, pasted as received")
    sender: str | None = Field(default=None, max_length=120, description="Who sent the SMS, e.g. VM-HDFCBK")
    subject: str | None = Field(default=None, max_length=200)
    note: str = NOTE


class BankAlertResult(BaseModel):
    bank_alert_id: uuid.UUID | None
    parsed_ok: bool
    reject_reason: str | None
    utr: str | None
    amount: str | None
    match: str | None = Field(description="paid | amount_mismatch | waiting_for_utr | duplicate | not_verifying")
    payment_id: uuid.UUID | None
    invoice_number: str | None