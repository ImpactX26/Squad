"""Booking contact details, validated in code, never trusted from a model (ARCHITECTURE.md §7.6).

/payments asks the customer for their full name, email, phone, and the service address. Intake's
payment_details slot filling (§7.1) has MODEL_FAST read them out of each reply, and every value then
passes two checks here before it is kept:

- its format, in code: email syntax, a 10-digit Indian mobile number, a 6-digit PIN code; and
- grounding: the value has to appear in what the customer actually wrote, so a model can't invent
  one. (The model may tidy spacing or capitalisation; it may not supply a value.)

A value that fails is dropped and asked for again, with a short reason. The details can arrive over
several messages; `merge_details` folds each message into what was collected so far.

The customer may also paste a Google Maps or Apple Maps link to the address. It is optional and never
asked for. It is read from the customer's own text in code (no model involved), and kept only when it
is https on one of MAPS_HOSTS.
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field

FIELDS = ("full_name", "email", "phone", "address", "location_url")
ADDRESS_REQUIRED = ("line1", "city", "postal_code")

# A mobile number: 10 digits starting 6-9, after an optional +91 / 91 / 0.
MOBILE = re.compile(r"[6-9]\d{9}")
PHONE_CHARACTERS = re.compile(r"[\d\s+\-().]{10,20}")
PIN_CODE = re.compile(r"[1-9]\d{5}")
NAME_FORBIDDEN = re.compile(r"[\d@_/\\<>{}\[\]|=+*#$%^&~`]")
WORD = re.compile(r"[^\W_]{3,}")

# Map links a customer may paste (§7.6): host -> the path it must start with ("" = any path).
MAPS_HOSTS = {
    "maps.app.goo.gl": "", "goo.gl": "/maps", "google.com": "/maps", "www.google.com": "/maps",
    "maps.google.com": "", "maps.apple.com": "",
}
LINK = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
MAX_LINK_CHARS = 500

ASK_LABELS = {
    "full_name": "your full name",
    "email": "your email address",
    "phone": "a 10-digit mobile number",
    "address": "the full service address where the technician should come "
               "(flat or house number and street, city, and the 6-digit PIN code)",
    "line1": "the flat or house number and street of the service address",
    "city": "the city of the service address",
    "postal_code": "the 6-digit PIN code of the service address",
}
PROBLEMS = {
    "full_name": "I couldn't read a full name in that.",
    "email": "That email address doesn't look right.",
    "phone": "That phone number doesn't look like a 10-digit Indian mobile number.",
    "postal_code": "The PIN code should be 6 digits.",
}


class AddressFields(BaseModel):
    line1: str | None = Field(default=None, description="flat or house number, building, street")
    line2: str | None = Field(default=None, description="area, locality or landmark")
    city: str | None = None
    state: str | None = None
    postal_code: str | None = Field(default=None, description="the 6-digit PIN code")


class DetailsExtract(BaseModel):
    """What MODEL_FAST reads out of one customer reply. Anything the message doesn't state is null."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: AddressFields = Field(default_factory=AddressFields)


@dataclass
class DetailsUpdate:
    """One message folded into the collected details."""

    collected: dict[str, Any]
    accepted: list[str] = field(default_factory=list)   # fields this message supplied
    problems: list[str] = field(default_factory=list)   # fields it supplied in a bad format
    missing: list[str] = field(default_factory=list)    # what is still needed, in ASK_LABELS keys

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def gave_nothing(self) -> bool:
        return not self.accepted and not self.problems


# ---------- one field at a time ----------


def clean_name(value: str | None, text: str) -> str | None:
    name = " ".join((value or "").split())
    if not (2 <= len(name) <= 80) or NAME_FORBIDDEN.search(name) or not re.search(r"[^\W\d_]", name):
        return None
    return name if _grounded(name, text) else None


