"""The email channel (ARCHITECTURE.md §6.2).

Inbound: `imap-tools` polls INBOX for UNSEEN every EMAIL_POLL_SECONDS. imap-tools is synchronous,
so the poll runs in a worker thread (`asyncio.to_thread`) and only the parsed messages come back
to the event loop.

Threading, in order of trust:
 1. `In-Reply-To` / `References` -> the root Message-ID of the thread we already know.
 2. the ticket number in the subject, `[SR-2026-00042]`, as the backup §6.2 asks for.
 3. otherwise a new thread, keyed by this mail's own Message-ID.

That root Message-ID is the conversation's `external_thread_id`, so Gmail keeps our replies in
the same thread: outbound mail carries `In-Reply-To` and `References` pointing at it.

Quoted history is stripped with `email-reply-parser`, so a ticket's description is what the
customer actually wrote this time, not the whole thread again. An HTML-only mail is read as text.

Mail a machine sent (a bounce, an auto-reply, a newsletter, a no-reply sender) is marked seen and
dropped: no ticket, no reply. Replying to it is how a mail loop starts.

Outbound goes through `aiosmtplib` with a Jinja2 HTML body (app/templates/email/) and a plain-text
alternative. A reply uses reply.html; a transactional mail from messaging.send_email (§5.4) renders
its own template (payment_link, payment_confirmed) with its data and keeps its subject as written.
A payment_confirmed mail that asks for `receipt_pdf` gets the PDF receipt (app/payments/receipt_pdf.py),
rendered here from the payment as the database has it, and only for a paid payment mailed to its own
customer. If that fails the mail still goes, without the PDF, and the ticket's timeline says why
(§7.6): a receipt is never withheld.
In development, EMAIL_REDIRECT_TO sends every outgoing mail to one inbox instead, with the real
recipient in the subject (§15). It starts only when ENABLE_EMAIL is true **and** the address and app
password are set, so on any machine but the demo host (§15) nothing connects.

Bank alerts are not customer mail. Every mail whose subject contains BANK_ALERT_SUBJECT belongs to
the UPI verifier (app/payments/upi_verifier.py, §7.6), whoever sent it: the IMAP search here leaves
them out, a second check in code skips any that slip through, and neither marks them seen. So a
forwarded bank SMS can never become a ticket, and the verifier never sees a customer's mail.
"""

import asyncio
import json
import logging
import re
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Any

from app.channels.base import Channel, InboundMessage
from app.channels.dispatcher import dispatcher
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# [SR-2026-00042] in a subject: the §6.2 backup when the mail client drops the threading headers.
TICKET_IN_SUBJECT = re.compile(r"\[(SR-\d{4}-\d{5})\]")
MESSAGE_ID = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")
MAX_BODY_CHARS = 4000
# References can grow without limit on a long thread; keep the tail, which is what clients use.
MAX_REFERENCES = 10
# The templates messaging.send_email may name (§12). Anything else is refused, not guessed at.
TRANSACTIONAL_TEMPLATES = frozenset({
    "payment_link", "payment_confirmed", "job_assigned", "visit_scheduled", "restock_alert", "ticket_created",
    "ticket_resolved", "ticket_deleted"})
# The one attachment a transactional mail may carry, and its template (messaging.send_email, §5.4).
RECEIPT_ATTACHMENT, RECEIPT_TEMPLATE = "receipt_pdf", "payment_confirmed"
# Mail no person wrote: bounces, auto-replies, newsletters, no-reply senders (RFC 3834, 2369, 3464).
# Taken as a customer's, each became a ticket and got a reply, and a reply to a bounce or a no-reply
# address bounces again: a loop (59 bounces on 2026-10-08). The poller marks it seen and leaves it.
AUTOMATED_LOCAL_PART = re.compile(
    r"(?:^|[-_.+])(?:mailer-daemon|postmaster|no-?reply|do-?not-?reply)(?:$|[-_.+])", re.IGNORECASE)
