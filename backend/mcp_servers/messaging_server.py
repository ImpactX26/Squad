"""messaging MCP server (:8104), ARCHITECTURE.md §5.4.

Block 1 tools: send_reply and notify_staff. send_email comes with the email templates.

messaging never talks to Discord, Telegram or Gmail itself. send_reply writes the reply and an
outbox row; the backend's channel dispatcher delivers it on the conversation's own channel and
records the outcome on that row (§5.4, §6.1). No tool here takes a channel, so a model can't
choose where a reply goes.

Run: uv run python -m mcp_servers.messaging_server
"""

import re

from mcp.server import MCPServer

from mcp_servers import serve
from mcp_servers.common import events
from mcp_servers.common.db import connection
from mcp_servers.common.results import dumps, fail, parse_uuid

PORT = 8104

STAFF_ROLES = ("agent", "technician", "warehouse", "admin")
NOTIFICATION_TYPE = re.compile(r"[a-z][a-z0-9_]{0,39}")
# The bell renders the link: a dashboard path or a web address, never javascript: or the like.
SAFE_LINK = re.compile(r"(/|https?://)\S*")

mcp = MCPServer("messaging", instructions="Reply to customers on their own channel; notify staff on the dashboard.")


@mcp.tool(structured_output=False)
async def send_reply(conversation_id: str, text: str) -> str:
    """Reply to a customer on their conversation's own channel. The reply is queued for delivery;
    its outbox row records when it went out.
    """
    conversation = parse_uuid(conversation_id, "conversation_id")
    body = text.strip()
    if not body:
        raise fail("missing_text", "text must not be empty")

    async with connection() as conn, conn.transaction():
        conv = await conn.fetchrow("SELECT id, channel, ticket_id FROM conversations WHERE id = $1", conversation)
        if conv is None:
            raise fail("not_found", "no conversation with that id", field="conversation_id")
        message = await conn.fetchrow(
            """
            INSERT INTO messages (conversation_id, ticket_id, sender_type, channel, body)
            VALUES ($1, $2, 'ai', $3, $4) RETURNING id, created_at
            """,
            conv["id"], conv["ticket_id"], conv["channel"], body,
        )
        # The dispatcher reads the channel and thread from the conversation, not from the payload.
        outbox_id = await conn.fetchval(
            "INSERT INTO outbox (conversation_id, message_id, payload) VALUES ($1, $2, $3) RETURNING id",
            conv["id"], message["id"], {"kind": "reply", "text": body},
        )
        await conn.execute("UPDATE conversations SET last_message_at = now() WHERE id = $1", conv["id"])
        if conv["ticket_id"] is not None:
            await conn.execute("UPDATE tickets SET updated_at = now() WHERE id = $1", conv["ticket_id"])

    if conv["ticket_id"] is not None:
        await events.publish("ticket.updated", {"ticket_id": conv["ticket_id"], "reason": "reply_queued",
                                                "message_id": message["id"], "channel": conv["channel"]})
    return dumps({"message_id": message["id"], "outbox_id": outbox_id, "conversation_id": conv["id"],
                  "channel": conv["channel"], "ticket_id": conv["ticket_id"], "status": "queued"})


@mcp.tool(structured_output=False)
async def notify_staff(
    title: str,
    body: str | None = None,
    link: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
    type: str = "info",
) -> str:
    """Notify staff on the dashboard bell: one person by user_id, or everyone with a role
    (agent, technician, warehouse, admin). type is a short label such as stock_low or job_rejected.
    """
    if bool(user_id) == bool(role):
        raise fail("bad_arguments", "give exactly one of user_id or role")
    if role is not None and role not in STAFF_ROLES:
        raise fail("bad_role", f"role must be one of: {', '.join(STAFF_ROLES)}", value=role)
    user = parse_uuid(user_id, "user_id") if user_id else None
    title = title.strip()
    if not title:
        raise fail("missing_text", "title must not be empty")
    if not NOTIFICATION_TYPE.fullmatch(type):
        raise fail("bad_type", "type is a short lowercase label such as stock_low", value=type)
    link = (link or "").strip() or None
    if link is not None and not SAFE_LINK.fullmatch(link):
        raise fail("bad_link", "link must be a dashboard path (/inventory) or an http(s) address", value=link)
    body = (body or "").strip() or None

    async with connection() as conn, conn.transaction():
        if role is not None:
            users = [r["id"] for r in await conn.fetch("SELECT id FROM staff_users WHERE role = $1 ORDER BY name", role)]
        else:
            if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM staff_users WHERE id = $1)", user):
                raise fail("not_found", "no staff user with that id", field="user_id")
            users = [user]
        rows = await conn.fetch(
            """
            INSERT INTO notifications (user_id, type, title, body, link)
            SELECT u, $2, $3, $4, $5 FROM unnest($1::uuid[]) AS u
            RETURNING id, user_id, created_at
            """,
            users, type, title, body, link,
        )

    for r in rows:
        await events.publish("notification.created", {"notification_id": r["id"], "user_id": r["user_id"],
                                                      "type": type, "title": title, "body": body, "link": link,
                                                      "created_at": r["created_at"]})
    return dumps({"notified": len(rows), "notifications": [{"notification_id": r["id"], "user_id": r["user_id"]}
                                                           for r in rows]})


if __name__ == "__main__":
    serve(mcp, PORT)
