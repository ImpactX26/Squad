"""The website chat: POST /api/chat/session and WS /ws/chat/{session_id}.

Against the real database inside a rolled-back transaction; intake is a fake (P1's intake is
tested on its own), and the socket is driven in-process (tests/ws_support.py).
"""

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels import inbound as inbound_module
from app.channels import web_chat
from app.channels.inbound import FALLBACK_REPLY_NO_TICKET
from app.main import create_app
from tests.api_support import client, session, settings  # noqa: F401 (fixtures)
from tests.ws_support import Closed, WSClient


@pytest.fixture
async def chat_app(session, monkeypatch):
    app = create_app(settings())
    maker = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False)
    app.dependency_overrides[web_chat.get_chat_sessionmaker] = lambda: maker
    flushed = []

    class Dispatcher:
        async def deliver_pending(self, conversation_id=None):
            flushed.append(conversation_id)
            return 0

    monkeypatch.setattr(web_chat, "get_dispatcher", lambda: Dispatcher())
    monkeypatch.setattr(inbound_module, "TYPING_EVERY_SECONDS", 0.02)
    yield SimpleNamespace(app=app, maker=maker, flushed=flushed, session=session)
    await web_chat.web_adapter.drain()  # before the transaction is rolled back


def fake_intake(monkeypatch, handler):
    monkeypatch.setattr(inbound_module, "_intake", lambda: handler)


async def open_session(app, name="Rahul Nair", email=None) -> dict:
    email = email or f"chat-{uuid.uuid4().hex[:8]}@example.com"
    async with client(app) as c:
        response = await c.post("/api/chat/session", json={"name": name, "email": email})
    assert response.status_code == 201, response.text
    return response.json()


def is_type(kind):
    return lambda frame: frame["type"] == kind


# ---------- POST /api/chat/session ----------


async def test_a_session_opens_a_web_account_and_conversation(chat_app):
    body = await open_session(chat_app.app, name="  New Person ", email="New-Chat-Person@Example.com")
    assert body["name"] == "New Person" and len(body["session_id"]) >= 32
    row = (await chat_app.session.execute(text(
        """SELECT cu.full_name, cu.email, ci.display_name, c.channel FROM customer_identities ci
           JOIN customers cu ON cu.id = ci.customer_id
           JOIN conversations c ON c.channel = 'web' AND c.external_thread_id = ci.external_user_id
           WHERE ci.channel = 'web' AND ci.external_user_id = :s"""), {"s": body["session_id"]})).one()
    assert row == ("New Person", "new-chat-person@example.com", "New Person", "web")


async def test_a_known_email_links_to_that_customer_without_revealing_them(chat_app):
    tag = uuid.uuid4().hex[:8]
    existing = await chat_app.session.scalar(text(
        "INSERT INTO customers (full_name, email) VALUES ('Stored Name', :e) RETURNING id"), {"e": f"known-{tag}@example.com"})
    body = await open_session(chat_app.app, name="Typed Name", email=f"KNOWN-{tag}@example.com")
    assert body == {"session_id": body["session_id"], "name": "Typed Name"}  # never the stored name
    linked = await chat_app.session.scalar(text(
        "SELECT customer_id FROM customer_identities WHERE channel = 'web' AND external_user_id = :s"),
        {"s": body["session_id"]})
    assert linked == existing


@pytest.mark.parametrize("payload", [
    {"name": "Rahul", "email": "not-an-email"},
    {"name": "   ", "email": "rahul@example.com"},
    {"name": "x" * 81, "email": "rahul@example.com"},
    {"email": "rahul@example.com"},
])
async def test_the_pre_chat_form_is_validated(chat_app, payload):
    async with client(chat_app.app) as c:
        response = await c.post("/api/chat/session", json=payload)
    assert response.status_code == 422


# ---------- WS /ws/chat/{session_id} ----------


async def test_an_unknown_session_is_closed_with_1008(chat_app):
    with pytest.raises(Closed) as closed:
        async with WSClient(chat_app.app, "/ws/chat/no-such-session") as ws:
            await ws.receive_json()
    assert closed.value.code == 1008


