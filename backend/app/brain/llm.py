"""The only module that talks to a text model (ARCHITECTURE.md §4.6).

Free tiers only. Groq (OpenAI-compatible) serves text and tool loops. When it answers 429,
times out, can't be reached, or returns a 5xx, the call goes once to the fallback provider
(Ollama on the 16GB laptop). Both are called through the openai SDK with its own retries off,
so a customer never waits on SDK backoff. When every provider fails, LLMUnavailable is raised
and customer-path callers send the friendly fallback reply (§15).

Check each provider and Groq's remaining limits (never prints keys):
    cd backend && uv run python -m app.brain.llm --check      # make llm-check
"""

import argparse
import asyncio
import json
import logging
import re
import time
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Annotated, Any, Literal, TypeVar, Union, get_args, get_origin

import httpx
import httpx2
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "smart"]
Provider = Literal["groq", "ollama"]
M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

CONNECT_TIMEOUT_SECONDS = 2.0
# A 429 whose retry-after is at most this is retried on the primary instead of falling back.
MAX_RETRY_AFTER_SECONDS = 2.0
CHECK_MAX_TOKENS = 64

# One process-wide limit across providers, so bursts don't trip Groq's ~30 requests/min.
_semaphore = asyncio.Semaphore(4)


class LLMUnavailable(Exception):
    """No provider could answer. Customer-path callers catch this and send the §15 fallback reply."""


class LLMBadOutput(LLMUnavailable):
    """complete_json got invalid JSON twice: the first answer and its one repair."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model's arguments aren't a JSON object
    raw_arguments: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    tool_calls: list[ToolCall]
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    finish_reason: str | None

    def assistant_message(self) -> dict[str, Any]:
        """This reply as an OpenAI assistant message, appended before its tool results."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.raw_arguments}}
                for tc in self.tool_calls
            ]
        return message


