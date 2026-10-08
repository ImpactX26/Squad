"""Telegram adapter (ARCHITECTURE.md §6.2, §18).

Long polling inside the API's event loop: initialize(), start(), updater.start_polling(), never
run_polling(), which blocks the loop. Private chats only; the thread key is the chat id. Each
message goes to channels.inbound.run_intake, one at a time per chat (so a customer's messages are
handled in order) and concurrently across chats.

One bot token, one machine (§3): a second machine polling the same token gets HTTP 409, logged
here as a Conflict. Laptops use the dev bot; the prod bot runs only on the server.
"""

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from app.channels.base import InboundMessage

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4096  # Telegram's limit per message
POLL_TIMEOUT_SECONDS = 10
BOT_TOKEN = re.compile(r"bot\d+:[\w-]+")

OnInbound = Callable[["TelegramAdapter", InboundMessage], Awaitable[None]]


class MaskBotToken(logging.Filter):
    """httpx logs every request URL at INFO, and a Telegram URL holds the bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = BOT_TOKEN.sub("bot<token>", record.getMessage()), None
        return True


def _mask_httpx_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, MaskBotToken) for f in logger.filters):
        logger.addFilter(MaskBotToken())


def split_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Telegram refuses longer messages: split at a newline or space where possible."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text] if text else parts or [""]


def _attachments(message) -> list[dict]:
    found = []
    if message.photo:
        largest = message.photo[-1]
        found.append({"type": "photo", "file_id": largest.file_id, "file_unique_id": largest.file_unique_id})
    if message.document:
        doc = message.document
        found.append({"type": "document", "file_id": doc.file_id, "file_name": doc.file_name,
                      "mime_type": doc.mime_type})
    return found


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, token: str, on_inbound: OnInbound, *, httpx_kwargs: dict | None = None) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        _mask_httpx_logs()
        builder = ApplicationBuilder().token(token).concurrent_updates(True)
        if httpx_kwargs is not None:  # tests: an httpx.MockTransport instead of api.telegram.org
            builder = (builder.request(HTTPXRequest(httpx_kwargs=httpx_kwargs))
                       .get_updates_request(HTTPXRequest(httpx_kwargs=httpx_kwargs)))
        self._app: Application = builder.build()
        self._app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.Document.ALL), self._on_message))
        self._app.add_error_handler(self._on_error)
        self._on_inbound = on_inbound
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.username: str | None = None

    async def start(self) -> None:
        await self._app.initialize()  # getMe: a wrong token fails here
        self.username = self._app.bot.username
        await self._app.start()
        await self._app.updater.start_polling(timeout=POLL_TIMEOUT_SECONDS, allowed_updates=["message"],
                                              error_callback=self._on_polling_error)
        log.info("telegram bot @%s is polling", self.username)

    async def stop(self) -> None:
        if self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        sent = None
        for part in split_text(text):
            sent = await self._app.bot.send_message(chat_id=int(thread_id), text=part)
        return str(sent.message_id)

    async def typing(self, thread_id: str) -> None:
        await self._app.bot.send_chat_action(chat_id=int(thread_id), action=ChatAction.TYPING)

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or chat is None or user.is_bot:
            return
        inbound = InboundMessage(
            channel="telegram",
            external_user_id=str(user.id),
            external_thread_id=str(chat.id),
            display_name=user.full_name or user.username,
            text=message.text or message.caption or "",
            attachments=_attachments(message),
            external_message_id=str(message.message_id),
            raw_meta={"username": user.username, "language_code": user.language_code,
                      "update_id": update.update_id},
        )
        async with self._chat_locks[chat.id]:
            await self._on_inbound(self, inbound)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram handler failed: %s", BOT_TOKEN.sub("bot<token>", repr(context.error)))

    def _on_polling_error(self, error: TelegramError) -> None:
        if isinstance(error, Conflict):
            # ASCII only: a Windows console garbles the section sign.
            log.error("telegram: another process is polling this bot token (HTTP 409). "
                      "One bot token, one machine: use the dev bot on laptops (ARCHITECTURE.md 3, 18.1).")
        else:
            log.warning("telegram polling: %s", BOT_TOKEN.sub("bot<token>", repr(error)))
