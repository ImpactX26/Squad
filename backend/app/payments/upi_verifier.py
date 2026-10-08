"""UPI payment verification: match the customer's UTR to the bank's credit SMS (ARCHITECTURE.md §7.6).

The method of the team's open-source UPI gateway, rebuilt on our backend, MCP tools, and Postgres
(no Apps Script, no Sheets):

1. The customer pays the company's UPI ID from the QR on /pay/[token] and submits the 12-digit UTR
   (payments.submit_utr -> status `verifying`).
2. The bank texts the company phone; the phone forwards the SMS to the project Gmail with subject
   BANK_ALERT_SUBJECT and BANK_SECRET at the end of the body.
3. This module polls that inbox over IMAP every BANK_POLL_SECONDS, decides whether the alert is
   genuine, reads the UTR and the amount, stores an audit row in bank_alerts (never the SMS text,
   only its SHA-256), and matches it: a `verifying` payment with the same UTR and the same amount
   to the paisa is marked paid in one transaction, and payment.paid is published. That match is
   the only automatic path to payment.paid (CLAUDE.md, §4.6).

Either side can arrive first. An alert whose UTR no payment holds yet is kept, and POST
/api/pay/{token}/utr calls match_for_utr() right after the UTR is stored.

Is the alert genuine? Checked in this order; the first failure is the stored reject_reason:
  - the From address is exactly one of BANK_ALERT_FROM;
  - if Gmail's Authentication-Results header is there (the topmost one, which Gmail itself adds),
    SPF, DKIM, or DMARC passed *for the sender's own domain*. A pass for some other domain doesn't
    count: an attacker's server passes SPF for its own domain while forging From;
  - the body contains BANK_SECRET. With no BANK_SECRET set, nothing is accepted (the reference
    skipped the check then; we refuse).

Reading the SMS: only two things come out of the text, the amount credited and the UTR; everything
else in it (footers, balances, "Sent via SMS Forwarder", promos, other numbers) is ignored. The mail
body is first turned into the SMS itself (sms_text): quoted-printable decoded, a forwarder's own
header lines ("From: VM-HDFCBK", "Sent: ...") dropped, invisible characters stripped.
  - It must contain a credit statement (credited / received / deposited). Failed, declined,
    reversed, pending, "will be credited" and collect requests are refused wherever they appear.
  - The amount is a Rs / INR / ₹ amount that is not a balance ("Bal", "Avl Bal", "Available balance",
    "Balance after transaction", "Bal is"); of several, the one nearest the credit statement. A
    two-decimal number with no currency is the fallback.
  - The verb nearest that amount says which way the money went: "debited for Rs 6,199.00; JOHN
    credited" is our money going out (debit_alert). A debit word anywhere else ("Sent via ...",
    "Avl Bal ... Dr", "paid" in a promo) doesn't matter.
  - The UTR is the 12-digit number next to a UTR / RRN / Ref / UPI keyword (a UPI path such as
    UPI/P2A/<utr>/NAME counts). Two different tagged numbers are ambiguous_utr, never a guess: an
    admin verifies that one by hand. With no tagged number, one standalone 12-digit number is the
    fallback.
A refused alert never pays anything: staff see it in bank_alerts, and the payment goes to
needs_review after PAYMENT_VERIFY_TIMEOUT_MINUTES.

Never blocks the event loop: IMAP runs in a worker thread (asyncio.to_thread), like the email
channel, whose login it reuses. A mail is marked seen only after its bank_alerts row is stored, and
gmail_message_id is UNIQUE, so a crash in between can't lose an alert or count it twice.
"""

import asyncio
import hashlib
import html
import json
import logging
import quopri
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parseaddr
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.events import EventBus, bus
from app.payments.money import money_str, to_money

log = logging.getLogger(__name__)

# ---------- reading the SMS ----------

# Zero-width spaces and joiners, BOM, soft hyphen, LRM/RLM, word joiner (the reference's set, plus U+2060).
INVISIBLE = re.compile("[​-‍﻿­‎‏⁠]")

_NUMBER = r"((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)(?!\d)(?!\.\d)"
STRICT_AMOUNT = re.compile(r"(?:\bRs\.?|\bINR|₹)\s*[:.]?\s*" + _NUMBER, re.IGNORECASE)
LOOSE_AMOUNT = re.compile(r"(?<![\d.,])((?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2})(?!\d)")
# "Bal", "Balance", "Avl Bal", "AvlBal", "Avail.bal", "Available Balance is", "Balance after transaction",
# "Bal is": looked for in the words between the previous number and an amount, so "Bal Rs 50,000.00
# credited Rs 500.00" still reads 500.00 as the payment.
BALANCE_WORD = re.compile(r"(?:\b|(?<=avl)|(?<=avail))bal(?:ance)?\b", re.IGNORECASE)
BALANCE_LOOKBACK = 40

