"""POST /api/dev/simulate and GET /api/dev/channels, inside a transaction that is rolled back.

Intake is replaced by a fake that stores a reply the way messaging.send_reply does, so the route's
own work is what's tested: the inbound message, the dispatcher's flush into the simulated sink,
and the response. Intake itself is tests/test_intake.py.
"""

import uuid

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import dev
from app.channels import identity
from app.channels import inbound as inbound_module
from app.channels.base import adapters
from app.channels.dispatcher import Dispatcher, SimulatedSink
from app.main import create_app
from tests.api_support import add_staff, app, bearer, client, session, settings  # noqa: F401 (fixtures)

REPLY = "Thanks. To open a ticket I need your device's serial number."


@pytest.fixture
def dispatcher(session):  # noqa: F811
    """The real dispatcher on the test's connection, with telegram switched off (a sink)."""
    maker = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False)
    return Dispatcher(adapters, maker, sink=SimulatedSink({"telegram", "discord", "email"}))


@pytest.fixture
def wired(app, session, dispatcher):  # noqa: F811
    """The app with the fake intake and the test dispatcher; returns what the fake intake saw."""
    seen: list = []

    async def fake_intake(message):
        seen.append(message)
        resolved = await identity.resolve(message, session)
        message_id = await session.scalar(text(
            """INSERT INTO messages (conversation_id, sender_type, channel, body)
               VALUES (:c, 'ai', :ch, :body) RETURNING id"""),
            {"c": resolved.conversation_id, "ch": message.channel, "body": REPLY})
        await session.execute(text(
            "INSERT INTO outbox (conversation_id, message_id, payload) VALUES (:c, :m, :p)").bindparams(
            bindparam("p", type_=JSONB)), {"c": resolved.conversation_id, "m": message_id,
                                           "p": {"kind": "reply", "text": REPLY}})
        await identity.set_context(resolved.conversation_id, {"awaiting": "serial_number"}, session)

    app.dependency_overrides[dev.get_intake] = lambda: fake_intake
    app.dependency_overrides[dev.get_dev_dispatcher] = lambda: dispatcher
    return seen


async def test_simulate_runs_intake_and_returns_the_reply_from_the_sink(app, session, wired):  # noqa: F811
    staff = await add_staff(session)
    async with client(app) as c:
        response = await c.post("/api/dev/simulate", headers=bearer(staff),
                                json={"channel": "telegram", "text": "  My laptop won't charge ", "display_name": "Aman"})
    assert response.status_code == 200, response.text
    body = response.json()
    [message] = wired
    assert (message.channel, message.text, message.display_name) == ("telegram", "My laptop won't charge", "Aman")
    assert message.external_user_id.startswith("sim-") and message.external_thread_id == message.external_user_id
    assert message.raw_meta == {"simulated": True}
    assert (body["channel"], body["external_user_id"], body["delivery"]) == ("telegram", message.external_user_id,
                                                                             "simulated")
    assert body["awaiting"] == "serial_number" and body["ticket"] is None and body["intake_error"] is None
    [reply] = body["replies"]
    assert (reply["text"], reply["status"], reply["simulated"], reply["last_error"]) == (REPLY, "sent", True, None)
    [sunk] = dispatcher_sink(app).replies(body["conversation_id"])
    assert sunk["text"] == REPLY and sunk["thread_id"] == message.external_user_id
    stored = await session.scalar(text("SELECT body FROM messages WHERE id = :id"), {"id": uuid.UUID(body["message_id"])})
    assert stored == "My laptop won't charge"


def dispatcher_sink(app) -> SimulatedSink:
    return app.dependency_overrides[dev.get_dev_dispatcher]().sink


async def test_the_ids_from_a_response_continue_the_same_conversation(app, session, wired):  # noqa: F811
    staff = await add_staff(session)
    async with client(app) as c:
        first = (await c.post("/api/dev/simulate", headers=bearer(staff),
                              json={"channel": "telegram", "text": "battery dead"})).json()
        second = (await c.post("/api/dev/simulate", headers=bearer(staff), json={
            "channel": "telegram", "text": "VX15-Q8M2D5", "external_user_id": first["external_user_id"],
            "external_thread_id": first["external_thread_id"]})).json()
    assert second["conversation_id"] == first["conversation_id"] and second["customer_id"] == first["customer_id"]
    assert len(second["replies"]) == 1  # only this message's reply


