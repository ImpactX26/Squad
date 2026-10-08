"""channels.inbound (typing while intake runs, never silence) and the lifespan's adapter start-up."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken

from app.channels import inbound as inbound_module
from app.channels import lifespan
from app.channels.base import InboundMessage, adapters
from app.channels.inbound import FALLBACK_REPLY_NO_TICKET, IntakeNotBuilt, run_intake
from app.core.config import Settings


class FakeAdapter:
    channel = "telegram"

    def __init__(self, fail_starts: int = 0, start_error: Exception | None = None) -> None:
        self.typed = 0
        self.sent: list[tuple[str, str]] = []
        self.starts = self.stops = 0
        self.fail_starts = fail_starts
        self.start_error = start_error

    async def start(self) -> None:
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error
        if self.starts <= self.fail_starts:
            raise ConnectionError("no network")

    async def stop(self) -> None:
        self.stops += 1

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        self.sent.append((thread_id, text))
        return "1"

    async def typing(self, thread_id: str) -> None:
        self.typed += 1


def message() -> InboundMessage:
    return InboundMessage("telegram", "555", "555", "Aman", "hello", [], uuid.uuid4().hex, {})


@pytest.fixture
def fallback_route(monkeypatch):
    """Fake identity, hub and dispatcher for the fallback's outbox route; records what it did."""
    seen = SimpleNamespace(replies=[], delivered=[], hub_ok=True, hub_raises=None)
    conversation = uuid.uuid4()

    async def resolve(inbound):
        return SimpleNamespace(conversation_id=conversation)

    class Hub:
        async def call_tool(self, name, arguments, *, allowed):
            assert name == "messaging__send_reply" and allowed == frozenset({"messaging__send_reply"})
            if seen.hub_raises:
                raise seen.hub_raises
            seen.replies.append(arguments)
            return SimpleNamespace(ok=seen.hub_ok, error={"error": "x"})

    class Dispatcher:
        async def deliver_pending(self, conversation_id=None):
            seen.delivered.append(conversation_id)

    monkeypatch.setattr(inbound_module.identity, "resolve", resolve)
    monkeypatch.setattr("app.brain.mcp_hub.get_hub", lambda: Hub())
    monkeypatch.setattr(inbound_module, "get_dispatcher", lambda: Dispatcher())
    seen.conversation = conversation
    return seen


async def test_typing_shows_while_intake_runs_and_stops_after(monkeypatch):
    monkeypatch.setattr(inbound_module, "TYPING_EVERY_SECONDS", 0.02)
    adapter, ran = FakeAdapter(), []

    async def intake(inbound):
        await asyncio.sleep(0.1)
        ran.append(inbound.text)

    await run_intake(adapter, message(), intake)
    typed = adapter.typed
    await asyncio.sleep(0.05)
    assert ran == ["hello"] and typed >= 3 and adapter.typed == typed  # stopped with intake
    assert adapter.sent == []


async def test_a_failing_intake_gets_the_fallback_through_the_outbox(fallback_route):
    adapter = FakeAdapter()

    async def broken(inbound):
        raise KeyError("boom")

    await run_intake(adapter, message(), broken)
    assert fallback_route.replies == [{"conversation_id": str(fallback_route.conversation),
                                       "text": FALLBACK_REPLY_NO_TICKET}]
    assert fallback_route.delivered == [fallback_route.conversation]
    assert adapter.sent == []  # it went through the outbox, not around it


async def test_intake_not_built_yet_still_answers(fallback_route, monkeypatch):
    def not_built():
        raise IntakeNotBuilt("app.brain.intake.handle_inbound is not built yet")

    monkeypatch.setattr(inbound_module, "_intake", not_built)
    await run_intake(FakeAdapter(), message())
    assert [r["text"] for r in fallback_route.replies] == [FALLBACK_REPLY_NO_TICKET]


@pytest.mark.parametrize("failure", ["refused", "raises"])
async def test_with_the_outbox_route_down_the_fallback_goes_straight_to_the_thread(fallback_route, failure):
    if failure == "refused":
        fallback_route.hub_ok = False
    else:
        fallback_route.hub_raises = OSError("messaging server down")
    adapter = FakeAdapter()

    async def broken(inbound):
        raise RuntimeError("boom")

    await run_intake(adapter, message(), broken)
    assert adapter.sent == [("555", FALLBACK_REPLY_NO_TICKET)]


async def test_run_intake_never_raises_even_when_the_fallback_fails(fallback_route):
    fallback_route.hub_raises = OSError("down")

    class Mute(FakeAdapter):
        async def send(self, thread_id, text, meta):
            raise OSError("telegram down too")

    async def broken(inbound):
        raise RuntimeError("boom")

    await run_intake(Mute(), message(), broken)  # logged, not raised


# ---------- the lifespan's adapters ----------


async def test_connect_retries_until_the_adapter_is_up_then_registers_it():
    adapter, stop = FakeAdapter(fail_starts=2), asyncio.Event()
    try:
        await asyncio.wait_for(lifespan.connect(adapter, stop, retry_seconds=0.01), 2)
        assert adapter.starts == 3 and adapter.stops == 2  # cleaned up after each failed start
        assert adapters.get("telegram") is adapter
    finally:
        adapters.remove("telegram")


async def test_a_refused_token_is_not_retried():
    adapter = FakeAdapter(start_error=InvalidToken("Not Found"))
    await asyncio.wait_for(lifespan.connect(adapter, asyncio.Event(), retry_seconds=0.01), 1)
    assert adapter.starts == 1 and adapters.get("telegram") is None


async def test_connect_gives_up_when_the_app_stops():
    adapter, stop = FakeAdapter(fail_starts=1000), asyncio.Event()
    task = asyncio.create_task(lifespan.connect(adapter, stop, retry_seconds=10))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert adapters.get("telegram") is None


def test_telegram_is_built_only_when_switched_on_with_a_token():
    assert lifespan.build_adapters(Settings(_env_file=None, enable_telegram=False, telegram_bot_token="1:x")) == []
    assert lifespan.build_adapters(Settings(_env_file=None, enable_telegram=True, telegram_bot_token="")) == []
    [built] = lifespan.build_adapters(Settings(_env_file=None, enable_telegram=True, telegram_bot_token="1:x"))
    assert built.channel == "telegram"