class LLM:
    """Provider clients and the fallback order. The module-level functions use a shared instance.

    Tests pass their own settings and an httpx client built on httpx.MockTransport.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._http_client = http_client
        self._sleep = sleep
        self._clients: dict[str, AsyncOpenAI] = {}

    def client(self, provider: Provider) -> AsyncOpenAI:
        if provider not in self._clients:
            s = self.settings
            if provider == "groq":
                base_url, api_key, read_timeout = s.groq_base_url, s.groq_api_key, s.ai_timeout_seconds
            else:
                base_url, api_key, read_timeout = s.ollama_base_url, "ollama", s.ollama_timeout_seconds
            self._clients[provider] = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,  # fallback is ours; SDK backoff would make the customer wait
                timeout=openai.Timeout(read_timeout, connect=CONNECT_TIMEOUT_SECONDS),
                http_client=self._http_client,
            )
        return self._clients[provider]

    def providers(self) -> list[Provider]:
        """Primary first, then the fallback when it's set and different."""
        s = self.settings
        order: list[Provider] = [s.llm_provider]
        if s.llm_fallback_provider and s.llm_fallback_provider != s.llm_provider:
            order.append(s.llm_fallback_provider)
        return order

    def model_for(self, provider: Provider, tier: Tier) -> str:
        if provider == "ollama":
            return self.settings.ollama_model
        return self.settings.model_fast if tier == "fast" else self.settings.model_smart

    def _max_tokens(self, tier: Tier) -> int:
        return self.settings.llm_max_tokens_fast if tier == "fast" else self.settings.llm_max_tokens_smart

    def _extras(self, provider: Provider, model: str) -> dict[str, Any]:
        """Groq's per-family reasoning controls. Groq rejects the wrong value for a family.

        gpt-oss takes low|medium|high. Qwen3 also takes none ("no reasoning tokens at all") and
        default, which gpt-oss rejects, so the two have their own settings.
        """
        if provider != "groq":
            return {}
        if model.startswith("openai/gpt-oss"):
            effort = self.settings.groq_reasoning_effort
        elif is_qwen3(model):
            effort = self.settings.groq_qwen_reasoning_effort
        else:
            return {}
        return {"extra_body": {"reasoning_effort": effort}} if effort else {}

    # ---------- public API ----------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: Tier,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        """With tools, one repair retry when Groq rejects the model's tool call (arguments off schema)."""
        try:
            return await self._complete(messages, tier=tier, max_tokens=max_tokens, tools=tools, json_mode=False)
        except LLMUnavailable as e:
            rejected = _rejected_tool_call(e) if tools else None
            if rejected is None:
                raise
            log.info("tool call rejected by the provider, repairing once: %s", rejected)
            repair = [*messages, {"role": "user", "content": (
                f"Your last tool call was rejected: {rejected}. Call the tool again with arguments that match its "
                "schema exactly (integers as numbers, true/false as booleans; leave out optional ones you don't need).")}]
            return await self._complete(repair, tier=tier, max_tokens=max_tokens, tools=tools, json_mode=False)

    async def complete_json(self, messages: list[dict[str, Any]], schema: type[M], *, tier: Tier) -> tuple[M, LLMResult]:
        """JSON mode, validated by `schema`, with one repair retry that shows the model its error.

        JSON mode rather than a forced tool call, because Ollama's OpenAI endpoint has no tool_choice.
        """
        start = time.monotonic()
        convo = _with_json_instruction(messages, schema)
        total: LLMResult | None = None
        for attempt in range(2):
            try:
                result = await self._complete(convo, tier=tier, max_tokens=None, tools=None, json_mode=True)
            except LLMUnavailable as e:
                bad = _failed_generation(e)  # Groq's 400 when the model's JSON didn't parse
                if bad is None:
                    raise
                text, error = bad, "the reply was not valid JSON"
            else:
                total = result if total is None else _add_usage(total, result)
                text = result.text
                try:
                    parsed = schema.model_validate_json(_strip_code_fence(text))
                    return parsed, replace(total, latency_ms=_elapsed_ms(start))
                except ValidationError as e:
                    error = _describe_validation_error(e)
            if attempt == 0:
                log.info("complete_json(%s) invalid, repairing once: %s", schema.__name__, error)
                convo = [
                    *convo,
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": f"That reply was invalid: {error}. Reply with only the corrected JSON object."},
                ]
        raise LLMBadOutput(f"{schema.__name__}: {error}")

    async def stream_text(
        self, messages: list[dict[str, Any]], *, tier: Tier, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        """Text deltas for SSE. Falls back only when the error comes before the first token."""
        limit = max_tokens or self._max_tokens(tier)

        async def open_stream(client: AsyncOpenAI, provider: Provider, model: str):
            stream = await client.chat.completions.create(
                model=model, messages=messages, max_tokens=limit, stream=True, **self._extras(provider, model)
            )
            chunks = stream.__aiter__()
            try:
                async for chunk in chunks:
                    if delta := _delta_text(chunk):
                        return stream, chunks, delta
            except BaseException:
                await stream.close()
                raise
            return stream, chunks, ""

        stream, chunks, first = await self._with_fallback(tier, open_stream)
        try:
            if first:
                yield first
            async for chunk in chunks:
                if delta := _delta_text(chunk):
                    yield delta
        except _UNAVAILABLE_ERRORS as e:
            raise LLMUnavailable(f"stream broke after the first token: {_describe(e)}") from e
        finally:
            await stream.close()

    # ---------- internals ----------

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: Tier,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
    ) -> LLMResult:
        start = time.monotonic()
        limit = max_tokens or self._max_tokens(tier)
        result = await self._complete_once(messages, tier, limit, tools, json_mode)
        if not result.text and not result.tool_calls and result.finish_reason == "length":
            # gpt-oss spends max_tokens on reasoning before any content; give it room once.
            log.info("%s:%s returned no content at max_tokens=%d; retrying at %d", result.provider, result.model, limit, limit * 2)
            result = _add_usage(result, await self._complete_once(messages, tier, limit * 2, tools, json_mode))
        return replace(result, latency_ms=_elapsed_ms(start))

    async def _complete_once(
        self,
        messages: list[dict[str, Any]],
        tier: Tier,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
    ) -> LLMResult:
        async def call(client: AsyncOpenAI, provider: Provider, model: str):
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
            if tools:
                kwargs["tools"] = tools
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            return provider, model, await client.chat.completions.create(**kwargs, **self._extras(provider, model))

        provider, model, response = await self._with_fallback(tier, call)
        return _to_result(response, provider, model)

    async def _with_fallback(self, tier: Tier, call: Callable[[AsyncOpenAI, Provider, str], Awaitable[T]]) -> T:
        failures: list[str] = []
        providers = self.providers()
        for i, provider in enumerate(providers):
            model = self.model_for(provider, tier)
            if provider == "groq" and not self.settings.groq_api_key:
                failures.append("groq: GROQ_API_KEY is empty")
                continue
            try:
                return await self._attempt(provider, model, call, primary=i == 0)
            except _UNAVAILABLE_ERRORS as e:
                if isinstance(e, openai.APIStatusError) and e.status_code < 500 and e.status_code != 429:
                    # A 4xx other than 429 is a bad request or key; another provider won't fix it.
                    raise LLMUnavailable(f"{provider}:{model}: {_describe(e)}") from e
                failures.append(f"{provider}:{model}: {_describe(e)}")
                next_step = "; trying the fallback" if i + 1 < len(providers) else ""
                log.warning("LLM %s:%s unavailable (%s)%s", provider, model, _describe(e), next_step)
        raise LLMUnavailable("; ".join(failures) or "no LLM provider configured")

    async def _attempt(
        self, provider: Provider, model: str, call: Callable[[AsyncOpenAI, Provider, str], Awaitable[T]], *, primary: bool
    ) -> T:
        client = self.client(provider)
        try:
            async with _semaphore:
                return await call(client, provider, model)
        except openai.RateLimitError as e:
            wait = _retry_after_seconds(e)
            if not primary or wait is None or wait > MAX_RETRY_AFTER_SECONDS:
                raise
            log.info("%s:%s rate-limited; retrying once in %.1fs", provider, model, wait)
        await self._sleep(wait)
        async with _semaphore:
            return await call(client, provider, model)


