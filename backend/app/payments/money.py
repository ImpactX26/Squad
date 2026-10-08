"""Money as the payment flow handles it (ARCHITECTURE.md §7.6): Decimal rupees, never floats.

No database here, so the payments MCP server and the app share it without either importing the
other's connection setup.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

PAISA = Decimal("0.01")

# The only payment method (§7.6), and UPI settles in rupees only. Stored on every payments row
# (provider, currency); constants rather than settings, because neither can take another value.
PAYMENT_PROVIDER = "upi_utr"
PAYMENT_CURRENCY = "INR"

# India Standard Time. India has no daylight saving, so a fixed offset is exact, and it needs no
# tz database (Windows ships none, and zoneinfo has nothing to read there).
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Warranty decides the price (§7.6). In warranty, replacing a failed part is free, and so is an OS
# reinstall. Upgrades (RAM, SSD) stay paid, since a warranty covers what was sold, not adding to
# it, and UPI_TEST is never free. A service that isn't listed here is paid, so a new one can't
# become free by accident.
WARRANTY_COVERED_SERVICES = frozenset({
    "BATTERY_REPLACE", "CMOS_REPLACE", "DISPLAY_REPLACE", "KEYBOARD_REPLACE", "FAN_REPLACE",
    "SSD_REPLACE", "CHARGER_REPLACE", "EAR_CUSHION_REPLACE", "OS_REINSTALL",
})


def free_under_warranty(service_code: str, in_warranty: bool) -> bool:
    """Whether this service costs the customer nothing: in warranty and a covered repair."""
    return bool(in_warranty) and service_code.strip().upper() in WARRANTY_COVERED_SERVICES


# A UPI transaction reference (UTR / RRN): exactly 12 digits.
UTR = re.compile(r"\d{12}")
# secrets.token_urlsafe(24): 24 random bytes as 32 URL-safe characters.
PUBLIC_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
# UTR submissions per payment link, typos included (§7.6). Refused ones count too, so a link can't
# be used to probe which UTRs exist.
MAX_UTR_ATTEMPTS = 5


def to_money(value: Any) -> Decimal:
    """A rupee amount with exactly two decimals. ValueError for anything that isn't one.

    Strict on purpose: an amount more precise than a paisa, negative, or not finite is refused
    rather than rounded, because "equal to the paisa" is what decides whether a payment is paid.
    """
    if isinstance(value, bool):
        raise ValueError("an amount can't be a boolean")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as e:
        raise ValueError(f"{value!r} is not an amount") from e
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{value!r} is not a positive amount")
    if amount != amount.quantize(PAISA):
        raise ValueError(f"{value!r} is more precise than a paisa")
    return amount.quantize(PAISA)


def money_str(value: Any) -> str:
    """"6199.00": how amounts travel in JSON (line items, tool results, the API), never as floats."""
    return f"{to_money(value):.2f}"


def format_inr(value: Any) -> str:
    """"₹6,199.00", "₹1,23,456.00": Indian digit grouping (the last three, then pairs)."""
    whole, fraction = money_str(value).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"₹{whole}.{fraction}"


def format_ist(moment: datetime | None) -> str | None:
    """"2 Oct 2026, 3:32 PM IST". Built by hand: Windows strftime has no %-d or %-I."""
    if moment is None:
        return None
    local = moment.astimezone(IST)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local.day} {local:%b %Y}, {hour}:{local:%M} {meridiem} IST"


def format_ist_date(moment: datetime | None) -> str | None:
    """"2 Oct 2026"."""
    if moment is None:
        return None
    local = moment.astimezone(IST)
    return f"{local.day} {local:%b %Y}"


def upi_uri(upi_id: str, payee_name: str, amount: Any, note: str) -> str:
    """The upi://pay link every UPI app understands (NPCI's deep-link parameters).

    pa = the payee's UPI ID, pn = the name the app shows, am = the amount with two decimals,
    cu = INR (UPI settles in rupees only), tn = the note on the transaction (the ticket number).
    """
    return (
        f"upi://pay?pa={quote(upi_id.strip(), safe='@.')}"
        f"&pn={quote(payee_name.strip(), safe='')}"
        f"&am={money_str(amount)}"
        f"&cu={PAYMENT_CURRENCY}"
        f"&tn={quote(note, safe='')}"
    )