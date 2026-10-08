"""The Telegram channel (ARCHITECTURE.md §6.2).

Long polling inside the backend's own event loop:

    await app.initialize(); await app.start(); await app.updater.start_polling()

`run_polling()` is never used -- it installs signal handlers and runs its own loop, which would
block FastAPI's. The thread key is the Telegram `chat_id`, and the typing indicator is
`send_chat_action("typing")` while the brain works (§4.5).

It starts only when ENABLE_TELEGRAM is true **and** TELEGRAM_BOT_TOKEN is set, so on any machine
but the demo host (§15) nothing connects and the dispatcher uses the simulated sink instead.
"""

import logging
from typing import Any

from app.channels.base import Channel, InboundMessage
from app.channels.dispatcher import dispatcher
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 10


class TelegramAdapter:
    """The `telegram` ChannelAdapter (§6.1), wrapping one python-telegram-bot Application."""

    channel: Channel = "telegram"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._app: Any = None

    @classmethod
    def enabled(cls, settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        return bool(settings.enable_telegram and settings.telegram_bot_token.strip())

    async def start(self) -> None:
        """Build the Application and begin polling, without taking over the loop (§6.2)."""
        from telegram.ext import ApplicationBuilder, MessageHandler, filters

        self._app = ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(timeout=POLL_TIMEOUT_SECONDS, drop_pending_updates=True)
        me = self._app.bot.username
        log.info("telegram bot @%s polling", me)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
        finally:
            await self._app.shutdown()
            self._app = None
            log.info("telegram bot stopped")

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        if self._app is None:
            raise RuntimeError("telegram adapter is not running")
        sent = await self._app.bot.send_message(chat_id=int(thread_id), text=text)
        return str(sent.message_id)

    async def typing(self, thread_id: str) -> None:
        if self._app is None:
            return
        from telegram.constants import ChatAction

        try:
            await self._app.bot.send_chat_action(chat_id=int(thread_id), action=ChatAction.TYPING)
        except Exception as e:  # a typing indicator is never worth failing a message over
            log.info("telegram typing indicator failed for %s: %s", thread_id, type(e).__name__)

    # ---------- inbound ----------

    async def _on_message(self, update: Any, context: Any) -> None:
        """One Telegram message -> the §7.1 intake pipeline."""
        from app.brain.intake import handle_inbound

        message = update.message
        if message is None or not (message.text or "").strip():
            return
        chat_id = str(message.chat_id)
        user = message.from_user
        inbound = InboundMessage(
            channel="telegram",
            external_user_id=str(user.id) if user else chat_id,
            external_thread_id=chat_id,
            display_name=(user.full_name if user else None),
            text=message.text.strip(),
            external_message_id=str(message.message_id),
            raw_meta={"chat_type": message.chat.type if message.chat else None},
        )
        await self.typing(chat_id)
        try:
            await handle_inbound(inbound)
        except Exception:
            log.exception("telegram intake failed for chat %s", chat_id)
            return
        # The reply is on the outbox; deliver it now rather than on the next dispatcher tick.
        await dispatcher.deliver_pending(conversation_id=result.conversation_id)