"""A fake OpenAI-compatible provider for tests: a MockTransport, never a real model (CLAUDE.md).

The openai SDK (§18.3) runs on httpx2, httpx's successor, so the transport is httpx2.MockTransport.
Each provider gets a queue of scripted replies; every request body is recorded.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2

from app.brain.llm import LLM
from app.core.config import Settings

GROQ_URL = "https://groq.test/openai/v1"
OLLAMA_URL = "http://ollama.test/v1"

Reply = httpx2.Response | Exception | Callable[[], httpx2.Response]


def chat(text: str = "ok", finish: str = "stop", tool_calls: list[dict] | None = None,
         usage: tuple[int, int] = (12, 3)) -> httpx2.Response:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx2.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    })


def error(status: int, code: str | None = None, message: str = "error", headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, json={
        "error": {"message": message, "type": "invalid_request_error", "code": code}})


class FakeProviders:
    def __init__(self, delay: float = 0.0) -> None:
        self.replies: dict[str, list[Reply]] = {"groq": [], "ollama": []}
        self.requests: dict[str, list[dict[str, Any]]] = {"groq": [], "ollama": []}
        self.delay = delay
        self.in_flight = self.max_in_flight = 0

    def queue(self, provider: str, *replies: Reply) -> "FakeProviders":
        self.replies[provider].extend(replies)
        return self

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        provider = "groq" if request.url.host == "groq.test" else "ollama"
        self.requests[provider].append(json.loads(request.content))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if not self.replies[provider]:
                raise AssertionError(f"unexpected call to {provider}")
            reply = self.replies[provider].pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply() if callable(reply) else reply
        finally:
            self.in_flight -= 1


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "groq_api_key": "test-key", "groq_base_url": GROQ_URL, "ollama_base_url": OLLAMA_URL,
        "llm_provider": "groq", "llm_fallback_provider": "ollama",
        "model_fast": "qwen/qwen3.8-27b", "model_smart": "openai/gpt-oss-20b", "ollama_model": "qwen2.5:7b",
        "groq_reasoning_effort": "low", "groq_qwen_reasoning_effort": "none",
        "llm_max_tokens_fast": 300, "llm_max_tokens_smart": 800,
    }
    return Settings(_env_file=None, **{**values, **overrides})


def fake_llm(fakes: FakeProviders, sleeps: list[float] | None = None, **overrides: Any) -> LLM:
    async def record_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(fakes.handle))
    return LLM(settings(**overrides), http_client=client, sleep=record_sleep)