# The 12-digit number next to a UTR / RRN / Ref / UPI keyword ("Ref no 427512345678", "UPI:…",
# "UTR No. …"), including a UPI path such as UPI/P2A/427512345678/RIYA (Axis) or UPI-427512345678-RIYA.
TAGGED_UTR = re.compile(
    r"\b(?:UTR|RRN|Reference|Ref|UPI)\.?\s*(?:no\.?|number|#)?\s*[:\-/#]?\s*(?:[A-Z0-9]{1,6}/)*(\d{12})(?!\d)",
    re.IGNORECASE)
LOOSE_UTR = re.compile(r"(?<!\d)(\d{12})(?!\d)")
# NUMERIC(10,2), the bank_alerts.amount column: anything larger is a misread, not a payment.
MAX_AMOUNT = Decimal("99999999.99")

CREDIT_WORDS = re.compile(
    r"\b(?:credited|received|deposited)\b|\bcredit(?:ed)?\s+(?:of|by|for|with)\b|\bhas\s+a\s+credit\b",
    re.IGNORECASE)
# Our own money going out. "paid by JOHN" / "sent to you" describe the payer, so they don't count.
DEBIT_WORDS = re.compile(
    r"\b(?:debited|debit|withdrawn|spent|sent|paid|transferred|trf|purchase|dr)\b(?!\s+(?:by|to\s+you)\b)",
    re.IGNORECASE)
FAILED_WORDS = re.compile(r"\b(?:failed|declined|reversed|reversal|unsuccessful)\b", re.IGNORECASE)
NOT_YET_WORDS = re.compile(
    r"\b(?:will\s+be|to\s+be)\s+credited\b|\bcollect\s+request\b|\brequested\b|\bpending\b", re.IGNORECASE)

# A forwarder's own lines before the SMS: "From: VM-HDFCBK", "Sent: 07/10/2026 14:02", Gmail's
# "---------- Forwarded message ---------" block, iOS's "Begin forwarded message:". Only the leading
# block is dropped; a line like this inside the SMS stays.
FORWARDER_HEADER = re.compile(
    r"^\s*(?:from|to|cc|sender|sent|date|time|received(?:\s+at)?|sim(?:\s*slot)?|slot|subject|device|"
    r"forwarded\s+(?:by|from|via)|message\s+from)\s*:.*$", re.IGNORECASE)
FORWARD_MARKER = re.compile(r"^\s*-{2,}\s*forwarded message\s*-{2,}\s*$|^\s*begin forwarded message:?\s*$",
                            re.IGNORECASE)
BODY_LABEL = re.compile(r"^\s*(?:message|msg|body|sms|text|content)\s*:\s*", re.IGNORECASE)
# A body that is still quoted-printable: a soft line break, or a UTF-8 character escaped as =E2=82=B9 (₹).
QUOTED_PRINTABLE = re.compile(r"=\r?\n|=[C-F][0-9A-F]=[89AB][0-9A-F]")


@dataclass(frozen=True)
class AlertEmail:
    """One forwarded bank alert, as read from the inbox (or built by the dev simulator)."""

    message_id: str                      # bank_alerts.gmail_message_id: the Message-ID header
    sender: str                          # the From header, as is
    subject: str
    body: str                            # plain text; never stored, never logged
    auth_results: tuple[str, ...] = ()   # Authentication-Results headers, topmost first
    received_at: datetime | None = None
    uid: str | None = None               # the IMAP UID, to mark it seen once stored


@dataclass(frozen=True)
class Verdict:
    """Whether an alert is a genuine credit, and what it says."""

    ok: bool
    reason: str | None = None
    utr: str | None = None
    amount: Decimal | None = None
    method: str | None = None            # "strict", or "loose" when a fallback pattern was needed

    @staticmethod
    def reject(reason: str) -> "Verdict":
        return Verdict(ok=False, reason=reason)


def normalize_body(raw: str) -> str:
    """The SMS on one line: invisible characters gone, whitespace collapsed (as the reference does)."""
    body = INVISIBLE.sub("", raw or "").replace(" ", " ")
    body = re.sub(r"[\r\n\t]+", " ", body)
    return re.sub(r"\s{2,}", " ", body).strip()


def sms_text(raw: str) -> str:
    """The SMS itself, out of a forwarded mail body, normalised: quoted-printable decoded when the
    forwarder left it encoded, the forwarder's own leading header lines and a "Message:" label dropped."""
    body = raw or ""
    if QUOTED_PRINTABLE.search(body):
        try:
            body = quopri.decodestring(body.encode("utf-8")).decode("utf-8")
        except (UnicodeError, ValueError):
            pass
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0
    while start < len(lines) and (not INVISIBLE.sub("", lines[start]).strip()
                                  or FORWARDER_HEADER.match(lines[start]) or FORWARD_MARKER.match(lines[start])):
        start += 1
    lines = lines[start:]
    if lines:
        lines[0] = BODY_LABEL.sub("", lines[0], count=1)
    return normalize_body("\n".join(lines))