async def test_the_database_down_closes_with_1011(chat_app):
    def broken_maker():
        raise OSError("database down")

    chat_app.app.dependency_overrides[web_chat.get_chat_sessionmaker] = lambda: broken_maker
    with pytest.raises(Closed) as closed:
        async with WSClient(chat_app.app, "/ws/chat/anything") as ws:
            await ws.receive_json()
    assert closed.value.code == 1011


async def test_a_message_is_echoed_handed_to_intake_and_answered(chat_app, monkeypatch):
    got = []

    async def intake(inbound):
        got.append(inbound)
        await asyncio.sleep(0.06)  # long enough for a few typing frames
        await web_chat.web_adapter.send(inbound.external_thread_id, "Which serial is on the sticker?",
                                        {"message_id": "m-1"})

    fake_intake(monkeypatch, intake)
    opened = await open_session(chat_app.app)
    sid = opened["session_id"]
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:
        first = await ws.receive_json()
        assert first == {"type": "session", "session_id": sid, "name": "Rahul Nair", "ticket_number": None,
                         "messages": []}
        await ws.send_json({"type": "message", "text": "  My screen flickers ", "client_id": "c-1"})
        echo = await ws.receive_json()
        assert (echo["type"], echo["sender"], echo["text"], echo["client_id"]) == ("message", "customer", "My screen flickers", "c-1")
        reply, before = await ws.receive_until(lambda f: f["type"] == "message")
        assert (reply["sender"], reply["text"], reply["id"]) == ("support", "Which serial is on the sticker?", "m-1")
        assert {"type": "typing", "on": True} in before
        await ws.receive_until(lambda f: f == {"type": "typing", "on": False})
    await web_chat.web_adapter.drain()
    [inbound] = got
    assert (inbound.channel, inbound.external_user_id, inbound.external_thread_id) == ("web", sid, sid)
    assert (inbound.text, inbound.external_message_id, inbound.display_name) == ("My screen flickers", "c-1", "Rahul Nair")
    conversation = await chat_app.session.scalar(text(
        "SELECT id FROM conversations WHERE channel = 'web' AND external_thread_id = :s"), {"s": sid})
    assert chat_app.flushed == [conversation]  # the reply is flushed for this conversation at once


async def test_the_ticket_number_is_pushed_once_intake_creates_it(chat_app, monkeypatch):
    opened = await open_session(chat_app.app)
    sid = opened["session_id"]

    async def intake(inbound):
        # What create_ticket(conversation_id=...) does: link the conversation to the new ticket.
        async with chat_app.maker() as db:
            conversation, customer = (await db.execute(text(
                "SELECT id, customer_id FROM conversations WHERE channel = 'web' AND external_thread_id = :s"),
                {"s": sid})).one()
            ticket = await db.scalar(text(
                """INSERT INTO tickets (customer_id, source_channel, category, title, description)
                   VALUES (:c, 'web', 'hardware', 'Screen flickers', 'x') RETURNING id"""), {"c": customer})
            await db.execute(text("UPDATE conversations SET ticket_id = :t WHERE id = :c"),
                             {"t": ticket, "c": conversation})
            await db.commit()

    fake_intake(monkeypatch, intake)
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "message", "text": "LB13-W4N7PC flickers", "client_id": "c-1"})
        frame, _ = await ws.receive_until(is_type("ticket"))
        assert frame["ticket_number"].startswith("SR-")
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:  # a reload shows it at once
        assert (await ws.receive_json())["ticket_number"] == frame["ticket_number"]


async def test_a_failing_intake_still_answers_and_the_socket_stays_open(chat_app, monkeypatch):
    async def broken(inbound):
        raise RuntimeError("boom")

    async def fallback(adapter, inbound):  # the outbox route is tested in test_channels_inbound
        await adapter.send(inbound.external_thread_id, FALLBACK_REPLY_NO_TICKET, {})

    fake_intake(monkeypatch, broken)
    monkeypatch.setattr(inbound_module, "send_fallback", fallback)
    sid = (await open_session(chat_app.app))["session_id"]
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:
        await ws.receive_json()
        for n in (1, 2):
            await ws.send_json({"type": "message", "text": f"hello {n}", "client_id": f"c-{n}"})
            reply, _ = await ws.receive_until(lambda f: f["type"] == "message" and f["sender"] == "support")
            assert reply["text"] == FALLBACK_REPLY_NO_TICKET


