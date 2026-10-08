"""MCP client hub: connect, list_tools, call_tool (ARCHITECTURE.md §4.3 step 1, §12).

The backend is the MCP client (§2). At startup the hub connects to every MCP_*_URL, calls
list_tools, and registers each tool as `<server>__<tool>` in OpenAI function-tool format, which
is what router.filter_tools and runtime.run_tool_loop consume. `call_tool` matches runtime's
ToolCaller signature, so the hub plugs straight into the tool loop.

A server that is down never stops the API from starting: its failure is logged, it contributes
no tools, and the hub retries discovery on the next call.

Each call opens its own short-lived session. The servers run `stateless_http=True` (mcp_servers
/__init__.py), so there is no session to keep warm, and a per-call session keeps every anyio
cancel scope inside the task that created it — a session opened in the lifespan task and closed
from a request task is the classic way this breaks. It also makes "reconnect on the next call"
fall out for free.
"""

import asyncio
import json
import logging
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.brain.router import SERVERS
from app.core.config import get_settings

log = logging.getLogger(__name__)

# <server>__<tool> (§4.3). Double underscore, so a tool name with one underscore stays readable.
NAME_SEPARATOR = "__"

CALL_TIMEOUT_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 10.0
# A server that was down isn't retried on every single call.
REDISCOVER_AFTER_SECONDS = 30.0

# Read-only lookups whose answer is stable for a minute. Everything else always hits the server.
CACHEABLE_TOOLS = frozenset({
    "catalog__lookup_serial",
    "catalog__lookup_model",
    "catalog__get_service_price",
    "knowledge__get_playbook",
})
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 512


class McpToolError(RuntimeError):
    """A tool ran and reported an error, or the server could not be reached."""


def split_name(name: str) -> tuple[str, str]:
    """`tickets__create_ticket` -> ("tickets", "create_ticket")."""
    server, separator, tool = name.partition(NAME_SEPARATOR)
    if not separator or not server or not tool:
        raise McpToolError(f"tool name {name[:100]!r} is not <server>{NAME_SEPARATOR}<tool>")
    return server, tool


class McpHub:
    """One hub for the whole app. `hub` below is the instance main.py's lifespan connects."""

    def __init__(self) -> None:
        self._urls: dict[str, str] = {}
        self._tools: dict[str, list[dict[str, Any]]] = {}  # server -> OpenAI function tools
        self._failed_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---------- discovery ----------

    def _configured(self) -> dict[str, str]:
        if not self._urls:
            settings = get_settings()
            self._urls = {name: getattr(settings, f"mcp_{name}_url") for name in SERVERS}
        return self._urls

    async def connect_all(self) -> dict[str, int]:
        """Discover tools on every configured server. Returns {server: tool count}; never raises."""
        names = list(self._configured())
        results = await asyncio.gather(*(self._discover(name) for name in names), return_exceptions=True)
        counts: dict[str, int] = {}
        for name, result in zip(names, results):
            counts[name] = 0 if isinstance(result, BaseException) else result
        up = [f"{n}({c})" for n, c in counts.items() if c]
        down = [n for n, c in counts.items() if not c]
        log.info("mcp hub: %d tools from %s%s", sum(counts.values()), ", ".join(up) or "nothing",
                 f"; unreachable: {', '.join(down)}" if down else "")
        return counts

    async def _discover(self, server: str) -> int:
        """list_tools on one server and register them. Returns the count; 0 when it's unreachable."""
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            url = self._configured()[server]
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    async with streamable_http_client(url) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = (await session.list_tools()).tools
            except Exception as e:
                self._tools.pop(server, None)
                self._failed_at[server] = time.monotonic()
                log.warning("mcp hub: %s at %s is unreachable: %s", server, url, describe(e))
                return 0
            self._tools[server] = [_as_openai_tool(server, tool) for tool in listed]
            self._failed_at.pop(server, None)
            return len(self._tools[server])

    async def _ensure(self, server: str) -> None:
        """Rediscover a server that has no tools, at most every REDISCOVER_AFTER_SECONDS."""
        if self._tools.get(server):
            return
        failed_at = self._failed_at.get(server)
        if failed_at is not None and time.monotonic() - failed_at < REDISCOVER_AFTER_SECONDS:
            return
        await self._discover(server)

    # ---------- the two methods everything else uses ----------

    def tools(self, servers: list[str] | frozenset[str] | None = None) -> list[dict[str, Any]]:
        """Registered tools in OpenAI function-tool format, for router.filter_tools (§4.3)."""
        wanted = list(self._tools) if servers is None else [s for s in servers if s in self._tools]
        return [tool for server in wanted for tool in self._tools.get(server, [])]

    def tool_names(self) -> list[str]:
        return sorted(tool["function"]["name"] for tool in self.tools())

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Run `<server>__<tool>` and return its result. Matches runtime.ToolCaller.

        Returns the tool's structured JSON content (a dict), or its text when it returned none.
        runtime._compact serialises and truncates it for the model; workflows get the object.
        """
        server, tool = split_name(name)
        if server not in self._configured():
            raise McpToolError(f"unknown MCP server {server!r}")

        cache_key = _cache_key(name, args)
        if cache_key is not None and (hit := self._cache_get(cache_key)) is not None:
            return hit[1]

        await self._ensure(server)
        if not self._tools.get(server):
            raise McpToolError(f"MCP server {server!r} is unreachable")

        url = self._configured()[server]
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, args, read_timeout_seconds=CALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as e:
            raise McpToolError(f"{name} timed out after {CALL_TIMEOUT_SECONDS:.0f}s") from e
        except Exception as e:
            # The server may have gone away; make the next call rediscover it.
            self._tools.pop(server, None)
            self._failed_at[server] = time.monotonic()
            raise McpToolError(f"{name} failed: {describe(e)}") from e

        value = _result_value(result)
        if getattr(result, "is_error", False):
            raise McpToolError(f"{name} reported an error: {json.dumps(value, default=str)[:300]}")
        if cache_key is not None:
            self._cache_put(cache_key, value)
        return value

    # ---------- 60-second cache for read-only lookups ----------

    def _cache_get(self, key: str) -> tuple[float, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry[0] < time.monotonic():
            self._cache.pop(key, None)
            return None
        return entry

    def _cache_put(self, key: str, value: Any) -> None:
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            for stale in [k for k, (expires, _) in self._cache.items() if expires < time.monotonic()]:
                self._cache.pop(stale, None)
            if len(self._cache) >= CACHE_MAX_ENTRIES:
                self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)

    def clear_cache(self) -> None:
        self._cache.clear()


def describe(e: BaseException) -> str:
    """"ConnectError: All connection attempts failed" rather than anyio's bare ExceptionGroup wrapper."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(describe(sub) for sub in e.exceptions[:3])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _cache_key(name: str, args: dict[str, Any]) -> str | None:
    if name not in CACHEABLE_TOOLS:
        return None
    return name + ":" + json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _as_openai_tool(server: str, tool: Any) -> dict[str, Any]:
    """One MCP tool as `{"type": "function", "function": {...}}` (§4.3 step 1)."""
    schema = tool.input_schema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": f"{server}{NAME_SEPARATOR}{tool.name}",
            "description": (tool.description or "").strip(),
            "parameters": schema,
        },
    }


def _result_value(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [block.text for block in (getattr(result, "content", None) or []) if getattr(block, "text", None)]
    if not texts:
        return {}
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


hub = McpHub()