def html_text(html_body: str) -> str:
    """An HTML-only mail as plain text: scripts and styles gone, line breaks kept, entities decoded."""
    body = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", html_body or "")
    body = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h[1-6])\s*>", "\n", body)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def parse_credit_alert(body: str) -> Verdict:
    """The UTR and the amount of a bank's UPI credit SMS, or why it isn't one. `body` is normalised.

    Everything but the credit statement, the credited amount and the UTR is ignored (see the module
    docstring): a footer, a balance or a stray "sent" or "Dr" doesn't change the verdict.
    """
    if FAILED_WORDS.search(body):
        return Verdict.reject("failed_or_reversed")
    if NOT_YET_WORDS.search(body):
        return Verdict.reject("not_completed")
    credits = [match.span() for match in CREDIT_WORDS.finditer(body)]
    if not credits:
        return Verdict.reject("debit_alert" if DEBIT_WORDS.search(body) else "not_a_credit_alert")

    amount, amount_span, amount_method = _amount(body, credits)
    if amount is None or amount_span is None:
        return Verdict.reject(amount_method)
    # The verb nearest the amount says which way it went: "debited for Rs 6,199.00; JOHN credited".
    debit_gap = min((_gap(amount_span, m.span()) for m in DEBIT_WORDS.finditer(body)), default=None)
    if debit_gap is not None and debit_gap < min(_gap(amount_span, span) for span in credits):
        return Verdict.reject("debit_alert")
    utr, utr_method = _utr(body)
    if utr is None:
        return Verdict.reject(utr_method)
    if amount <= 0:
        return Verdict.reject("zero_amount")
    if amount > MAX_AMOUNT:
        # Past bank_alerts.amount's NUMERIC(10,2): a misread, and storing it would fail on every tick.
        return Verdict.reject("amount_out_of_range")
    method = "strict" if utr_method == amount_method == "strict" else "loose"
    return Verdict(ok=True, utr=utr, amount=amount, method=method)


def _utr(body: str) -> tuple[str | None, str]:
    """(utr, "strict"|"loose"), or (None, reason). Two different tagged numbers are never a guess."""
    tagged = set(TAGGED_UTR.findall(body))
    if len(tagged) == 1:
        return next(iter(tagged)), "strict"
    if len(tagged) > 1:
        return None, "ambiguous_utr"
    loose = set(LOOSE_UTR.findall(body))
    if len(loose) == 1:
        return next(iter(loose)), "loose"
    return None, "ambiguous_utr" if loose else "no_utr"


def _amount(body: str, credits: list[tuple[int, int]]) -> tuple[Decimal | None, tuple[int, int] | None, str]:
    """(amount, its span, "strict"|"loose"), or (None, None, reason). Of several amounts, the one nearest
    the credit statement; a balance is never the payment."""
    for pattern, method in ((STRICT_AMOUNT, "strict"), (LOOSE_AMOUNT, "loose")):
        found: list[tuple[int, int, Decimal, tuple[int, int]]] = []
        for match in pattern.finditer(body):
            if _is_balance(body, match.start()):
                continue
            try:
                value = to_money(match.group(1))
            except ValueError:
                continue
            found.append((min(_gap(match.span(), span) for span in credits), match.start(), value, match.span()))
        if found:
            _, _, value, span = min(found)
            return value, span, method
    return None, None, "no_amount"