async def test_web_links_the_customer_by_email_and_email_needs_an_address(app, session, wired):  # noqa: F811
    staff = await add_staff(session)
    async with client(app) as c:
        web = await c.post("/api/dev/simulate", headers=bearer(staff),
                           json={"channel": "web", "text": "hello", "email": "Someone.Test@Example.com"})
        no_address = await c.post("/api/dev/simulate", headers=bearer(staff), json={"channel": "email", "text": "hi"})
        mail = await c.post("/api/dev/simulate", headers=bearer(staff),
                            json={"channel": "email", "text": "hi", "email": "Writer@Example.com", "subject": "Help"})
    assert web.status_code == 200 and wired[0].raw_meta == {"simulated": True, "email": "Someone.Test@example.com"}
    email = await session.scalar(text("SELECT email FROM customers WHERE id = :id"),
                                 {"id": uuid.UUID(web.json()["customer_id"])})
    assert email == "someone.test@example.com"
    assert no_address.status_code == 422
    assert mail.status_code == 200 and wired[1].external_user_id == "writer@example.com"
    assert wired[1].raw_meta == {"simulated": True, "subject": "Help"}


async def test_a_failing_intake_is_reported_and_the_fallback_sent(app, session, dispatcher, monkeypatch):  # noqa: F811
    fallbacks = []

    async def broken_intake(message):
        await identity.resolve(message, session)
        raise RuntimeError("tickets server down")

    async def fake_fallback(adapter, message):
        fallbacks.append((adapter.channel, message.text))
        return "outbox"

    monkeypatch.setattr(inbound_module, "send_fallback", fake_fallback)
    app.dependency_overrides[dev.get_intake] = lambda: broken_intake
    app.dependency_overrides[dev.get_dev_dispatcher] = lambda: dispatcher
    staff = await add_staff(session)
    async with client(app) as c:
        response = await c.post("/api/dev/simulate", headers=bearer(staff), json={"channel": "discord", "text": "hi"})
    assert response.status_code == 200
    assert response.json()["intake_error"] == "RuntimeError: tickets server down"
    assert fallbacks == [("discord", "hi")]


async def test_simulate_needs_a_staff_login_and_text(app, session, wired):  # noqa: F811
    staff = await add_staff(session, role="technician")
    async with client(app) as c:
        signed_out = await c.post("/api/dev/simulate", json={"channel": "telegram", "text": "hi"})
        blank = await c.post("/api/dev/simulate", headers=bearer(staff), json={"channel": "telegram", "text": "   "})
        bad_channel = await c.post("/api/dev/simulate", headers=bearer(staff), json={"channel": "sms", "text": "hi"})
    assert (signed_out.status_code, blank.status_code, bad_channel.status_code) == (401, 422, 422)
    assert wired == []


async def test_channels_lists_connected_enabled_and_simulated(app, session, wired):  # noqa: F811
    class Web:
        channel = "web"

    staff = await add_staff(session, role="warehouse")
    adapters.add(Web())
    try:
        async with client(app) as c:
            response = await c.get("/api/dev/channels", headers=bearer(staff))
            signed_out = await c.get("/api/dev/channels")
    finally:
        adapters.remove("web")
    assert response.status_code == 200
    assert response.json() == {"connected": ["web"], "enabled": ["web"],
                               "simulated": ["discord", "email", "telegram"]}
    assert signed_out.status_code == 401


async def test_outside_development_the_dev_routes_are_not_found(session):  # noqa: F811
    production = create_app(settings(app_env="production"))
    staff = await add_staff(session)

    async def same_session():
        yield session

    from app.core.db import get_session

    production.dependency_overrides[get_session] = same_session
    async with client(production) as c:
        simulate = await c.post("/api/dev/simulate", headers=bearer(staff), json={"channel": "telegram", "text": "hi"})
        channels = await c.get("/api/dev/channels", headers=bearer(staff))
    assert (simulate.status_code, channels.status_code) == (404, 404)
