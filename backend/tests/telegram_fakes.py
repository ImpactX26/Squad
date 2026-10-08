"""A fake Telegram Bot API behind httpx.MockTransport: tests never call api.telegram.org."""

import asyncio
import json
from urllib.parse import parse_qsl

import httpx

TOKEN = "123456:TEST-not-a-real-token"
BOT = {"id": 123456, "is_bot": True, "first_name": "Aurora Support", "username": "aurora_test_bot"}


def update(update_id: int, text: str = "hello", chat_id: int = 555, user_id: int = 555, chat_type: str = "private",
           message_id: int | None = None, **message_extra) -> dict:
    user = {"id": user_id, "is_bot": False, "first_name": "Aman", "last_name": "Verma", "username": "amanv",
            "language_code": "en"}
    message = {"message_id": message_id or update_id * 10, "date": 1_760_000_000, "from": user,
               "chat": {"id": chat_id, "type": chat_type, "first_name": "Aman"}, **message_extra}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


class FakeTelegram:
    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.status: dict[str, int] = {}  # method -> HTTP status to answer with
        self._message_id = 1000

    def queue(self, *updates: dict) -> None:
        self.pending.extend(updates)

    def sent(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = dict(parse_qsl(request.content.decode())) if request.content else {}
        self.calls.append((method, params))
        if method in self.status:
            code = self.status[method]
            return httpx.Response(code, json={"ok": False, "error_code": code, "description": f"fake {code}"})
        if method == "getMe":
            return self._ok(BOT)
        if method in ("deleteWebhook", "sendChatAction"):
            return self._ok(True)
        if method == "getUpdates":
            if not self.pending:
                await asyncio.sleep(0.02)
                return self._ok([])
            ready, self.pending = self.pending, []
            return self._ok(ready)
        if method == "sendMessage":
            self._message_id += 1
            chat_id = int(params["chat_id"])
            return self._ok({"message_id": self._message_id, "date": 1_760_000_000, "text": params.get("text"),
                             "chat": {"id": chat_id, "type": "private"}, "from": BOT})
        return httpx.Response(404, json={"ok": False, "error_code": 404, "description": f"no fake for {method}"})

    @staticmethod
    def _ok(result) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"ok": True, "result": result}),
                              headers={"content-type": "application/json"})