def _is_balance(body: str, start: int) -> bool:
    """A balance word between the previous number and the amount starting at `start`."""
    before = re.split(r"\d", body[max(0, start - BALANCE_LOOKBACK):start])[-1]
    return bool(BALANCE_WORD.search(before))


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Characters between two spans of the text (0 when they touch or overlap)."""
    return max(0, b[0] - a[1], a[0] - b[1])


# ---------- is it genuine? ----------


def sender_address(from_header: str) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


def authentication_failure(auth_results: tuple[str, ...], sender: str) -> str | None:
    """None when Gmail's verdict passes for the sender's domain (or there is no verdict); else a reason.

    Only the topmost Authentication-Results counts: Gmail adds it on arrival, above anything the
    sender wrote. A pass must be aligned with the From domain (exact match).
    """
    if not auth_results:
        return None
    header = " ".join(str(auth_results[0]).split())
    domain = sender.rpartition("@")[2]
    if not domain:
        return "spf_dkim_failed"

    def aligned(value: str) -> bool:
        return value.strip().strip("<>;").lstrip("@").rpartition("@")[2].lower() == domain

    for clause in header.split(";")[1:]:  # the first clause is the authserv-id, e.g. mx.google.com
        match = re.match(r"\s*(spf|dkim|dmarc)\s*=\s*([a-z]+)\b(.*)", clause, re.IGNORECASE | re.DOTALL)
        if not match or match.group(2).lower() != "pass":
            continue
        method, properties = match.group(1).lower(), match.group(3)
        key = {"dkim": r"header\.[di]", "spf": r"smtp\.mailfrom", "dmarc": r"header\.from"}[method]
        for value in re.findall(key + r"=([^\s;]+)", properties, re.IGNORECASE):
            if aligned(value):
                return None
    return "spf_dkim_failed"


def assess(alert: AlertEmail, settings: Settings) -> Verdict:
    """Is this a genuine bank credit, and for which UTR and amount? (No database.)"""
    sender = sender_address(alert.sender)
    if not sender or sender not in settings.bank_alert_senders:
        return Verdict.reject("sender_not_allowed")
    if (failure := authentication_failure(alert.auth_results, sender)) is not None:
        return Verdict.reject(failure)
    secret = settings.bank_secret.strip()
    if not secret:
        return Verdict.reject("bank_secret_not_configured")
    if secret not in (alert.body or ""):
        return Verdict.reject("missing_secret")
    # The passcode isn't part of the SMS; it could even look like a reference number.
    return parse_credit_alert(sms_text(alert.body.replace(secret, " ")))


# ---------- storing and matching ----------


@dataclass(frozen=True)
class MatchOutcome:
    """What one alert did. status: paid | amount_mismatch | waiting_for_utr | duplicate |
    not_verifying | already_handled."""

    status: str
    payment_id: str | None = None
    ticket_id: str | None = None
    invoice_number: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class IngestResult:
    alert_id: str | None
    verdict: Verdict
    duplicate: bool = False                 # this gmail_message_id was already stored
    match: MatchOutcome | None = None


_LOCK_ALERT = text("""
SELECT id, utr, amount, sender, gmail_message_id FROM bank_alerts
WHERE id = CAST(:id AS uuid) AND parsed_ok AND matched_payment_id IS NULL AND reject_reason IS NULL
FOR UPDATE
""")

_LOCK_PAYMENT_BY_UTR = text("""
SELECT p.id, p.ticket_id, p.amount, p.currency, p.status, p.invoice_number, p.utr, t.ticket_number
FROM payments p JOIN tickets t ON t.id = p.ticket_id
WHERE p.utr = :utr
FOR UPDATE OF p
""")


async def ingest(alert: AlertEmail, *, settings: Settings | None = None, events: EventBus | None = None) -> IngestResult:
    """Judge one alert, store its audit row, and match it when it is genuine. Idempotent per message id."""
    settings = settings or get_settings()
    verdict = assess(alert, settings)
    sender = sender_address(alert.sender)
    async with SessionLocal() as session:
        alert_id = (await session.execute(text("""
            INSERT INTO bank_alerts (gmail_message_id, sender, utr, amount, raw_sha256, parsed_ok,
                                     reject_reason, received_at, processed_at)
            VALUES (:message_id, :sender, :utr, :amount, :sha, :ok, :reason, :received, :processed)
            ON CONFLICT (gmail_message_id) DO NOTHING
            RETURNING id
        """), {
            "message_id": alert.message_id[:300], "sender": sender[:320], "utr": verdict.utr,
            "amount": verdict.amount, "sha": hashlib.sha256((alert.body or "").encode("utf-8")).hexdigest(),
            "ok": verdict.ok, "reason": verdict.reason,
            "received": alert.received_at or datetime.now(UTC),
            "processed": None if verdict.ok else datetime.now(UTC),
        })).scalar()
        await session.commit()

    if alert_id is None:
        log.info("bank alert %s already stored; skipped", alert.message_id)
        return IngestResult(alert_id=None, verdict=verdict, duplicate=True)
    if not verdict.ok:
        # Never the body: only who sent it and why it was refused.
        log.warning("bank alert %s from %s rejected: %s", alert.message_id, sender or "?", verdict.reason)
        return IngestResult(alert_id=str(alert_id), verdict=verdict)
    log.info("bank alert %s: UTR %s, %s (%s parse)", alert.message_id, verdict.utr,
             money_str(verdict.amount), verdict.method)
    match = await match_alert(alert_id, events=events)
    return IngestResult(alert_id=str(alert_id), verdict=verdict, match=match)


# ---------- a bank SMS an admin pastes on the payments page (§11.2) ----------

MANUAL_SENDER = "admin:"
MANUAL_MESSAGE_ID = "manual-"


def manual_admin(sender: str | None, message_id: str | None) -> str | None:
    """The admin's staff id when this bank_alerts row was pasted on the payments page, else None."""
    if not (sender or "").startswith(MANUAL_SENDER) or not (message_id or "").startswith(MANUAL_MESSAGE_ID):
        return None
    try:
        return str(uuid.UUID(sender[len(MANUAL_SENDER):]))
    except ValueError:
        return None


