"""The backend's MCP client (ARCHITECTURE.md §2, §4.3): every tool call to the servers goes through here.

Tools are named "<server>__<tool>" (tickets__create_ticket). Each call names the allowlist it is
checked against, and a tool outside it is refused before anything is sent. Each call opens its
own short session, so a server that was down is simply tried again on the next call (§17.2).

Block 1 connects the four servers on 127.0.0.1:8101-8104 (§4.5); payments, dispatch and
inventory join in Blocks 3 and 4.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from mcp import Client
from mcp.server import MCPServer

from app.brain.router import MODEL_FORBIDDEN_TOOLS
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

CALL_TIMEOUT_SECONDS = 20

Target = str | MCPServer  # a Streamable HTTP URL, or a server object in-process (tests)


class ToolNotAllowed(Exception):
    """The caller's allowlist doesn't include this tool. Nothing was sent."""


class MCPUnavailable(Exception):
    """The server couldn't be reached or didn't answer in time."""


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: Any  # the tool's JSON result, or its {error, message, ...} refusal
    ms: int

    @property
    def error(self) -> str | None:
        return self.data.get("error") if not self.ok and isinstance(self.data, dict) else None


@dataclass
class ToolSpec:
    name: str  # "<server>__<tool>"
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class MCPHub:
    def __init__(self, servers: dict[str, Target]) -> None:
        self.servers = servers
        self.tools: dict[str, ToolSpec] = {}

    async def refresh(self) -> dict[str, bool]:
        """list_tools on every server; registers what answered. Returns which servers are up."""
        results = await asyncio.gather(*(self._list(name) for name in self.servers))
        up = {}
        for name, specs in zip(self.servers, results):
            up[name] = specs is not None
            if specs is not None:
                self.tools = {k: v for k, v in self.tools.items() if not k.startswith(f"{name}__")} | specs
        return up

    async def _list(self, server: str) -> dict[str, ToolSpec] | None:
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS), Client(self.servers[server]) as client:
                listed = await client.list_tools()
        except Exception as exc:  # unreachable: an ExceptionGroup around a connect error
            log.warning("MCP server %s is down: %s", server, _reason(exc))
            return None
        return {f"{server}__{t.name}": ToolSpec(f"{server}__{t.name}", (t.description or "").strip(), t.input_schema)
                for t in listed.tools}

    def openai_tools(self, allowed: frozenset[str]) -> list[dict[str, Any]]:
        """The allowed tools in OpenAI function format for a model. MODEL_FORBIDDEN_TOOLS are never offered."""
        return [spec.openai() for name, spec in sorted(self.tools.items())
                if name in allowed and name not in MODEL_FORBIDDEN_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any], *, allowed: frozenset[str]) -> ToolResult:
        if name not in allowed:
            raise ToolNotAllowed(name)
        server, _, tool = name.partition("__")
        if server not in self.servers or not tool:
            raise ToolNotAllowed(f"{name} (no such server)")
        started = time.perf_counter()
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS), Client(self.servers[server]) as client:
                result = await client.call_tool(tool, arguments)
        except Exception as exc:
            raise MCPUnavailable(f"{server} unreachable while calling {tool}: {_reason(exc)}") from exc
        text = "".join(getattr(part, "text", "") for part in result.content)
        return ToolResult(name=name, ok=not result.is_error, data=_parse(text),
                          ms=round((time.perf_counter() - started) * 1000))


def _parse(text: str) -> Any:
    # A refusal reads "Error executing tool x: {...}": keep the JSON part (mcp_servers/common/results.py).
    for candidate in (text, text[text.find("{"):] if "{" in text else ""):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return text


def _reason(exc: BaseException) -> str:
    inner = getattr(exc, "exceptions", None)
    return _reason(inner[0]) if inner else type(exc).__name__


def server_urls(settings: Settings) -> dict[str, Target]:
    return {
        "tickets": settings.mcp_tickets_url,
        "catalog": settings.mcp_catalog_url,
        "knowledge": settings.mcp_knowledge_url,
        "messaging": settings.mcp_messaging_url,
    }


@lru_cache
def get_hub() -> MCPHub:
    return MCPHub(server_urls(get_settings()))
