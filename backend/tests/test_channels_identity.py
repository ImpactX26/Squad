"""identity.resolve() and set_context() against the real database.

Most tests run inside a transaction that is rolled back (tests/api_support.py). The concurrency
test needs real commits on two connections, so it deletes what it created.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.channels import identity
from app.channels.base import InboundMessage
from app.core.db import get_engine, get_sessionmaker
from app.core.events import bus
from tests.api_support import session  # noqa: F401 (fixture)


def inbound(channel="telegram", user=None, thread=None, text_="My laptop won't charge", message_id=None,
            name="Test Person", meta=None) -> InboundMessage:
    user = user or f"test-{uuid.uuid4().hex[:10]}"
    return InboundMessage(channel=channel, external_user_id=user, external_thread_id=thread or user,
                          display_name=name, text=text_, attachments=[],
                          external_message_id=message_id or uuid.uuid4().hex, raw_meta=meta or {})


def events_of(listener, kind="message.received") -> list[dict]:
    seen = []
    while True:
        try:
            event = listener._queue.get_nowait()
        except asyncio.QueueEmpty:
            return seen
        if event is not None and event.type == kind:
            seen.append(event.data)


async def test_a_new_telegram_account_gets_a_placeholder_customer_and_a_conversation(session):
    message = inbound()
    with bus.listen() as listener:
        r = await identity.resolve(message, session)
    assert (r.new_customer, r.new_conversation, r.repeat) == (True, True, False)
    assert (r.customer_name, r.customer_email, r.ticket_id, r.context) == ("Test Person", None, None, {})
    stored = (await session.execute(text(
        "SELECT conversation_id, ticket_id, sender_type, channel, body, external_message_id FROM messages WHERE id = :id"),
        {"id": r.message_id})).one()
    assert stored == (r.conversation_id, None, "customer", "telegram", "My laptop won't charge",
                      message.external_message_id)
    linked = await session.scalar(text(
        "SELECT customer_id FROM customer_identities WHERE channel = 'telegram' AND external_user_id = :u"),
        {"u": message.external_user_id})
    assert linked == r.customer_id
    [event] = events_of(listener)
    assert event["message_id"] == str(r.message_id) and event["channel"] == "telegram"
    assert event["ticket_id"] is None and event["body"] == "My laptop won't charge"


async def test_the_next_message_reuses_the_customer_and_the_conversation(session):
    first = await identity.resolve(inbound(user="test-same-user"), session)
    second = await identity.resolve(inbound(user="test-same-user", text_="AX14-7F3K92"), session)
    assert (second.customer_id, second.conversation_id) == (first.customer_id, first.conversation_id)
    assert (second.new_customer, second.new_conversation, second.repeat) == (False, False, False)
    assert second.message_id != first.message_id


async def test_a_redelivered_message_is_stored_once_and_not_announced(session):
    message = inbound()
    first = await identity.resolve(message, session)
    with bus.listen() as listener:
        again = await identity.resolve(message, session)
    assert again.repeat and again.message_id == first.message_id
    assert events_of(listener) == []
    count = await session.scalar(text("SELECT count(*) FROM messages WHERE conversation_id = :c"),
                                 {"c": first.conversation_id})
    assert count == 1


async def test_email_links_to_the_customer_with_that_address_whatever_its_case(session):
    tag = uuid.uuid4().hex[:8]
    existing = await session.scalar(text(
        "INSERT INTO customers (full_name, email) VALUES ('Real Name', :e) RETURNING id"), {"e": f"real-{tag}@example.com"})
    r = await identity.resolve(inbound(channel="email", user=f"  Real-{tag}@Example.com ",
                                       thread=f"<root-{tag}@mail>", name="Someone Else"), session)
    assert (r.customer_id, r.new_customer) == (existing, False)
    assert r.customer_name == "Real Name"  # never renamed from a message


async def test_web_links_by_the_pre_chat_email_and_creates_a_new_customer_once(session):
    tag = uuid.uuid4().hex[:8]
    email = f"web-{tag}@example.com"
    first = await identity.resolve(inbound(channel="web", user=f"s1-{tag}", name="Web Person",
                                           meta={"email": email}), session)
    other_session = await identity.resolve(inbound(channel="web", user=f"s2-{tag}", meta={"email": email.upper()}),
                                           session)
    assert first.new_customer and (first.customer_name, first.customer_email) == ("Web Person", email)
    assert other_session.customer_id == first.customer_id and not other_session.new_customer
    assert other_session.conversation_id != first.conversation_id  # one conversation per web session


async def test_set_context_is_read_back_by_the_next_message(session):
    first = await identity.resolve(inbound(user="test-context-user"), session)
    await identity.set_context(first.conversation_id, {"awaiting": "serial_number", "misses": 0}, session)
    second = await identity.resolve(inbound(user="test-context-user"), session)
    assert second.context == {"awaiting": "serial_number", "misses": 0}
    await identity.set_context(first.conversation_id, {}, session)
    assert (await identity.resolve(inbound(user="test-context-user"), session)).context == {}


async def test_set_context_on_an_unknown_conversation_is_refused(session):
    with pytest.raises(LookupError):
        await identity.set_context(uuid.uuid4(), {"awaiting": "serial_number"}, session)


@pytest.mark.parametrize("message", [inbound(channel="sms"), inbound(user=" ")])
async def test_bad_input_is_refused_before_the_database(message):
    with pytest.raises(ValueError):
        await identity.resolve(message)


async def test_two_messages_at_once_from_a_new_account_make_one_customer(session):
    # `session` only makes the test skip when the database is unreachable.
    user = f"test-race-{uuid.uuid4().hex[:8]}"
    try:
        a, b = await asyncio.gather(identity.resolve(inbound(user=user)), identity.resolve(inbound(user=user)))
        assert a.customer_id == b.customer_id and a.conversation_id == b.conversation_id
        assert sorted([a.new_customer, b.new_customer]) == [False, True]
    finally:
        async with get_sessionmaker()() as db:
            customers = (await db.scalars(text(
                "SELECT customer_id FROM customer_identities WHERE channel = 'telegram' AND external_user_id = :u"),
                {"u": user})).all()
            await db.execute(text("DELETE FROM messages WHERE conversation_id IN "
                                  "(SELECT id FROM conversations WHERE channel = 'telegram' AND external_thread_id = :u)"),
                             {"u": user})
            await db.execute(text("DELETE FROM conversations WHERE channel = 'telegram' AND external_thread_id = :u"),
                             {"u": user})
            await db.execute(text("DELETE FROM customers WHERE id = ANY(:ids)"), {"ids": list(customers)})
            await db.commit()
        await get_engine().dispose()  # its connections belong to this test's event loop