async def ingest_manual(sms: str, *, staff_id: str, note: str | None = None,
                        events: EventBus | None = None) -> IngestResult:
    """A bank SMS an admin pasted: the same reader and the same matcher as a forwarded mail (§7.6).

    The sender allowlist, SPF/DKIM and BANK_SECRET checks are skipped because the admin is signed in;
    the row is stored with sender "admin:<staff id>" and message id "manual-<uuid>", and a match pays
    with verified_by = that admin, never 'bank_alert'. The SMS text is never stored, only its hash.
    `note` (what the admin checked) goes on the timeline event when the SMS matches a payment at once.
    """
    staff_id = str(uuid.UUID(str(staff_id)))
    verdict = parse_credit_alert(sms_text(sms))
    message_id = f"{MANUAL_MESSAGE_ID}{uuid.uuid4()}"
    async with SessionLocal() as session:
        alert_id = (await session.execute(text("""
            INSERT INTO bank_alerts (gmail_message_id, sender, utr, amount, raw_sha256, parsed_ok, reject_reason,
                                     processed_at)
            VALUES (:message_id, :sender, :utr, :amount, :sha, :ok, :reason, :processed)
            RETURNING id
        """), {
            "message_id": message_id, "sender": f"{MANUAL_SENDER}{staff_id}", "utr": verdict.utr,
            "amount": verdict.amount, "sha": hashlib.sha256((sms or "").encode("utf-8")).hexdigest(),
            "ok": verdict.ok, "reason": verdict.reason, "processed": None if verdict.ok else datetime.now(UTC),
        })).scalar()
        await session.commit()
    if not verdict.ok:
        log.warning("bank SMS pasted by admin %s rejected: %s", staff_id, verdict.reason)
        return IngestResult(alert_id=str(alert_id), verdict=verdict)
    log.info("bank SMS pasted by admin %s: UTR %s, %s", staff_id, verdict.utr, money_str(verdict.amount))
    return IngestResult(alert_id=str(alert_id), verdict=verdict,
                        match=await match_alert(alert_id, events=events, note=note))


async def match_alert(alert_id: uuid.UUID | str, *, events: EventBus | None = None,
                      note: str | None = None) -> MatchOutcome:
    """Reconcile one stored, genuine alert with the payment holding its UTR, in one transaction.

    Paid only when the payment is `verifying` and the amounts are equal to the paisa. Both rows are
    locked (alert first, then payment, on every path), so the alert-first and UTR-first orders
    can't both win, and running this twice changes nothing. `note` goes on the timeline event.
    """
    events = events or bus
    published: list[tuple[str, dict[str, Any]]] = []
    async with SessionLocal() as session:
        async with session.begin():
            alert = (await session.execute(_LOCK_ALERT, {"id": str(alert_id)})).mappings().one_or_none()
            if alert is None:
                return MatchOutcome("already_handled")
            # A bank SMS an admin pasted on the payments page pays as that admin's verification, never
            # as 'bank_alert': the automatic path stays the forwarded mail alone (§4.6).
            admin = manual_admin(alert["sender"], alert["gmail_message_id"])
            verified_by, actor = (admin, admin) if admin else ("bank_alert", "system")
            method = "admin_bank_sms" if admin else "bank_alert"
            noted = {"note": note} if note else {}
            payment = (await session.execute(_LOCK_PAYMENT_BY_UTR, {"utr": alert["utr"]})).mappings().one_or_none()
            if payment is None:
                return MatchOutcome("waiting_for_utr", detail=alert["utr"])
            pid, tid = str(payment["id"]), str(payment["ticket_id"])
            facts = {"payment_id": pid, "ticket_id": tid, "invoice_number": payment["invoice_number"]}

            if payment["status"] == "paid":
                await _close_alert(session, alert_id, pid, "duplicate_alert")
                return MatchOutcome("duplicate", **facts)
            if payment["status"] != "verifying":
                reason = f"payment_{payment['status']}"
                await _close_alert(session, alert_id, pid, reason)
                await _ticket_event(session, tid, "payment_alert_unapplied", {
                    "payment_id": pid, "invoice_number": payment["invoice_number"], "utr": alert["utr"],
                    "amount": money_str(alert["amount"]), "reason": reason, "bank_alert_id": str(alert_id),
                    **noted}, actor=actor)
                return MatchOutcome("not_verifying", **facts, detail=reason)

            expected, received = money_str(payment["amount"]), money_str(alert["amount"])
            event = {"payment_id": pid, "ticket_id": tid, "ticket_number": payment["ticket_number"],
                     "invoice_number": payment["invoice_number"], "utr": alert["utr"],
                     "currency": payment["currency"], "bank_alert_id": str(alert_id)}
            if Decimal(alert["amount"]) != Decimal(payment["amount"]):
                reason = f"amount_mismatch: expected {expected}, bank alert says {received}"
                await session.execute(text(
                    "UPDATE payments SET status = 'failed', needs_review = TRUE"
                    " WHERE id = CAST(:p AS uuid) AND status = 'verifying'"), {"p": pid})
                await _close_alert(session, alert_id, pid, reason)
                await _ticket_event(session, tid, "payment_failed", {**event, "reason": reason, "expected": expected,
                                                                     "received": received, **noted}, actor=actor)
                published.append(("payment.failed", {**event, "reason": reason, "expected": expected,
                                                     "received": received}))
                outcome = MatchOutcome("amount_mismatch", **facts, detail=reason)
            else:
                paid = (await session.execute(text(
                    "UPDATE payments SET status = 'paid', paid_at = now(), verified_at = now(),"
                    " verified_by = :by, needs_review = FALSE"
                    " WHERE id = CAST(:p AS uuid) AND status = 'verifying' RETURNING paid_at"),
                    {"p": pid, "by": verified_by})).scalar()
                if paid is None:  # can't happen under the row lock; refuse rather than guess
                    return MatchOutcome("already_handled", **facts)
                await session.execute(text(
                    "UPDATE bank_alerts SET matched_payment_id = CAST(:p AS uuid), processed_at = now()"
                    " WHERE id = CAST(:a AS uuid)"), {"p": pid, "a": str(alert_id)})
                await _ticket_event(session, tid, "payment_paid", {
                    **event, "amount": expected, "verified_by": verified_by, "method": method, **noted}, actor=actor)
                published.append(("payment.paid", {**event, "amount": expected, "verified_by": verified_by,
                                                   "method": method, "paid_at": paid.isoformat()}))
                outcome = MatchOutcome("paid", **facts)
    # Committed. Publishing after the commit means a handler never sees a payment that then rolls back.
    for type_, data in published:
        await events.publish(type_, data)
    log.info("bank alert %s -> %s %s", alert_id, outcome.status, outcome.invoice_number)
    return outcome


