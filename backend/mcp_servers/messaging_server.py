"""messaging MCP server (ARCHITECTURE.md §5.4) — :8104.

The single way the brain talks to customers. It never connects to Discord or Telegram: it
writes a messages row and an outbox row, and the backend's channel dispatcher delivers it
with retries, so bot connections stay in one process (§5.4, §6.1).

The reply target is always the conversation's own channel, never chosen by a model — that is
what guarantees "reply on the same platform" (§6.1).
"""

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_servers import run
from mcp_servers.common import db, events

mcp = MCPServer(name="messaging", instructions=__doc__)

# What send_email may attach (§5.4), and the template each belongs to. Only the name and the
# payment id go on the outbox; the email channel renders the file when it builds the mail, so no
# PDF bytes are ever stored.
EMAIL_ATTACHMENTS = {"receipt_pdf": "payment_confirmed"}


@mcp.tool()
async def send_reply(conversation_id: str, text: str) -> dict[str, Any]:
    """Reply to the customer on their own channel (§5.4).

    Writes the messages row and queues an outbox row; the dispatcher delivers it.
    """
    if not text.strip():
        return {"ok": False, "error": "text is empty"}
    conversation = await db.fetchrow(
        "SELECT id, channel, external_thread_id, ticket_id FROM conversations WHERE id = $1", conversation_id)
    if conversation is None:
        return {"ok": False, "error": "conversation not found", "conversation_id": conversation_id}

    async with db.acquire() as conn:
        async with conn.transaction():
            message = await conn.fetchrow(
                "INSERT INTO messages (conversation_id, ticket_id, sender_type, channel, body)"
                " VALUES ($1,$2,'ai',$3,$4) RETURNING id AS message_id, created_at",
                conversation_id, conversation["ticket_id"], conversation["channel"], text,
            )
            outbox = await conn.fetchrow(
                "INSERT INTO outbox (conversation_id, message_id, payload) VALUES ($1,$2,$3)"
                " RETURNING id AS outbox_id",
                conversation_id, message["message_id"],
                {"kind": "reply", "channel": conversation["channel"],
                 "thread_id": conversation["external_thread_id"], "text": text},
            )
            await conn.execute("UPDATE conversations SET last_message_at = now() WHERE id = $1", conversation_id)
    return {
        "ok": True, "queued": True, "channel": conversation["channel"],
        **db.row_to_dict(message), **db.row_to_dict(outbox),
    }


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    template: str,
    data: dict[str, Any] | None = None,
    ticket_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a transactional email: payment link, confirmation, visit scheduled, restock alert (§5.4).

    Goes on the same outbox as channel replies, so it gets the dispatcher's retries. The outbox
    row has no conversation_id, because an email address is not a channel conversation (§8.1).
    `attachments` may only be ["receipt_pdf"], with the payment_confirmed template and the
    payment's id in data.payment_id: the PDF receipt (§7.6).
    """
    if "@" not in to:
        return {"ok": False, "error": "to is not an email address"}
    refs, error = attachment_refs(attachments, template, data or {})
    if error:
        return {"ok": False, "error": error}
    payload = {"kind": "email", "channel": "email", "to": to, "subject": subject,
               "template": template, "data": data or {}, "ticket_id": ticket_id}
    if refs:
        payload["attachments"] = refs
    row = await db.fetchrow(
        "INSERT INTO outbox (conversation_id, message_id, payload) VALUES (NULL, NULL, $1)"
        " RETURNING id AS outbox_id, created_at",
        payload,
    )
    return {"ok": True, "queued": True, "to": to, "template": template,
            "attachments": [ref["type"] for ref in refs], **db.row_to_dict(row)}


def attachment_refs(
    attachments: list[str] | None, template: str, data: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    """([{type, payment_id}], None) for the outbox payload, or ([], the reason it's refused)."""
    refs: list[dict[str, str]] = []
    for name in dict.fromkeys(attachments or []):
        if name not in EMAIL_ATTACHMENTS:
            return [], f"unknown attachment {str(name)[:40]!r}; allowed: {', '.join(EMAIL_ATTACHMENTS)}"
        if template != EMAIL_ATTACHMENTS[name]:
            return [], f"{name} goes only with the {EMAIL_ATTACHMENTS[name]} template"
        try:
            payment_id = str(uuid.UUID(str(data.get("payment_id") or "")))
        except ValueError:
            return [], f"{name} needs the payment's id in data.payment_id"
        refs.append({"type": name, "payment_id": payment_id})
    return refs, None


@mcp.tool()
async def notify_staff(
    title: str,
    body: str | None = None,
    link: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
    type: str = "info",
) -> dict[str, Any]:
    """Dashboard or technician notification (§5.4). Give user_id for one person, or role for everyone in it."""
    if not user_id and not role:
        return {"ok": False, "error": "give user_id or role"}
    if user_id:
        recipients = await db.fetch("SELECT id FROM staff_users WHERE id = $1", user_id)
    else:
        recipients = await db.fetch("SELECT id FROM staff_users WHERE role = $1", role)
    if not recipients:
        return {"ok": False, "error": "no matching staff user", "user_id": user_id, "role": role}

    created: list[str] = []
    async with db.acquire() as conn:
        async with conn.transaction():
            for person in recipients:
                row = await conn.fetchrow(
                    "INSERT INTO notifications (user_id, type, title, body, link)"
                    " VALUES ($1,$2,$3,$4,$5) RETURNING id",
                    person["id"], type, title, body, link,
                )
                created.append(str(row["id"]))
    for notification_id, person in zip(created, recipients):
        await events.publish("notification.created", {
            "notification_id": notification_id, "user_id": str(person["id"]),
            "type": type, "title": title, "link": link,
        })
    return {"ok": True, "notification_ids": created, "recipients": len(created)}


if __name__ == "__main__":
    run(mcp, "messaging")