BULK_PRECEDENCE = frozenset({"bulk", "list", "junk", "auto_reply"})


class EmailAdapter:
    """The `email` ChannelAdapter (§6.1): an IMAP poller and an SMTP sender."""

    channel: Channel = "email"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None
        self._templates: Any = None

    @classmethod
    def enabled(cls, settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        return bool(
            settings.enable_email
            and settings.email_address.strip()
            and settings.email_app_password.strip()
        )

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Begin polling. One connection check first, so a bad app password fails loudly."""
        await asyncio.to_thread(self._check_login)
        self._task = asyncio.create_task(self._poll_forever(), name="email-poller")
        log.info("email channel polling %s every %ds",
                 self.settings.imap_host, self.settings.email_poll_seconds)

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
            log.info("email channel stopped")

    def _check_login(self) -> None:
        with mailbox(self.settings):
            return

    async def _poll_forever(self) -> None:
        while True:
            try:
                for inbound in await asyncio.to_thread(self._fetch_unseen):
                    await self._handle(inbound)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient IMAP failure must not kill the poller; the mail stays UNSEEN.
                log.exception("email poll failed; retrying on the next tick")
            await asyncio.sleep(self.settings.email_poll_seconds)

    # ---------- inbound ----------

    def _fetch_unseen(self) -> list[InboundMessage]:
        """Blocking IMAP work, run in a thread. Marks the mail it takes as seen, so it is read once.

        Fetched with mark_seen=False (BODY.PEEK) and flagged afterwards, so a bank alert that
        slipped past the search is left unseen for the verifier rather than swallowed here.
        """
        from imap_tools import AND, NOT, MailMessageFlags

        subject = self.settings.bank_alert_subject.strip()
        criteria = AND(NOT(subject=subject), seen=False) if subject else AND(seen=False)
        found: list[InboundMessage] = []
        taken: list[str] = []
        with mailbox(self.settings) as box:
            for mail in box.fetch(criteria, mark_seen=False, bulk=True):
                if is_bank_alert_subject(mail.subject, self.settings):
                    continue  # the verifier's, not ours (§7.6)
                taken.append(mail.uid)
                inbound = self._to_inbound(mail)
                if inbound is not None:
                    found.append(inbound)
            if taken:
                box.flag(taken, MailMessageFlags.SEEN, True)
        return found

    def _to_inbound(self, mail: Any) -> InboundMessage | None:
        sender = parseaddr(mail.from_ or "")[1].lower()
        if not sender or sender == self.settings.email_address.lower():
            return None  # our own copy of an outbound mail
        if (why := automated_mail(sender, mail.headers or {})) is not None:
            log.info("email from %s skipped, no ticket and no reply: %s", sender, why)
            return None
        body = strip_quoted(mail.text if (mail.text or "").strip() else plain_from_html(mail.html or ""))
        if not body:
            return None

        message_id = (mail.headers.get("message-id") or (None,))[0] or make_msgid()
        thread_root = thread_root_of(mail.headers, message_id)
        subject = mail.subject or ""
        return InboundMessage(
            channel="email",
            external_user_id=sender,
            external_thread_id=thread_root,
            display_name=parseaddr(mail.from_ or "")[0] or None,
            text=body[:MAX_BODY_CHARS],
            attachments=[{"filename": a.filename, "size": a.size} for a in (mail.attachments or [])],
            external_message_id=message_id,
            raw_meta={
                "subject": subject,
                "ticket_number": ticket_from_subject(subject),
                "references": references_of(mail.headers, message_id),
                "to": self.settings.email_address,
            },
        )

    async def _handle(self, inbound: InboundMessage) -> None:
        from app.brain.intake import handle_inbound

        try:
            result = await handle_inbound(
                inbound, email=inbound.external_user_id, full_name=inbound.display_name)
        except Exception:
            log.exception("email intake failed for %s", inbound.external_message_id)
            return
        await dispatcher.deliver_pending(conversation_id=result.conversation_id)

    # ---------- outbound ----------

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        """Reply into the same Gmail thread, with In-Reply-To and References (§6.2)."""
        import aiosmtplib

        s = self.settings
        to_address = str(meta.get("to") or meta.get("email") or "").strip()
        if not to_address:
            raise RuntimeError("no recipient for this email reply")
        transactional = meta.get("kind") == "email"
        subject = str(meta.get("subject") or "").strip() if transactional else reply_subject(meta)
        if transactional and not subject:
            raise RuntimeError("a transactional email needs a subject")
        receipts, problems = await self.receipt_attachments(meta, to_address) if transactional else ([], [])
        to_address, subject = redirected(to_address, subject, s)

        message_id = make_msgid(domain=s.email_address.split("@")[-1])
        message = EmailMessage()
        message["Message-ID"] = message_id
        message["From"] = formataddr((s.email_from_name, s.email_address))
        message["To"] = to_address
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        if not transactional and thread_id and MESSAGE_ID.fullmatch(thread_id):
            # What keeps it in the customer's existing thread instead of starting a new one.
            message["In-Reply-To"] = thread_id
            references = [*(meta.get("references") or []), thread_id]
            message["References"] = " ".join(dict.fromkeys(references[-MAX_REFERENCES:]))

        if transactional:
            plain, html = self.render_transactional(str(meta.get("template") or ""), dict(meta.get("data") or {}),
                                                    receipt_attached=bool(receipts))
        else:
            plain, html = text, self._render(text, meta)
        message.set_content(plain)
        message.add_alternative(html, subtype="html")
        for filename, pdf in receipts:
            message.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)

        await aiosmtplib.send(
            message,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.email_address,
            password=s.email_app_password,
            use_tls=s.smtp_port == 465,
            start_tls=s.smtp_port == 587,
        )
        # After the send, so a retried delivery doesn't note the same problem once per attempt.
        for ticket_id, payment_id, reason in problems:
            await note_receipt_problem(ticket_id, payment_id, reason)
        return message_id

    async def receipt_attachments(
        self, meta: dict, recipient: str,
    ) -> tuple[list[tuple[str, bytes]], list[tuple[str | None, str | None, str]]]:
        """([(filename, PDF)], [(ticket_id, payment_id, why it isn't attached)]) for a transactional mail (§7.6).

        Never raises. The payment is read again here and must be paid, and the mail must be going to
        that payment's own customer, so a receipt can't reach anyone else whatever the caller asked.
        """
        attached: list[tuple[str, bytes]] = []
        problems: list[tuple[str | None, str | None, str]] = []
        for ref in meta.get("attachments") or []:
            ref = ref if isinstance(ref, dict) else {"type": ref}
            payment_id = str(ref.get("payment_id") or "") or None
            ticket_id = meta.get("ticket_id")
            try:
                if ref.get("type") != RECEIPT_ATTACHMENT or meta.get("template") != RECEIPT_TEMPLATE:
                    raise ReceiptSkipped(f"attachment {str(ref.get('type'))[:40]!r} isn't allowed here")
                invoice = await receipt_invoice(payment_id or "", self.settings)
                if invoice is None:
                    raise ReceiptSkipped("the payment wasn't found")
                ticket_id = invoice["ticket_id"]
                if invoice["status"] != "paid":
                    raise ReceiptSkipped(f"the payment is {invoice['status']}, not paid")
                on_file = str((invoice.get("customer") or {}).get("email") or "").strip().casefold()
                if not on_file or on_file != recipient.strip().casefold():
                    raise ReceiptSkipped("the mail isn't going to the payment's customer")
                from app.payments.receipt_pdf import build_receipt_pdf

                pdf = await asyncio.to_thread(build_receipt_pdf, invoice, self.settings)
                attached.append((receipt_filename(invoice["invoice_number"]), pdf))
            except ReceiptSkipped as e:
                log.warning("receipt PDF for payment %s not attached: %s", payment_id, e)
                problems.append((ticket_id, payment_id, str(e)))
            except Exception as e:
                log.exception("receipt PDF for payment %s failed; sending the email without it", payment_id)
                problems.append((ticket_id, payment_id, f"it couldn't be rendered ({type(e).__name__})"))
        return attached, problems

    async def typing(self, thread_id: str) -> None:
        """Email has no typing indicator (§6.2)."""
        return None

    def _environment(self) -> Any:
        if self._templates is None:
            from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

            from app.core.config import BACKEND_DIR

            self._templates = Environment(
                loader=FileSystemLoader(BACKEND_DIR / "app" / "templates" / "email"),
                autoescape=select_autoescape(["html"]),
                # A transactional mail missing a field fails loudly (retried, then marked failed on
                # the outbox) instead of going out with a blank where the amount should be.
                undefined=StrictUndefined,
                keep_trailing_newline=True,
            )
        return self._templates

    def render_transactional(
        self, template: str, data: dict[str, Any], *, receipt_attached: bool = False,
    ) -> tuple[str, str]:
        """(plain text, HTML) for a messaging.send_email template (§5.4): `<template>.txt` and `.html`.

        `receipt_attached` is set here from what was actually attached, never taken from the data."""
        if template not in TRANSACTIONAL_TEMPLATES:
            raise RuntimeError(f"unknown email template {template[:60]!r}")
        values = {"company_name": self.settings.company_name, **data, "receipt_attached": receipt_attached}
        environment = self._environment()
        return (
            environment.get_template(f"{template}.txt").render(**values),
            environment.get_template(f"{template}.html").render(**values),
        )

    def _render(self, text: str, meta: dict) -> str:
        return self._environment().get_template("reply.html").render(
            body=text,
            company_name=self.settings.company_name,
            ticket_number=meta.get("ticket_number"),
            paragraphs=[p.strip() for p in text.split("\n") if p.strip()],
        )


class ReceiptSkipped(Exception):
    """A receipt PDF that is deliberately not attached; the reason is the message."""


async def receipt_invoice(payment_id: str, settings: Settings) -> dict[str, Any] | None:
    """The payment as the database has it now (app/payments/invoice.py), or None for a bad or unknown id."""
    from app.payments.invoice import load_invoice

    try:
        uuid.UUID(payment_id)
    except ValueError:
        return None
    return await load_invoice(payment_id=payment_id, settings=settings)


def receipt_filename(invoice_number: str) -> str:
    """"Receipt-INV-2026-00007.pdf"."""
    return f"Receipt-{re.sub(r'[^A-Za-z0-9-]', '', str(invoice_number)) or 'payment'}.pdf"


async def note_receipt_problem(ticket_id: str | None, payment_id: str | None, reason: str) -> None:
    """A timeline note when the receipt mail went without its PDF (§7.6). Never raises."""
    from sqlalchemy import text as sql

    from app.core.db import SessionLocal

    try:
        uuid.UUID(str(ticket_id))
        async with SessionLocal() as session:
            await session.execute(sql(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor)"
                " VALUES (CAST(:t AS uuid), 'note', CAST(:payload AS jsonb), 'system')"
            ), {"t": str(ticket_id), "payload": json.dumps({
                "note": f"The receipt email went out without its PDF: {reason}.",
                "payment_id": payment_id, "receipt_pdf": "not_attached"})})
            await session.commit()
    except Exception:
        log.exception("couldn't note the missing receipt PDF on ticket %s", ticket_id)


# ---------- IMAP, shared with the UPI verifier (§7.6) ----------


def mailbox(settings: Settings) -> Any:
    """A logged-in imap-tools MailBox on INBOX, used as a context manager. Blocking: call it in a thread."""
    from imap_tools import MailBox

    return MailBox(settings.imap_host, port=settings.imap_port).login(
        settings.email_address, settings.email_app_password, initial_folder="INBOX")


def is_bank_alert_subject(subject: str | None, settings: Settings) -> bool:
    """Whether a mail belongs to the UPI verifier rather than intake (§7.6).

    The same rule as Gmail's IMAP SUBJECT search: the subject contains BANK_ALERT_SUBJECT, ignoring
    case. The sender doesn't enter into it; the verifier judges that, and records a rejection.
    """
    marker = settings.bank_alert_subject.strip().casefold()
    return bool(marker) and marker in (subject or "").casefold()


def redirected(to_address: str, subject: str, settings: Settings) -> tuple[str, str]:
    """(recipient, subject) after EMAIL_REDIRECT_TO, which applies in development only (§15)."""
    target = settings.email_redirect_to.strip()
    if settings.app_env != "development" or not target:
        return to_address, subject
    return target, f"[to {to_address}] {subject}"


# ---------- parsing helpers, kept pure so the tests need no mail server ----------


def strip_quoted(body: str) -> str:
    """The customer's new text, without the thread quoted underneath (§6.2)."""
    try:
        from email_reply_parser import EmailReplyParser

        reply = EmailReplyParser.parse_reply(body)
    except Exception:  # never lose a message because the parser tripped
        reply = body
    return (reply or "").strip()


def automated_mail(sender: str, headers: dict[str, Any]) -> str | None:
    """Why a machine sent this mail rather than a person, or None (§6.2)."""
    if AUTOMATED_LOCAL_PART.search(sender.rpartition("@")[0]):
        return "an automated sender"
    auto_submitted = _header(headers, "auto-submitted").split(";")[0].strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"
    if _header(headers, "precedence").strip().lower() in BULK_PRECEDENCE:
        return "bulk mail"
    if _header(headers, "list-id") or _header(headers, "list-unsubscribe"):
        return "a mailing list"
    if _header(headers, "feedback-id"):
        return "bulk mail (Feedback-ID)"  # what bulk senders set for Gmail's spam feedback; a person's mail has none
    if _header(headers, "return-path").strip() == "<>":
        return "a bounce (empty Return-Path)"
    if _header(headers, "content-type").strip().lower().startswith("multipart/report"):
        return "a delivery report"
    return None


def plain_from_html(html_body: str) -> str:
    """An HTML-only mail as text, so a ticket never starts with "<!DOCTYPE html"."""
    from app.payments.upi_verifier import html_text

    text = re.sub(r"[ \t\xa0]+", " ", html_text(html_body))
    return re.sub(r"\s*\n\s*(?:\n\s*)+", "\n\n", text).strip()


def thread_root_of(headers: dict[str, Any], message_id: str) -> str:
    """The thread's root Message-ID: the first entry of References, else In-Reply-To, else ours."""
    references = _header(headers, "references")
    if references:
        found = MESSAGE_ID.findall(references)
        if found:
            return found[0]
    in_reply_to = _header(headers, "in-reply-to")
    if in_reply_to:
        found = MESSAGE_ID.findall(in_reply_to)
        if found:
            return found[0]
    return message_id


def references_of(headers: dict[str, Any], message_id: str) -> list[str]:
    """Every Message-ID in this mail's thread, oldest first, with its own on the end."""
    found = MESSAGE_ID.findall(_header(headers, "references") or "")
    in_reply_to = MESSAGE_ID.findall(_header(headers, "in-reply-to") or "")
    return list(dict.fromkeys([*found, *in_reply_to, message_id]))


def ticket_from_subject(subject: str) -> str | None:
    """`[SR-2026-00042]` out of a subject line: §6.2's backup when the headers are gone."""
    match = TICKET_IN_SUBJECT.search(subject or "")
    return match.group(1) if match else None


def reply_subject(meta: dict) -> str:
    """Keep the customer's subject and put the ticket number in it, once (§6.2)."""
    subject = str(meta.get("subject") or "").strip()
    ticket_number = meta.get("ticket_number")
    if not subject:
        subject = f"Your support request {ticket_number}" if ticket_number else "Your support request"
    if ticket_number and ticket_number not in subject:
        subject = f"[{ticket_number}] {subject}"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _header(headers: dict[str, Any], name: str) -> str:
    value = (headers or {}).get(name)
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v)
    return str(value or "")