async def match_for_utr(utr: str, *, events: EventBus | None = None) -> MatchOutcome | None:
    """Right after a UTR is submitted: the alert may already be here (§7.6). None when it isn't."""
    async with SessionLocal() as session:
        ids = (await session.execute(text(
            "SELECT id FROM bank_alerts WHERE utr = :utr AND parsed_ok AND matched_payment_id IS NULL"
            " AND reject_reason IS NULL ORDER BY received_at"), {"utr": utr})).scalars().all()
    outcome = None
    for alert_id in ids:
        outcome = await match_alert(alert_id, events=events)
        if outcome.status in ("paid", "amount_mismatch"):
            return outcome
    return outcome


async def match_waiting(*, events: EventBus | None = None) -> list[MatchOutcome]:
    """Alerts still waiting whose UTR a verifying payment now holds: match them.

    POST /api/pay/{token}/utr already does this for its own UTR; this sweep catches the rest (that
    call failing half way, or a UTR stored some other way), so an alert never waits forever.
    """
    async with SessionLocal() as session:
        ids = (await session.execute(text(
            "SELECT a.id FROM bank_alerts a JOIN payments p ON p.utr = a.utr"
            " WHERE a.parsed_ok AND a.matched_payment_id IS NULL AND a.reject_reason IS NULL"
            "   AND p.status = 'verifying' ORDER BY a.received_at"))).scalars().all()
    return [await match_alert(alert_id, events=events) for alert_id in ids]


async def _close_alert(session: AsyncSession, alert_id: Any, payment_id: str, reason: str) -> None:
    await session.execute(text(
        "UPDATE bank_alerts SET matched_payment_id = CAST(:p AS uuid), reject_reason = :r, processed_at = now()"
        " WHERE id = CAST(:a AS uuid)"), {"p": payment_id, "r": reason[:300], "a": str(alert_id)})


async def _ticket_event(session: AsyncSession, ticket_id: str, type_: str, payload: dict[str, Any], *,
                        actor: str = "system") -> None:
    await session.execute(text(
        "INSERT INTO ticket_events (ticket_id, type, payload, actor)"
        " VALUES (CAST(:t AS uuid), :type, CAST(:payload AS jsonb), :actor)"),
        {"t": ticket_id, "type": type_, "payload": json.dumps(payload, default=str), "actor": actor})


# ---------- the sweeps ----------


