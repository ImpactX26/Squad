"""The Telegram adapter against a fake Bot API (tests/telegram_fakes.py). No real Telegram call."""

import asyncio
import logging

import pytest
from telegram.error import Conflict

from app.channels.base import InboundMessage
from app.channels.telegram_bot import TelegramAdapter, split_text
from tests.telegram_fakes import TOKEN, FakeTelegram, update


class Inbox:
    """Records what the adapter hands over; optionally slow, to check the per-chat order."""

    def __init__(self, delay: float = 0) -> None:
        self.got: list[InboundMessage] = []
        self.events: list[str] = []
        self.delay = delay

    async def __call__(self, adapter, inbound: InboundMessage) -> None:
        self.events.append(f"start {inbound.text}")
        await asyncio.sleep(self.delay)
        self.got.append(inbound)
        self.events.append(f"end {inbound.text}")

    async def wait_for(self, n: int, timeout: float = 3) -> None:
        async with asyncio.timeout(timeout):
            while len(self.got) < n:
                await asyncio.sleep(0.01)


@pytest.fixture
async def running():
    fake, inbox = FakeTelegram(), Inbox()
    adapter = TelegramAdapter(TOKEN, inbox, httpx_kwargs={"transport": fake.transport()})
    await adapter.start()
    try:
        yield fake, inbox, adapter
    finally:
        await adapter.stop()


async def test_a_private_message_becomes_an_inbound_message(running):
    fake, inbox, adapter = running
    assert adapter.username == "aurora_test_bot"
    fake.queue(update(1, "My Vertex 15 won't charge, serial VX15-Q8M2D5", message_id=42))
    await inbox.wait_for(1)
    [m] = inbox.got
    assert (m.channel, m.external_user_id, m.external_thread_id) == ("telegram", "555", "555")
    assert (m.display_name, m.text, m.external_message_id) == ("Aman Verma", "My Vertex 15 won't charge, serial VX15-Q8M2D5", "42")
    assert m.attachments == [] and m.raw_meta["username"] == "amanv"
    assert fake.sent("deleteWebhook")  # long polling, not a webhook


async def test_group_chats_and_other_bots_are_ignored(running):
    fake, inbox, _ = running
    fake.queue(update(1, "in a group", chat_id=-100, chat_type="group"), update(2, "private one"))
    await inbox.wait_for(1)
    await asyncio.sleep(0.1)
    assert [m.text for m in inbox.got] == ["private one"]


async def test_a_photo_with_a_caption_keeps_the_caption_and_the_file_id(running):
    fake, inbox, _ = running
    photo = [{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
             {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 1280}]
    fake.queue(update(1, None, photo=photo, caption="the screen looks like this"))
    await inbox.wait_for(1)
    [m] = inbox.got
    assert m.text == "the screen looks like this"
    assert m.attachments == [{"type": "photo", "file_id": "large", "file_unique_id": "l"}]


async def test_one_chat_in_order_two_chats_at_once():
    fake, inbox = FakeTelegram(), Inbox(delay=0.2)
    adapter = TelegramAdapter(TOKEN, inbox, httpx_kwargs={"transport": fake.transport()})
    await adapter.start()
    try:
        fake.queue(update(1, "a1", chat_id=1, user_id=1), update(2, "a2", chat_id=1, user_id=1),
                   update(3, "b1", chat_id=2, user_id=2))
        await inbox.wait_for(3)
    finally:
        await adapter.stop()
    assert inbox.events.index("end a1") < inbox.events.index("start a2")  # one chat: in order
    assert inbox.events.index("start b1") < inbox.events.index("end a1")  # other chats don't wait


async def test_send_replies_in_the_chat_and_splits_long_text(running):
    fake, _, adapter = running
    first = await adapter.send("555", "Your ticket is SR-2026-00042.", {"kind": "reply"})
    assert first.isdigit()
    await adapter.send("555", "word " * 1000, {})  # 5000 characters
    texts = [p["text"] for p in fake.sent("sendMessage")]
    assert texts[0] == "Your ticket is SR-2026-00042."
    assert len(texts) == 3 and all(len(t) <= 4096 for t in texts)
    assert {p["chat_id"] for p in fake.sent("sendMessage")} == {"555"}
    assert "parse_mode" not in fake.sent("sendMessage")[0]  # plain text: a model's markdown can't break it


async def test_typing_sends_the_typing_action(running):
    fake, _, adapter = running
    await adapter.typing("555")
    assert fake.sent("sendChatAction") == [{"chat_id": "555", "action": "typing"}]


def test_split_text():
    assert split_text("short") == ["short"]
    assert split_text("") == [""]
    parts = split_text("aaaa bbbb cccc", limit=10)
    assert parts == ["aaaa bbbb", "cccc"]
    assert split_text("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        TelegramAdapter("", Inbox())


def test_the_bot_token_never_reaches_the_logs(caplog):
    TelegramAdapter(TOKEN, Inbox())  # installs the httpx filter
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "HTTP/1.1 200 OK"', "POST",
                                        f"https://api.telegram.org/bot{TOKEN}/getUpdates")
    assert "TEST-not-a-real-token" not in caplog.text and "bot<token>/getUpdates" in caplog.text


def test_a_409_conflict_says_one_token_one_machine(caplog):
    adapter = TelegramAdapter(TOKEN, Inbox())
    with caplog.at_level(logging.ERROR):
        adapter._on_polling_error(Conflict("terminated by other getUpdates request"))
    assert "One bot token, one machine" in caplog.text