def clean_email(value: str | None, text: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        email = validate_email(raw, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None
    return email if email in text.lower() else None


def clean_phone(value: str | None, text: str) -> str | None:
    """"+91 98450 12345", or None. Accepts +91 / 91 / 0 prefixes, spaces, dashes, dots and brackets."""
    raw = (value or "").strip()
    if not PHONE_CHARACTERS.fullmatch(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not MOBILE.fullmatch(digits) or digits not in re.sub(r"\D", "", text):
        return None
    return f"+91 {digits[:5]} {digits[5:]}"


def clean_pin(value: str | None, text: str) -> str | None:
    digits = re.sub(r"\s", "", value or "")
    if not PIN_CODE.fullmatch(digits) or digits not in re.sub(r"\D", "", text):
        return None
    return digits


def clean_location_url(url: str) -> str | None:
    """A Google Maps or Apple Maps link, or None. https only, on MAPS_HOSTS, no login or port parts."""
    url = url.strip().rstrip(").,;:!?]}>")
    if len(url) > MAX_LINK_CHARS:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or parts.username or parts.password or port is not None:
        return None
    if host not in MAPS_HOSTS or not parts.path.lower().startswith(MAPS_HOSTS[host]):
        return None
    return url


def find_location_url(text: str) -> str | None:
    """The first acceptable maps link the customer wrote in this message, if any."""
    for match in LINK.finditer(text or ""):
        url = clean_location_url(match.group(0))
        if url:
            return url
    return None


def clean_part(value: str | None, text: str, *, max_chars: int = 200) -> str | None:
    """A free-text address part: kept when at least one of its words is in the customer's text."""
    part = " ".join((value or "").split()).strip(" ,")
    if not (2 <= len(part) <= max_chars) or "://" in part:  # a pasted maps link is not an address line
        return None
    return part if _shares_a_word(part, text) else None


# ---------- a whole message ----------


def merge_details(collected: dict[str, Any] | None, extract: DetailsExtract, text: str) -> DetailsUpdate:
    """Fold one message's extract into what was collected; work out what is still missing."""
    merged: dict[str, Any] = {k: v for k, v in (collected or {}).items() if k in FIELDS}
    address = dict(merged.get("address") or {})
    update = DetailsUpdate(collected=merged)

    for name, cleaner in (("full_name", clean_name), ("email", clean_email), ("phone", clean_phone)):
        given = getattr(extract, name)
        if not given or not str(given).strip():
            continue
        value = cleaner(str(given), text)
        if value:
            merged[name] = value
            update.accepted.append(name)
        else:
            update.problems.append(name)

    fields = extract.address
    for name in ("line1", "line2", "city", "state", "postal_code"):
        given = getattr(fields, name)
        if not given or not str(given).strip():
            continue
        value = clean_pin(str(given), text) if name == "postal_code" else clean_part(str(given), text)
        if value:
            address[name] = value
            update.accepted.append(name)
        elif name == "postal_code":
            update.problems.append(name)
    if address:
        merged["address"] = address

    location_url = find_location_url(text)  # optional and never asked for (§7.6)
    if location_url:
        merged["location_url"] = location_url
        update.accepted.append("location_url")

    update.missing = missing(merged)
    return update


def missing(collected: dict[str, Any]) -> list[str]:
    """What is still needed. An address missing every part is asked for as one thing."""
    out = [name for name in ("full_name", "email", "phone") if not collected.get(name)]
    address = collected.get("address") or {}
    absent = [part for part in ADDRESS_REQUIRED if not address.get(part)]
    if len(absent) == len(ADDRESS_REQUIRED):
        out.append("address")
    else:
        out += absent
    return out


def ask_for_missing(update: DetailsUpdate) -> str:
    """The customer reply: what was wrong with this message, then only what is still needed."""
    lines = [PROBLEMS[name] for name in update.problems if name in PROBLEMS]
    wanted = [ASK_LABELS[name] for name in update.missing]
    opener = "Thanks!" if update.accepted else "Thanks."
    return " ".join([opener, *lines, f"To book the visit I still need {_join(wanted)}."])


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _grounded(value: str, text: str) -> bool:
    return " ".join(value.split()).casefold() in " ".join(text.split()).casefold()


def _shares_a_word(value: str, text: str) -> bool:
    words = {w.casefold() for w in WORD.findall(text)}
    return any(w.casefold() in words for w in WORD.findall(value)) or value.casefold() in text.casefold()