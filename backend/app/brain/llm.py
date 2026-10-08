"""The only module that talks to a text model (ARCHITECTURE.md §4.6).

complete() and complete_json() try LLM_PROVIDER, then LLM_FALLBACK_PROVIDER:
1. Groq is skipped when GROQ_API_KEY is empty.
2. A Groq 429 whose retry-after is 2 s or less is waited out and retried once.
3. Any other 429, a timeout, a connection error or a 5xx moves on to the fallback (once).
   Any other 4xx (bad request, bad key, unknown model) stops there: no provider will do better.
4. Nothing left: LLMUnavailable. Customer-path callers send the §15 fallback reply.

Groq's tool_use_failed (a tool argument that doesn't match its schema) and a JSON reply that
doesn't validate are repaired once by asking again with the rejection; an empty gpt-oss reply cut
off by max_tokens (its reasoning used them up) is retried once with double the limit (§18.2).

`make llm-check` runs this module: one tiny request per provider and model, with Groq's limits.
"""

import asyncio
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, TypeVar

import httpx2
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "smart"]
T = TypeVar("T", bound=BaseModel)

MAX_CONCURRENT_CALLS = 4  # bursts stay inside Groq's per-minute limits (§4.5)
QUICK_RETRY_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 2.0
REPAIRABLE_CODES = {"tool_use_failed", "json_validate_failed"}
THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class LLMUnavailable(Exception):
    """No provider produced an answer."""


@dataclass
class LLMResult:
    text: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    model: str  # "<provider>:<model>", what ai_runs.model records
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class _Fallback(Exception):
    """This provider failed in a way the next one may not."""


class _Rejected(Exception):
    """The provider refused the request itself (a 4xx): don't fall back."""


