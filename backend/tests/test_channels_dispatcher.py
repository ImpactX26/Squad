"""The outbox dispatcher against the real database, inside a transaction that is rolled back.

The dispatcher commits between claiming a row and recording its outcome; here those commits are
savepoints of one outer transaction, so the shared dev database is left as it was.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels import lifespan
from app.channels.base import Adapters
from app.channels.dispatcher import MAX_ATTEMPTS, Dispatcher, SimulatedSink
from app.core.config import Settings
from app.core.events import bus
from tests.api_support import session  # noqa: F401 (fixture)


class FakeAdapter:
    def __init__(self, channel="telegram", fail_with: Exception | None = None, delay: float = 0) -> None:
        self.channel = channel
        self.fail_with = fail_with
        self.delay = delay
        self.sent: list[tuple[str, str, dict]] = []

    async def start(self) -> None: ...

    async def typing(self, thread_id: str) -> None: ...

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((thread_id, text, meta))
        return f"ext-{len(self.sent)}"


def dispatcher_for(session, *adapters_, sink=None) -> Dispatcher:
    registry = Adapters()
    for adapter in adapters_:
        registry.add(adapter)
    # Every session the dispatcher opens joins the test's transaction; its commits are savepoints.
    maker = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False)
    return Dispatcher(registry, maker, sink=sink)


async def queue_reply(session, channel="telegram", text_="Your ticket is SR-2026-00001.") -> SimpleNamespace:
    """A conversation with one queued reply, as messaging.send_reply leaves it."""
    tag = uuid.uuid4().hex[:8]
    customer = await session.scalar(text("INSERT INTO customers (full_name) VALUES ('Outbox Test') RETURNING id"))
    conversation = await session.scalar(text(
        "INSERT INTO conversations (customer_id, channel, external_thread_id) VALUES (:c, :ch, :t) RETURNING id"),
        {"c": customer, "ch": channel, "t": f"thread-{tag}"})
    message = await session.scalar(text(
        "INSERT INTO messages (conversation_id, sender_type, channel, body) VALUES (:c, 'ai', :ch, :b) RETURNING id"),
        {"c": conversation, "ch": channel, "b": text_})
    outbox = await session.scalar(text(
        "INSERT INTO outbox (conversation_id, message_id, payload) "
        "VALUES (:c, :m, jsonb_build_object('kind', 'reply', 'text', CAST(:b AS text))) RETURNING id"),
        {"c": conversation, "m": message, "b": text_})
    return SimpleNamespace(conversation=conversation, message=message, outbox=outbox, thread=f"thread-{tag}")


async def make_due(session, outbox_id) -> None:
    """Skip the retry back-off."""
    await session.execute(text("UPDATE outbox SET created_at = now() - interval '1 hour' WHERE id = :id"),
                          {"id": outbox_id})


async def test_a_reply_goes_to_the_conversations_channel_and_thread(session):
    row = await queue_reply(session)
    telegram, web = FakeAdapter("telegram"), FakeAdapter("web")
    dispatcher = dispatcher_for(session, telegram, web)
    with bus.listen() as listener:
        assert await dispatcher.deliver_pending(row.conversation) == 1
    assert web.sent == []
    [(thread, body, meta)] = telegram.sent
    assert (thread, body) == (row.thread, "Your ticket is SR-2026-00001.")
    assert meta["kind"] == "reply" and meta["message_id"] == str(row.message)
    outcome = await dispatcher.outcome_of(row.outbox)
    assert (outcome.status, outcome.attempts, outcome.last_error) == ("sent", 1, None)
    assert await session.scalar(text("SELECT external_message_id FROM messages WHERE id = :id"),
                                {"id": row.message}) == "ext-1"
    event = listener._queue.get_nowait()
    assert event.type == "message.sent"
    assert event.data["outbox_id"] == str(row.outbox) and event.data["channel"] == "telegram"
    # Nothing is sent twice.
    assert await dispatcher.deliver_pending(row.conversation) == 0 and len(telegram.sent) == 1


async def test_a_failing_send_is_retried_after_a_back_off_then_marked_failed(session):
    row = await queue_reply(session)
    broken = FakeAdapter(fail_with=RuntimeError("POST https://api.telegram.org/bot123456:AAH-secret_x/sendMessage: 502"))
    dispatcher = dispatcher_for(session, broken)
    assert await dispatcher.deliver_pending(row.conversation) == 0
    first = await dispatcher.outcome_of(row.outbox)
    assert (first.status, first.attempts) == ("pending", 1)
    assert "bot<token>" in first.last_error and "AAH-secret" not in first.last_error
    await dispatcher.deliver_pending(row.conversation)  # too soon: the back-off holds it
    assert (await dispatcher.outcome_of(row.outbox)).attempts == 1
    for _ in range(MAX_ATTEMPTS - 1):
        await make_due(session, row.outbox)
        await dispatcher.deliver_pending(row.conversation)
    final = await dispatcher.outcome_of(row.outbox)
    assert (final.status, final.attempts) == ("failed", MAX_ATTEMPTS)
    await make_due(session, row.outbox)
    await dispatcher.deliver_pending(row.conversation)
    assert (await dispatcher.outcome_of(row.outbox)).attempts == MAX_ATTEMPTS  # failed rows are left alone


async def test_a_channel_with_no_adapter_waits_and_retries(session):
    row = await queue_reply(session, channel="discord")
    dispatcher = dispatcher_for(session, FakeAdapter("telegram"))
    assert await dispatcher.deliver_pending(row.conversation) == 0
    outcome = await dispatcher.outcome_of(row.outbox)
    assert outcome.status == "pending" and "no discord adapter" in outcome.last_error


async def test_a_switched_off_channel_goes_to_the_simulated_sink(session):
    row = await queue_reply(session, channel="discord")
    sink = SimulatedSink({"discord", "email"})
    dispatcher = dispatcher_for(session, FakeAdapter("telegram"), sink=sink)
    assert await dispatcher.deliver_pending(row.conversation) == 1
    [reply] = sink.replies(row.conversation)
    assert (reply["channel"], reply["thread_id"], reply["text"]) == ("discord", row.thread, "Your ticket is SR-2026-00001.")
    assert (await dispatcher.outcome_of(row.outbox)).status == "sent"


async def test_a_request_path_delivers_only_its_own_conversation(session):
    mine, theirs = await queue_reply(session, text_="mine"), await queue_reply(session, text_="theirs")
    telegram = FakeAdapter()
    dispatcher = dispatcher_for(session, telegram)
    await dispatcher.deliver_pending(mine.conversation)
    assert [body for _, body, _ in telegram.sent] == ["mine"]
    assert (await dispatcher.outcome_of(theirs.outbox)).status == "pending"


async def test_two_ticks_at_once_send_a_reply_once(session):
    row = await queue_reply(session)
    slow = FakeAdapter(delay=0.2)
    dispatcher = dispatcher_for(session, slow)
    results = await asyncio.gather(dispatcher.deliver_pending(row.conversation),
                                   dispatcher.deliver_pending(row.conversation))
    assert sorted(results) == [0, 1] and len(slow.sent) == 1


async def test_email_rows_without_a_conversation_are_left_to_the_email_channel(session):
    outbox = await session.scalar(text(
        "INSERT INTO outbox (payload) VALUES ('{\"kind\": \"email\", \"to\": \"x@example.com\"}') RETURNING id"))
    await make_due(session, outbox)
    dispatcher = dispatcher_for(session, FakeAdapter("email"))
    await dispatcher.deliver_pending()
    outcome = await dispatcher.outcome_of(outbox)
    assert (outcome.status, outcome.attempts) == ("pending", 0)


class Ticker(Dispatcher):
    """A dispatcher without a database: counts ticks, optionally failing or slow."""

    def __init__(self, fail_first=False, tick_seconds=0.0):
        self.ticks = self.finished = 0
        self.fail_first = fail_first
        self.tick_seconds = tick_seconds

    async def deliver_pending(self, conversation_id=None):
        self.ticks += 1
        if self.fail_first and self.ticks == 1:
            raise OSError("database down")
        await asyncio.sleep(self.tick_seconds)
        self.finished += 1
        return 0


async def test_the_loop_keeps_ticking_through_errors_until_stopped():
    ticker, stop = Ticker(fail_first=True), asyncio.Event()
    task = asyncio.create_task(ticker.run(stop, interval=0.01))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert ticker.ticks > 2


async def test_a_stop_during_a_tick_lets_the_tick_finish():
    ticker, stop = Ticker(tick_seconds=0.2), asyncio.Event()
    task = asyncio.create_task(ticker.run(stop, interval=0.01))
    while ticker.ticks == 0:
        await asyncio.sleep(0.005)
    stop.set()  # mid-tick
    await asyncio.wait_for(task, 1)
    assert ticker.ticks == ticker.finished == 1


async def test_the_loop_waits_one_interval_before_its_first_tick():
    # So an app started and stopped at once (TestClient) never touches the database.
    ticker, stop = Ticker(), asyncio.Event()
    task = asyncio.create_task(ticker.run(stop, interval=5))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert ticker.ticks == 0


async def test_the_lifespan_runs_the_loop_and_gives_switched_off_channels_to_the_sink(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()

    class Recorder:
        sink = None

        async def run(self, stop):
            started.set()
            await stop.wait()
            stopped.set()

    recorder = Recorder()
    monkeypatch.setattr(lifespan, "get_dispatcher", lambda: recorder)
    app = SimpleNamespace(state=SimpleNamespace(settings=Settings(_env_file=None, enable_telegram=True)))
    async with lifespan.channels_lifespan(app):
        await asyncio.wait_for(started.wait(), 1)
        assert recorder.sink.channels == {"discord", "email"}
    assert stopped.is_set()


@pytest.mark.parametrize(("env", "expect_sink"), [("development", True), ("production", False)])
async def test_no_sink_outside_development(monkeypatch, env, expect_sink):
    class Recorder:
        sink = None

        async def run(self, stop):
            await stop.wait()

    recorder = Recorder()
    monkeypatch.setattr(lifespan, "get_dispatcher", lambda: recorder)
    app = SimpleNamespace(state=SimpleNamespace(settings=Settings(_env_file=None, app_env=env)))
    async with lifespan.channels_lifespan(app):
        assert (recorder.sink is not None) == expect_sink