# Everything that counts as "this provider can't answer right now". Raw httpx errors can
# surface while reading a stream, outside the SDK's own error mapping.
_UNAVAILABLE_ERRORS = (openai.APIError, httpx.TransportError, httpx2.TransportError)


@lru_cache
def default_llm() -> LLM:
    return LLM()


async def complete(
    messages: list[dict[str, Any]],
    *,
    tier: Tier,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> LLMResult:
    return await default_llm().complete(messages, tier=tier, max_tokens=max_tokens, tools=tools)


async def complete_json(messages: list[dict[str, Any]], schema: type[M], *, tier: Tier) -> tuple[M, LLMResult]:
    return await default_llm().complete_json(messages, schema, tier=tier)


async def stream_text(messages: list[dict[str, Any]], *, tier: Tier, max_tokens: int | None = None) -> AsyncIterator[str]:
    async for delta in default_llm().stream_text(messages, tier=tier, max_tokens=max_tokens):
        yield delta


# ---------- helpers ----------


def is_qwen3(model: str) -> bool:
    """A Groq Qwen3 model id, e.g. qwen/qwen3.8-27b or qwen/qwen3-32b."""
    return model.startswith("qwen/qwen3")


def _to_result(response: Any, provider: str, model: str) -> LLMResult:
    choice = response.choices[0] if response.choices else None
    message = choice.message if choice else None
    raw_calls = (message.tool_calls or []) if message else []
    tool_calls = [
        ToolCall(id=tc.id, name=tc.function.name, arguments=_parse_arguments(tc.function.arguments),
                 raw_arguments=tc.function.arguments or "{}")
        for tc in raw_calls
        if getattr(tc, "function", None) is not None
    ]
    usage = response.usage
    return LLMResult(
        text=(message.content or "") if message else "",
        tool_calls=tool_calls,
        provider=provider,
        model=model,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        latency_ms=0,
        finish_reason=choice.finish_reason if choice else None,
    )


def _parse_arguments(raw: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _delta_text(chunk: Any) -> str:
    return (chunk.choices[0].delta.content or "") if chunk.choices else ""


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


def _add_usage(first: LLMResult, second: LLMResult) -> LLMResult:
    """`second`'s answer, with the tokens of both calls."""
    return replace(
        second,
        input_tokens=_add(first.input_tokens, second.input_tokens),
        output_tokens=_add(first.output_tokens, second.output_tokens),
    )


def _elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def _retry_after_seconds(e: openai.RateLimitError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _describe(e: BaseException) -> str:
    if isinstance(e, openai.APITimeoutError) or isinstance(e, (httpx.TimeoutException, httpx2.TimeoutException)):
        return "timeout"
    if isinstance(e, openai.APIConnectionError) or isinstance(e, (httpx.TransportError, httpx2.TransportError)):
        return "connection error"
    if isinstance(e, openai.APIStatusError):
        return f"HTTP {e.status_code}: {e.message[:200]}"
    return f"{type(e).__name__}: {str(e)[:200]}"


def _failed_generation(e: LLMUnavailable) -> str | None:
    cause = e.__cause__
    if isinstance(cause, openai.BadRequestError) and cause.code == "json_validate_failed":
        body = cause.body if isinstance(cause.body, dict) else {}
        return str(body.get("failed_generation") or "")
    return None


def _rejected_tool_call(e: LLMUnavailable) -> str | None:
    """Groq's 400 when a tool call's arguments don't match the tool's schema (code tool_use_failed)."""
    cause = e.__cause__
    if not isinstance(cause, openai.BadRequestError):
        return None
    if cause.code == "tool_use_failed" or "tool call validation failed" in (cause.message or "").lower():
        return (cause.message or "arguments did not match the schema")[:300]
    return None


def _strip_code_fence(text: str) -> str:
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return match.group(1) if match else text


def _describe_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        where = ".".join(str(p) for p in err["loc"]) or "reply"
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def _with_json_instruction(messages: list[dict[str, Any]], schema: type[BaseModel]) -> list[dict[str, Any]]:
    instruction = f"Reply with only a JSON object of this shape: {describe_schema(schema)}"
    convo = [dict(m) for m in messages]
    if convo and convo[0].get("role") == "system":
        convo[0]["content"] = f"{convo[0]['content']}\n\n{instruction}"
    else:
        convo.insert(0, {"role": "system", "content": instruction})
    return convo


def describe_schema(schema: type[BaseModel]) -> str:
    """A compact shape like {"intent":"new_issue|follow_up","symptoms":["string"]}: far fewer tokens than JSON Schema."""
    return json.dumps(_model_shape(schema), separators=(",", ":"))


def _model_shape(schema: type[BaseModel]) -> dict[str, Any]:
    shape = {}
    for name, field in schema.model_fields.items():
        value = _type_shape(field.annotation)
        if field.description and isinstance(value, str):
            value = f"{value} ({field.description})"
        shape[field.alias or name] = value
    return shape


def _type_shape(tp: Any) -> Any:
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        return _type_shape(args[0])
    if origin is Literal:
        return "|".join(str(a) for a in args)
    if origin in (Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        shape = _type_shape(inner[0]) if len(inner) == 1 else "|".join(str(_type_shape(a)) for a in inner)
        return f"{shape}|null" if type(None) in args and isinstance(shape, str) else shape
    if origin in (list, tuple, set, frozenset):
        return [_type_shape(args[0]) if args else "any"]
    if origin is dict or tp is dict:
        return "object"
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _model_shape(tp)
    return {str: "string", int: "integer", float: "number", bool: "true|false"}.get(tp, "any")


# ---------- python -m app.brain.llm --check ----------


async def check(llm: LLM | None = None) -> bool:
    """One tiny request per configured provider (both Groq tiers), bypassing fallback."""
    llm = llm or LLM()
    s = llm.settings
    targets: list[tuple[Provider, str]] = []
    for provider in llm.providers():
        models = [s.model_fast, s.model_smart] if provider == "groq" else [s.ollama_model]
        targets += [(provider, m) for m in dict.fromkeys(models)]

    ok = True
    for provider, model in targets:
        label = f"{provider:<7} {model:<28}"
        if provider == "groq" and not s.groq_api_key:
            print(f"{label} skipped: GROQ_API_KEY is empty in backend/.env")
            ok = False
            continue
        start = time.monotonic()
        try:
            raw = await llm.client(provider).chat.completions.with_raw_response.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=CHECK_MAX_TOKENS,
                **llm._extras(provider, model),
            )
            response = raw.parse()
        except _UNAVAILABLE_ERRORS as e:
            where = s.ollama_base_url if provider == "ollama" else s.groq_base_url
            print(f"{label} FAILED after {_elapsed_ms(start)} ms: {_describe(e)} ({where})")
            ok = False
            continue
        usage = response.usage
        tokens = f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "n/a"
        finish = response.choices[0].finish_reason if response.choices else None
        print(f"{label} {_elapsed_ms(start):>6} ms  finish={finish}  tokens in/out={tokens}")
        for name, value in sorted(raw.headers.items()):
            if name.lower().startswith("x-ratelimit-"):
                print(f"    {name.lower()}: {value}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.brain.llm", description="LLM provider tools (ARCHITECTURE.md §4.6)")
    parser.add_argument("--check", action="store_true", help="send one tiny request to each configured provider")
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return 2
    return 0 if asyncio.run(check()) else 1


if __name__ == "__main__":
    raise SystemExit(main())