class _Repairable(Exception):
    """A tool call or JSON reply the provider rejected: ask once more with the reason."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Provider:
    name: str
    client: AsyncOpenAI
    fast_model: str
    smart_model: str

    def model(self, tier: Tier) -> str:
        return self.fast_model if tier == "fast" else self.smart_model


class LLM:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self.providers = self._build_providers(http_client)

    def _build_providers(self, http_client: httpx2.AsyncClient | None) -> list[_Provider]:
        s = self.settings
        providers: list[_Provider] = []
        for name in dict.fromkeys(n.strip().lower() for n in (s.llm_provider, s.llm_fallback_provider) if n.strip()):
            if name == "groq":
                if not s.groq_api_key:
                    continue
                client = AsyncOpenAI(
                    api_key=s.groq_api_key, base_url=s.groq_base_url, max_retries=0, http_client=http_client,
                    timeout=httpx2.Timeout(s.ai_timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS),
                )
                providers.append(_Provider("groq", client, s.model_fast, s.model_smart))
            elif name == "ollama":
                client = AsyncOpenAI(
                    api_key="ollama", base_url=s.ollama_base_url, max_retries=0, http_client=http_client,
                    timeout=httpx2.Timeout(s.ollama_timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS),
                )
                providers.append(_Provider("ollama", client, s.ollama_model, s.ollama_model))
            else:
                log.warning("unknown LLM provider %r ignored (groq | ollama)", name)
        return providers

    # ---------- public ----------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tier: Tier = "fast",
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        try:
            return await self._complete(messages, tier, max_tokens, tools, json_mode=False)
        except _Repairable as first:
            repaired = [*messages, {"role": "user", "content": (
                f"Your last tool call was rejected: {first.message} "
                "Call the tool again with arguments that match its schema exactly.")}]
            try:
                return await self._complete(repaired, tier, max_tokens, tools, json_mode=False)
            except _Repairable as second:
                raise LLMUnavailable(f"tool call rejected twice: {second.message}") from second

    async def complete_json(self, messages: list[dict[str, Any]], schema: type[T], tier: Tier = "fast") -> tuple[T, LLMResult]:
        """One reply in JSON mode, validated as `schema`; one repair round, then LLMUnavailable."""
        shape = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        conversation = [*messages, {"role": "system", "content": (
            f"Reply with one JSON object and nothing else. It must match this JSON schema: {shape}")}]
        for attempt in (1, 2):
            text = None
            try:
                result = await self._complete(conversation, tier, None, None, json_mode=True)
            except _Repairable as exc:
                problem = exc.message
            else:
                text = result.text
                try:
                    return schema.model_validate_json(text), result
                except ValidationError as exc:
                    problem = "; ".join(
                        f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in exc.errors(include_input=False)
                    )[:500]
            if attempt == 2:
                raise LLMUnavailable(f"no valid JSON after one repair: {problem}")
            if text:
                conversation = [*conversation, {"role": "assistant", "content": text}]
            conversation = [*conversation, {"role": "user", "content": (
                f"That reply was not valid: {problem}. Reply with the corrected JSON object only.")}]
        raise AssertionError("unreachable")

    # ---------- the fallback order ----------

    async def _complete(
        self, messages: list[dict[str, Any]], tier: Tier, max_tokens: int | None,
        tools: list[dict[str, Any]] | None, *, json_mode: bool,
    ) -> LLMResult:
        if not self.providers:
            raise LLMUnavailable("no LLM provider is configured (GROQ_API_KEY is empty and there is no fallback)")
        limit = max_tokens or (self.settings.llm_max_tokens_fast if tier == "fast" else self.settings.llm_max_tokens_smart)
        failures = []
        for provider in self.providers:
            request = self.request(provider, tier, messages, limit, tools, json_mode)
            try:
                return await self._call(provider, request)
            except _Fallback as exc:
                failures.append(str(exc))
                log.warning("LLM %s failed (%s); trying the next provider", provider.name, exc)
            except _Rejected as exc:
                raise LLMUnavailable(str(exc)) from exc
        raise LLMUnavailable("every provider failed: " + "; ".join(failures))

    def request(
        self, provider: _Provider, tier: Tier, messages: list[dict[str, Any]], max_tokens: int,
        tools: list[dict[str, Any]] | None = None, json_mode: bool = False,
    ) -> dict[str, Any]:
        model = provider.model(tier)
        request: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            request["tools"] = tools
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        effort = self._reasoning_effort(provider, model)
        if effort:
            # Groq rejects a value the model family doesn't know, so each family has its own setting.
            request["extra_body"] = {"reasoning_effort": effort}
        return request

    def _reasoning_effort(self, provider: _Provider, model: str) -> str | None:
        if provider.name != "groq":
            return None
        if model.startswith("openai/gpt-oss"):
            return self.settings.groq_reasoning_effort or None
        if model.startswith("qwen/qwen3"):
            return self.settings.groq_qwen_reasoning_effort or None
        return None

    async def _call(self, provider: _Provider, request: dict[str, Any]) -> LLMResult:
        quick_retry_used = doubled = False
        while True:
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    response = await provider.client.chat.completions.create(**request)
            except openai.RateLimitError as exc:
                wait = _retry_after(exc.response)
                if provider.name == "groq" and not quick_retry_used and wait is not None and wait <= QUICK_RETRY_SECONDS:
                    quick_retry_used = True
                    await self._sleep(wait)
                    continue
                raise _Fallback(f"{provider.name} 429") from exc
            except openai.APITimeoutError as exc:
                raise _Fallback(f"{provider.name} timed out") from exc
            except openai.APIConnectionError as exc:
                raise _Fallback(f"{provider.name} unreachable") from exc
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    raise _Fallback(f"{provider.name} {exc.status_code}") from exc
                code, message = _error_detail(exc)
                if exc.status_code == 400 and code in REPAIRABLE_CODES:
                    raise _Repairable(message) from exc
                raise _Rejected(f"{provider.name} {exc.status_code} {code or ''}".strip()) from exc

            result = _result(provider, request["model"], response, started)
            if (not doubled and request["model"].startswith("openai/gpt-oss") and result.finish_reason == "length"
                    and not result.text and not result.tool_calls):
                doubled = True
                request = {**request, "max_tokens": request["max_tokens"] * 2}
                continue
            return result


def _retry_after(response: httpx2.Response | None) -> float | None:
    try:
        return float(response.headers["retry-after"]) if response is not None else None
    except (KeyError, ValueError):
        return None


def _error_detail(exc: openai.APIStatusError) -> tuple[str | None, str]:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
    return error.get("code"), str(error.get("message") or exc.message)[:500]


def _result(provider: _Provider, model: str, response: Any, started: float) -> LLMResult:
    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    return LLMResult(
        text=THINK_BLOCK.sub("", choice.message.content or "").strip(),
        tool_calls=[call.model_dump() for call in (choice.message.tool_calls or [])],
        finish_reason=choice.finish_reason,
        model=f"{provider.name}:{model}",
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


@lru_cache
def get_llm() -> LLM:
    return LLM(get_settings())


async def complete(
    messages: list[dict[str, Any]], tier: Tier = "fast", max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> LLMResult:
    return await get_llm().complete(messages, tier, max_tokens, tools)


async def complete_json(messages: list[dict[str, Any]], schema: type[T], tier: Tier = "fast") -> tuple[T, LLMResult]:
    return await get_llm().complete_json(messages, schema, tier)


# ---------- make llm-check ----------

RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
                "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens")


async def check(settings: Settings) -> bool:
    """One tiny request per configured provider and model; Groq's model list and remaining limits."""
    llm = LLM(settings)
    print(f"Providers, in order: {', '.join(p.name for p in llm.providers) or 'none'}"
          + ("" if settings.groq_api_key else "  (GROQ_API_KEY is empty, so Groq is skipped)"))
    ok = bool(llm.providers)
    for provider in llm.providers:
        if provider.name == "groq":
            try:
                available = {m.id async for m in provider.client.models.list()}
                for model in dict.fromkeys((provider.fast_model, provider.smart_model)):
                    print(f"  groq model {model}: {'listed' if model in available else 'NOT in Groq model list'}")
                    ok &= model in available
            except openai.OpenAIError as exc:
                print(f"  groq model list: failed ({type(exc).__name__})")
                ok = False
        tiers: list[Tier] = ["fast", "smart"] if provider.fast_model != provider.smart_model else ["fast"]
        for tier in tiers:
            limit = settings.llm_max_tokens_fast if tier == "fast" else settings.llm_max_tokens_smart
            request = llm.request(provider, tier, [{"role": "user", "content": "Reply with the single word: ok"}], limit)
            started = time.perf_counter()
            try:
                raw = await provider.client.chat.completions.with_raw_response.create(**request)
            except openai.OpenAIError as exc:
                status = getattr(exc, "status_code", None)
                print(f"  {provider.name}:{request['model']} ({tier}): FAILED {type(exc).__name__}{f' {status}' if status else ''}")
                ok = False
                continue
            result = _result(provider, request["model"], raw.parse(), started)
            print(f"  {result.model} ({tier}): {result.latency_ms} ms, reply {result.text[:40]!r}, "
                  f"finish {result.finish_reason}, tokens in/out {result.input_tokens}/{result.output_tokens}")
            limits = {h.removeprefix("x-ratelimit-"): raw.headers[h] for h in RATE_HEADERS if h in raw.headers}
            if limits:
                print("    limits: " + ", ".join(f"{k} {v}" for k, v in limits.items()))
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(check(get_settings())) else 1)