async def expire_links(*, events: EventBus | None = None, notify=None) -> list[dict[str, Any]]:
    """Pending links past expires_at become expired; their tickets stop waiting on payment, and each customer
    is told on their channel (`notify`, default workflows.tell_customer_link_expired), once per link."""
    events = events or bus
    async with SessionLocal() as session:
        async with session.begin():
            rows = (await session.execute(text(
                "UPDATE payments p SET status = 'expired' FROM tickets t WHERE t.id = p.ticket_id"
                " AND p.status = 'pending' AND p.expires_at <= now()"
                " RETURNING p.id, p.ticket_id, p.customer_id, p.invoice_number, t.ticket_number"))).mappings().all()
            for row in rows:
                await _ticket_event(session, str(row["ticket_id"]), "payment_expired",
                                    {"payment_id": str(row["id"]), "invoice_number": row["invoice_number"]})
                moved = (await session.execute(text(
                    "UPDATE tickets SET status = 'in_progress', updated_at = now()"
                    " WHERE id = :t AND status = 'awaiting_payment' RETURNING id"), {"t": row["ticket_id"]})).scalar()
                if moved:
                    await _ticket_event(session, str(row["ticket_id"]), "status_changed", {
                        "status": "in_progress", "note": f"Payment link {row['invoice_number']} expired"})
    if rows and notify is None:
        from app.brain.workflows import tell_customer_link_expired  # the brain loads lazily, as in the lifespan
        notify = tell_customer_link_expired
    for row in rows:
        await events.publish("ticket.updated", {"ticket_id": str(row["ticket_id"]), "reason": "payment_expired",
                                                "invoice_number": row["invoice_number"]})
        try:
            await notify({"payment_id": str(row["id"]), "ticket_id": str(row["ticket_id"]),
                          "customer_id": str(row["customer_id"]), "invoice_number": row["invoice_number"],
                          "ticket_number": row["ticket_number"]})
        except Exception:
            log.exception("payment %s expired, but the customer couldn't be told", row["invoice_number"])
    return [dict(row) for row in rows]


async def flag_overdue(*, settings: Settings | None = None, events: EventBus | None = None,
                       call_tool=None) -> list[dict[str, Any]]:
    """A UTR still unverified after PAYMENT_VERIFY_TIMEOUT_MINUTES: needs_review, and the admins are told.

    An admin then checks the bank statement and uses payments.mark_paid_manually (§5.5). Flagged in
    one UPDATE, so two machines running this sweep notify once.
    """
    from app.brain.commands import CommandTools

    settings = settings or get_settings()
    events = events or bus
    async with SessionLocal() as session:
        async with session.begin():
            rows = (await session.execute(text("""
                UPDATE payments p SET needs_review = TRUE
                FROM tickets t
                WHERE t.id = p.ticket_id AND p.status = 'verifying' AND NOT p.needs_review
                  AND p.utr_submitted_at < now() - make_interval(mins => CAST(:m AS int))
                RETURNING p.id, p.ticket_id, p.invoice_number, p.utr, p.amount, t.ticket_number
            """), {"m": settings.payment_verify_timeout_minutes})).mappings().all()
            for row in rows:
                await _ticket_event(session, str(row["ticket_id"]), "payment_needs_review", {
                    "payment_id": str(row["id"]), "invoice_number": row["invoice_number"], "utr": row["utr"],
                    "minutes": settings.payment_verify_timeout_minutes})
    for row in rows:
        tools = CommandTools(frozenset({"messaging__notify_staff"}), call_tool, events, role="automation",
                             trigger="payment.needs_review", ticket_id=str(row["ticket_id"]))
        try:
            await tools.call(
                "messaging__notify_staff", role="admin", type="payment_needs_review",
                title=f"Payment {row['invoice_number']} needs review",
                body=(f"UTR {row['utr']} for {money_str(row['amount'])} on {row['ticket_number']} has no bank alert "
                      f"after {settings.payment_verify_timeout_minutes} minutes. Check the bank statement, "
                      "then mark it paid manually or cancel it."),
                link=f"/tickets/{row['ticket_id']}")
        except Exception:
            log.exception("could not notify admins about %s (it is flagged needs_review)", row["invoice_number"])
        await events.publish("ticket.updated", {"ticket_id": str(row["ticket_id"]), "reason": "payment_needs_review",
                                                "invoice_number": row["invoice_number"]})
    return [dict(row) for row in rows]