@pytest.mark.parametrize(("raw", "detail"), [
    ("not json", "Send JSON text frames."),
    ('{"type": "hello"}', 'Only {"type": "message"} frames are accepted.'),
    ('{"type": "message", "text": "hi"}', "Field required"),
    ('{"type": "message", "text": "hi", "client_id": "bad id!"}', "Value error, client_id is 1-64 letters, digits, - or _"),
    ('{"type": "message", "text": "   ", "client_id": "c-1"}', "The message is empty."),
    ('{"type": "message", "text": "' + "x" * 4001 + '", "client_id": "c-1"}', "String should have at most 4000 characters"),
])
async def test_a_bad_frame_gets_an_error_and_the_socket_stays_open(chat_app, monkeypatch, raw, detail):
    async def intake(inbound):
        await web_chat.web_adapter.send(inbound.external_thread_id, "ok", {})

    fake_intake(monkeypatch, intake)
    sid = (await open_session(chat_app.app))["session_id"]
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:
        await ws.receive_json()
        await ws.send_text(raw)
        error = await ws.receive_json()
        assert error["type"] == "error" and error["detail"] == detail
        await ws.send_json({"type": "message", "text": "still here", "client_id": "c-2"})
        assert (await ws.receive_json())["client_id"] == "c-2"


async def test_a_reconnect_shows_the_chat_so_far_without_internal_notes(chat_app):
    sid = (await open_session(chat_app.app))["session_id"]
    conversation = await chat_app.session.scalar(text(
        "SELECT id FROM conversations WHERE channel = 'web' AND external_thread_id = :s"), {"s": sid})
    for n, (sender, body, internal, ext) in enumerate([
        ("customer", "My screen flickers", False, "c-1"),
        ("ai", "Which serial?", False, None),
        ("agent", "Internal: check the cable", True, None),
        ("system", "Customer says it's fixed", False, None),
        ("customer", "LB13-W4N7PC", False, "c-2"),
    ]):
        await chat_app.session.execute(text(
            """INSERT INTO messages (conversation_id, sender_type, channel, body, is_internal_note, external_message_id, created_at)
               VALUES (:c, :s, 'web', :b, :i, :e, now() + make_interval(secs => :n))"""),
            {"c": conversation, "s": sender, "b": body, "i": internal, "e": ext, "n": n})
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as ws:
        history = (await ws.receive_json())["messages"]
    assert [(m["sender"], m["text"], m["client_id"]) for m in history] == [
        ("customer", "My screen flickers", "c-1"), ("support", "Which serial?", None), ("customer", "LB13-W4N7PC", "c-2")]


async def test_every_open_tab_gets_the_reply(chat_app, monkeypatch):
    async def intake(inbound):
        await web_chat.web_adapter.send(inbound.external_thread_id, "Got it", {})

    fake_intake(monkeypatch, intake)
    sid = (await open_session(chat_app.app))["session_id"]
    # One tab at a time: the test's single database connection can't serve two connects at once.
    async with WSClient(chat_app.app, f"/ws/chat/{sid}") as one:
        await one.receive_json()
        async with WSClient(chat_app.app, f"/ws/chat/{sid}") as two:
            await two.receive_json()
            await one.send_json({"type": "message", "text": "hi", "client_id": "c-1"})
            for tab in (one, two):
                reply, _ = await tab.receive_until(lambda f: f["type"] == "message" and f["sender"] == "support")
                assert reply["text"] == "Got it"


def test_session_ids_are_masked_in_uvicorns_logs(caplog):
    web_chat.mask_chat_sessions()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5000",
                                                "/ws/chat/uhk9Q108fv1lUanwXp-sIK5eA_Iu00do")
    assert "uhk9Q108fv1l" not in caplog.text and "/ws/chat/uhk9***" in caplog.text


async def test_send_with_no_socket_open_still_succeeds():
    # The reply is stored (messages); the chat shows it on reconnect, so the outbox row is sent.
    assert (await web_chat.WebChatAdapter().send("nobody-here", "hello", {"message_id": "m-9"})) == "web-m-9"
