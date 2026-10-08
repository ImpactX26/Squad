"""The public payment API behind /pay/[token] (ARCHITECTURE.md §7.6, §10).

No login: the 32-character token from secrets.token_urlsafe(24) is the only key, so every route is
rate-limited per token and per client IP, and a malformed token never reaches the database.

    GET  /api/pay/{token}         the invoice, the UPI ID, payee, amount, and the upi://pay string
    POST /api/pay/{token}/utr     {utr}: the customer's 12-digit UTR -> payments.submit_utr, then an
                                  immediate match against bank alerts that are already in
    GET  /api/pay/{token}/status  what the page polls while the payment is verified

Nothing here can mark a payment paid. The UTR is stored by the payments MCP server; only a
matching bank alert (app/payments/upi_verifier.py) or an admin's mark_paid_manually pays it.

The limits are in memory, which is right for the one API process this runs as (§3). The pay page
reaches these routes through the web server's /api/pay proxy (web/next.config.ts), so the address
seen here is 127.0.0.1, unless the request carried X-Forwarded-For: uvicorn trusts that header from
127.0.0.1 (its default) and keys on the rightmost address in it, which a tunnel normally sets to the
visitor's. Without one, every page shares the proxy's address, so the per-IP budgets are sized for
that: three pages polling every 3 s use half the status budget. Anyone who can reach the web server
directly can forge the header to dodge the per-IP limits, but not the per-token ones or the five-UTR
cap on a link, and a 32-character random token can't be found by scanning anyway.
"""

import logging
import time
from collections import deque
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.brain.mcp_hub import McpToolError, hub
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.payments.invoice import load_invoice, upi_link
from app.payments.money import MAX_UTR_ATTEMPTS, PUBLIC_TOKEN, UTR, format_ist
from app.payments.upi_verifier import match_for_utr

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pay", tags=["pay"])

# What the page tells the customer for each status. The amounts and reasons stay on the staff side.
STATUS_MESSAGES = {
    "pending": "Scan the QR code with any UPI app to pay, then enter the 12-digit UTR below.",
    "verifying": "Thanks! We're checking your payment with our bank. This page updates on its own.",
    "paid": "Payment received. Thank you!",
    "failed": "We couldn't match that payment to this invoice. If you paid a different amount, contact us; "
              "if you paid again for the exact amount, enter the new UTR.",
    "expired": "This payment link has expired. Please ask us for a new one.",
    "cancelled": "This invoice was cancelled.",
    "refunded": "This payment was refunded.",
}

# The page's HTTP codes for submit_utr's refusals.
REFUSAL_STATUS = {
    "invalid_utr": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "already_paid": status.HTTP_409_CONFLICT,
    "cancelled": status.HTTP_409_CONFLICT,
    "refunded": status.HTTP_409_CONFLICT,
    "utr_used": status.HTTP_409_CONFLICT,
    "expired": status.HTTP_410_GONE,
    "too_many_attempts": status.HTTP_429_TOO_MANY_REQUESTS,
}


# ---------- rate limiting ----------


class SlidingWindow:
    """At most `limit` hits per key in any `seconds`-long window."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, seconds: float = 60.0) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, now: float | None = None) -> float:
        """Record a hit. Returns 0 when allowed, else the seconds until the next one would be."""
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(0.0, self.seconds - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > self.MAX_KEYS:
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - self.seconds}
        return 0.0

    def clear(self) -> None:
        self._hits.clear()


# (per token, per client IP), hits per minute. The page polls status every 3 s while a UTR is
# verified (20 a minute): three phones polling, on one link or three, use half of either status
# limit, even when they all share the proxy's address.
LIMITS: dict[str, tuple[SlidingWindow, SlidingWindow]] = {
    "invoice": (SlidingWindow(30), SlidingWindow(60)),
    "utr": (SlidingWindow(10), SlidingWindow(20)),
    "status": (SlidingWindow(120), SlidingWindow(120)),
}


def _limit(route: str, token: str, request: Request, now: float | None = None) -> None:
    """Count this request against the IP first (so bad tokens count too), then the token."""
    per_token, per_ip = LIMITS[route]
    ip = request.client.host if request.client else "unknown"
    wait = per_ip.hit(f"{route}:{ip}", now)
    if not wait and PUBLIC_TOKEN.fullmatch(token):
        wait = per_token.hit(f"{route}:{token}", now)
    if wait:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests. Please wait a moment and try again.",
                            headers={"Retry-After": str(max(1, round(wait)))})
    if not PUBLIC_TOKEN.fullmatch(token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This payment link is not valid.")


# ---------- response shapes (the frontend's types come from these, §14.3) ----------


class PayLineItem(BaseModel):
    kind: str
    label: str
    amount: str = Field(description='Two decimals, e.g. "5400.00"')
    amount_display: str


class PayCustomer(BaseModel):
    full_name: str | None
    email: str | None
    phone: str | None


class PayAddress(BaseModel):
    line1: str
    line2: str | None
    city: str
    state: str | None
    postal_code: str | None
    text: str


class PayDevice(BaseModel):
    name: str | None
    model_number: str | None
    serial_number: str


class PayUpi(BaseModel):
    upi_id: str
    payee_name: str
    amount: str = Field(description='Two decimals, e.g. "6199.00"')
    uri: str = Field(description="upi://pay?pa=…&pn=…&am=…&cu=INR&tn=<ticket number>, for the QR code")


class PayInvoice(BaseModel):
    invoice_number: str
    invoice_date: str
    ticket_number: str
    problem_summary: str
    device: PayDevice | None
    customer: PayCustomer
    service_address: PayAddress | None
    service_name: str
    line_items: list[PayLineItem]
    total: str
    total_display: str
    currency: str
    status: str
    message: str
    expires_at: str
    expires_display: str
    utr: str | None
    utr_attempts_left: int
    paid_at: str | None
    paid_display: str | None
    upi: PayUpi | None = Field(description="null when UPI_ID / UPI_PAYEE_NAME aren't set")


class PayStatus(BaseModel):
    invoice_number: str
    status: str
    message: str
    utr_submitted: bool
    utr_attempts_left: int
    paid_at: str | None
    paid_display: str | None


class UtrIn(BaseModel):
    utr: str = Field(max_length=40, description="The 12-digit UTR / UPI reference number from the payment app")


class UtrOut(BaseModel):
    ok: bool
    status: str
    message: str
    utr_attempts_left: int


# ---------- routes ----------


@router.get("/{token}", response_model=PayInvoice)
async def get_invoice(token: str, request: Request,
                      settings: Annotated[Settings, Depends(get_settings)]) -> PayInvoice:
    """The invoice and how to pay it: UPI ID, payee, amount, and the upi://pay string for the QR."""
    _limit("invoice", token, request)
    invoice = await load_invoice(token=token, settings=settings)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This payment link is not valid.")
    uri = upi_link(invoice, settings)
    return PayInvoice(
        **{key: invoice[key] for key in (
            "invoice_number", "invoice_date", "ticket_number", "problem_summary", "device", "customer",
            "service_address", "line_items", "total", "total_display", "currency", "status", "expires_at",
            "expires_display", "utr", "paid_at", "paid_display")},
        service_name=invoice["service"]["name"],
        message=STATUS_MESSAGES.get(invoice["status"], ""),
        utr_attempts_left=max(0, MAX_UTR_ATTEMPTS - invoice["utr_attempts"]),
        upi=PayUpi(upi_id=settings.upi_id.strip(), payee_name=settings.upi_payee_name.strip(),
                   amount=invoice["total"], uri=uri) if uri else None,
    )


