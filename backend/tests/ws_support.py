"""An in-process ASGI WebSocket client.

It runs the app in the test's own event loop, so a route can share the test's database
transaction (rolled back afterwards); Starlette's TestClient runs it in another thread and loop.
"""

import asyncio
import json
from contextlib import suppress
from typing import Any


class Closed(Exception):
    def __init__(self, code: int | None, reason: str = "") -> None:
        super().__init__(f"closed with {code}: {reason}")
        self.code = code
        self.reason = reason


class WSClient:
    def __init__(self, app, path: str) -> None:
        self._app = app
        self._path = path
        self._to_app: asyncio.Queue[dict] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "WSClient":
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": self._path,
                 "raw_path": self._path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"host", b"test")], "client": ("127.0.0.1", 50000), "server": ("test", 80),
                 "subprotocols": []}
        await self._to_app.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(scope, self._to_app.get, self._from_app.put))
        first = await self._next()
        if first["type"] == "websocket.close":
            raise Closed(first.get("code"), first.get("reason", ""))
        assert first["type"] == "websocket.accept", first
        return self

    async def __aexit__(self, *exc) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        with suppress(Exception):
            await asyncio.wait_for(self._task, 5)

    async def _next(self, timeout: float = 5) -> dict:
        return await asyncio.wait_for(self._from_app.get(), timeout)

    async def send_text(self, data: str) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": data})

    async def send_json(self, data: Any) -> None:
        await self.send_text(json.dumps(data))

    async def receive_json(self, timeout: float = 5) -> dict:
        message = await self._next(timeout)
        if message["type"] == "websocket.close":
            raise Closed(message.get("code"), message.get("reason", ""))
        return json.loads(message["text"])

    async def receive_until(self, predicate, timeout: float = 5) -> tuple[dict, list[dict]]:
        """The first frame matching predicate, and every frame before it."""
        before = []
        async with asyncio.timeout(timeout):
            while True:
                frame = await self.receive_json(timeout)
                if predicate(frame):
                    return frame, before
                before.append(frame)