async def republish_unconfirmed(*, events: EventBus | None = None) -> int:
    """payment.paid again for any payment paid in the last day whose receipt or booking never ran.

    Covers a crash between the commit and the publish (or between the receipt and the booking), and
    a mark_paid_manually whose event POST didn't reach the backend. The workflow claims each half
    once, so this can't send a receipt or book a visit twice.
    """
    events = events or bus
    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT p.id, p.ticket_id, p.invoice_number, p.amount, p.currency, p.utr, p.verified_by, t.ticket_number
            FROM payments p JOIN tickets t ON t.id = p.ticket_id
            WHERE p.status = 'paid' AND p.paid_at > now() - interval '1 day'
              AND (NOT EXISTS (SELECT 1 FROM ticket_events e
                               WHERE e.ticket_id = p.ticket_id AND e.type = 'payment_confirmed'
                                 AND e.payload->>'payment_id' = CAST(p.id AS text))
                   OR (EXISTS (SELECT 1 FROM service_catalog s WHERE s.code = p.service_code
                               AND (s.requires_visit OR s.part_type IS NOT NULL))
                       AND NOT EXISTS (SELECT 1 FROM ticket_events e
                                       WHERE e.ticket_id = p.ticket_id AND e.type = 'job_requested'
                                         AND e.payload->>'claim' = CAST(p.id AS text))))
        """))).mappings().all()
    for row in rows:
        await events.publish("payment.paid", {
            "payment_id": str(row["id"]), "ticket_id": str(row["ticket_id"]), "ticket_number": row["ticket_number"],
            "invoice_number": row["invoice_number"], "amount": money_str(row["amount"]), "currency": row["currency"],
            "utr": row["utr"], "verified_by": row["verified_by"], "republished": True,
            "method": "bank_alert" if row["verified_by"] == "bank_alert" else "manual",
        })
    if rows:
        log.info("republished payment.paid for %d payment(s) with no receipt yet", len(rows))
    return len(rows)


# ---------- the poller ----------


def alert_from_mail(mail: Any) -> AlertEmail:
    """An imap-tools message as an AlertEmail."""
    headers = mail.headers or {}
    message_id = str((headers.get("message-id") or ("",))[0] or "").strip() or f"imap-uid:{mail.uid}"
    # text/plain when there is one; an HTML-only mail stripped to its text.
    body = mail.text if (mail.text or "").strip() else html_text(mail.html or "")
    received = getattr(mail, "date", None)
    return AlertEmail(
        message_id=message_id, sender=mail.from_ or "", subject=mail.subject or "", body=body,
        auth_results=tuple(headers.get("authentication-results") or ()),
        received_at=received if isinstance(received, datetime) and received.tzinfo else None,
        uid=str(mail.uid) if mail.uid is not None else None,
    )


def imap_missing(settings: Settings) -> list[str]:
    """The settings the bank-alert poller still needs; empty when it can run."""
    needed = {
        "EMAIL_ADDRESS": settings.email_address, "EMAIL_APP_PASSWORD": settings.email_app_password,
        "BANK_ALERT_SUBJECT": settings.bank_alert_subject, "BANK_ALERT_FROM": settings.bank_alert_from,
        "BANK_SECRET": settings.bank_secret,
    }
    return [name for name, value in needed.items() if not value.strip()]


class UpiVerifier:
    """Started in main.py's lifespan. Every BANK_POLL_SECONDS: poll the bank alerts (when the inbox and
    the bank settings are there), expire old links, and flag UTRs that waited too long."""

    def __init__(self, settings: Settings | None = None, *, events: EventBus | None = None, call_tool=None) -> None:
        self.settings = settings or get_settings()
        self.events = events or bus
        self.call_tool = call_tool
        self.polling = not imap_missing(self.settings)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        missing = imap_missing(self.settings)
        if missing:
            log.info("UPI verifier: not polling bank alerts (%s empty); sweeps only", ", ".join(missing))
        else:
            log.info("UPI verifier: polling %s for '%s' from %s every %ds", self.settings.imap_host,
                     self.settings.bank_alert_subject, ", ".join(sorted(self.settings.bank_alert_senders)),
                     self.settings.bank_poll_seconds)
        self._task = asyncio.create_task(self._run(), name="upi-verifier")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            log.info("UPI verifier stopped")

    async def _run(self) -> None:
        try:
            await republish_unconfirmed(events=self.events)
        except Exception:
            log.exception("UPI verifier: republishing unconfirmed payments failed")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # IMAP or the database blipped; the alerts are still unseen, try again next tick.
                log.exception("UPI verifier tick failed; retrying on the next one")
            await asyncio.sleep(self.settings.bank_poll_seconds)

    async def tick(self) -> list[IngestResult]:
        results: list[IngestResult] = []
        if self.polling:
            stored: list[str] = []
            for alert in await asyncio.to_thread(self._fetch_alerts):
                try:
                    results.append(await ingest(alert, settings=self.settings, events=self.events))
                except Exception:
                    log.exception("bank alert %s not stored; it stays unseen for the next tick", alert.message_id)
                    continue
                if alert.uid:
                    stored.append(alert.uid)
            if stored:
                await asyncio.to_thread(self._mark_seen, stored)
        await match_waiting(events=self.events)
        await expire_links(events=self.events)
        await flag_overdue(settings=self.settings, events=self.events, call_tool=self.call_tool)
        return results

    def _fetch_alerts(self) -> list[AlertEmail]:
        """Blocking IMAP, in a thread: UNSEEN mail with the bank-alert subject, fetched without marking it seen."""
        from imap_tools import AND

        from app.channels.email_channel import is_bank_alert_subject, mailbox

        subject = self.settings.bank_alert_subject.strip()
        with mailbox(self.settings) as box:
            return [alert_from_mail(mail)
                    for mail in box.fetch(AND(seen=False, subject=subject), mark_seen=False, bulk=True)
                    if is_bank_alert_subject(mail.subject, self.settings)]  # never a customer's mail

    def _mark_seen(self, uids: list[str]) -> None:
        from imap_tools import MailMessageFlags

        from app.channels.email_channel import mailbox

        with mailbox(self.settings) as box:
            box.flag(uids, MailMessageFlags.SEEN, True)