@router.post("/{token}/utr", response_model=UtrOut, responses={
    404: {"description": "Not a payment link"}, 409: {"description": "Paid, cancelled, or the UTR is used"},
    410: {"description": "The link expired"}, 422: {"description": "Not a 12-digit UTR"},
    429: {"description": "Too many attempts or requests"}, 503: {"description": "Payments unavailable"}})
async def submit_utr(token: str, body: UtrIn, request: Request) -> Any:
    """Store the customer's UTR (payments.submit_utr), then match it against bank alerts already in."""
    _limit("utr", token, request)
    utr = body.utr.strip()
    if not UTR.fullmatch(utr):
        # Refused here, before the payments server: a malformed UTR is not an attempt.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Enter the 12-digit UTR (UPI reference number) from your payment app.")
    try:
        result = await hub.call_tool("payments__submit_utr", {"token": token, "utr": utr})
    except McpToolError as e:
        log.error("submit_utr unavailable: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payments are briefly unavailable. Please try again in a minute.") from e
    if not isinstance(result, dict) or not result.get("ok"):
        error = (result or {}).get("error") if isinstance(result, dict) else "unknown"
        return JSONResponse(status_code=REFUSAL_STATUS.get(error, status.HTTP_400_BAD_REQUEST), content={
            "ok": False, "error": error, "detail": (result or {}).get("message") or "That UTR couldn't be used.",
            "utr_attempts_left": (result or {}).get("utr_attempts_left"),
        })

    # §7.6: the bank alert may already be in. A failure here is not the customer's: the verifier's
    # match_waiting sweep picks the pair up on its next tick.
    payment_status = result.get("status", "verifying")
    try:
        outcome = await match_for_utr(utr)
        if outcome is not None and outcome.status == "paid":
            payment_status = "paid"
        elif outcome is not None and outcome.status == "amount_mismatch":
            payment_status = "failed"
    except Exception:
        log.exception("immediate match for a submitted UTR failed; the verifier will retry it")
    return UtrOut(ok=True, status=payment_status, message=STATUS_MESSAGES.get(payment_status, ""),
                  utr_attempts_left=int(result.get("utr_attempts_left") or 0))


@router.get("/{token}/status", response_model=PayStatus)
async def get_status(token: str, request: Request) -> PayStatus:
    """The payment's status, for the page to poll while it is verified."""
    _limit("status", token, request)
    async with SessionLocal() as session:
        row = (await session.execute(text(
            "SELECT invoice_number, status, utr, utr_attempts, paid_at, expires_at <= now() AS past_expiry"
            " FROM payments WHERE public_token = :token"), {"token": token})).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This payment link is not valid.")
    current = "expired" if row["status"] == "pending" and row["past_expiry"] else row["status"]
    return PayStatus(
        invoice_number=row["invoice_number"], status=current, message=STATUS_MESSAGES.get(current, ""),
        utr_submitted=row["utr"] is not None, utr_attempts_left=max(0, MAX_UTR_ATTEMPTS - row["utr_attempts"]),
        paid_at=row["paid_at"].isoformat() if row["paid_at"] else None, paid_display=format_ist(row["paid_at"]),
    )