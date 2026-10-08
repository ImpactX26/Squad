"""app/brain/llm.py against a fake provider (tests/llm_fakes.py): no real model is ever called."""

import asyncio

import httpx2
import pytest
from pydantic import BaseModel

from app.brain.llm import LLMUnavailable
from tests.llm_fakes import FakeProviders, chat, error, fake_llm

USER = [{"role": "user", "content": "My laptop won't charge"}]


class Triage(BaseModel):
    intent: str
    serial_number: str | None


# ---------- models, tiers and reasoning controls ----------

async def test_each_tier_uses_its_model_token_limit_and_reasoning_setting():
    fakes = FakeProviders().queue("groq", chat("fast"), chat("smart"))
    llm = fake_llm(fakes)
    fast = await llm.complete(USER)
    smart = await llm.complete(USER, tier="smart")
    first, second = fakes.requests["groq"]
    assert (first["model"], first["max_tokens"], first["reasoning_effort"]) == ("qwen/qwen3.8-27b", 300, "none")
    assert (second["model"], second["max_tokens"], second["reasoning_effort"]) == ("openai/gpt-oss-20b", 800, "low")
    assert (fast.text, fast.model, fast.input_tokens, fast.output_tokens) == ("fast", "groq:qwen/qwen3.8-27b", 12, 3)
    assert smart.model == "groq:openai/gpt-oss-20b"


async def test_an_empty_reasoning_setting_is_not_sent():
    fakes = FakeProviders().queue("groq", chat())
    await fake_llm(fakes, groq_qwen_reasoning_effort="").complete(USER)
    assert "reasoning_effort" not in fakes.requests["groq"][0]


async def test_think_tags_never_reach_the_caller():
    fakes = FakeProviders().queue("groq", chat("<think>hmm</think>\nHello"))
    assert (await fake_llm(fakes).complete(USER)).text == "Hello"


# ---------- the fallback order (§4.6) ----------

async def test_an_empty_groq_key_skips_groq():
    fakes = FakeProviders().queue("ollama", chat("from ollama"))
    result = await fake_llm(fakes, groq_api_key="").complete(USER)
    assert result.model == "ollama:qwen2.5:7b" and result.text == "from ollama"
    assert fakes.requests["groq"] == [] and "reasoning_effort" not in fakes.requests["ollama"][0]


async def test_a_429_with_a_short_retry_after_is_retried_on_groq_once():
    sleeps: list[float] = []
    fakes = FakeProviders().queue("groq", error(429, headers={"retry-after": "1.5"}), chat("second try"))
    result = await fake_llm(fakes, sleeps).complete(USER)
    assert result.text == "second try" and sleeps == [1.5]
    assert fakes.requests["ollama"] == []


async def test_a_second_short_429_falls_back():
    fakes = FakeProviders().queue("groq", error(429, headers={"retry-after": "1"}), error(429, headers={"retry-after": "1"}))
    fakes.queue("ollama", chat("ollama"))
    assert (await fake_llm(fakes, []).complete(USER)).model == "ollama:qwen2.5:7b"


@pytest.mark.parametrize(
    "failure",
    [
        error(429, headers={"retry-after": "30"}),
        error(429),
        error(503),
        httpx2.ReadTimeout("slow"),
        httpx2.ConnectError("refused"),
    ],
    ids=["429-long", "429-no-header", "503", "timeout", "unreachable"],
)
async def test_these_failures_fall_back_to_ollama_once(failure):
    fakes = FakeProviders().queue("groq", failure).queue("ollama", chat("ollama"))
    result = await fake_llm(fakes, []).complete(USER)
    assert result.model == "ollama:qwen2.5:7b"
    assert len(fakes.requests["groq"]) == len(fakes.requests["ollama"]) == 1


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_other_4xx_errors_do_not_fall_back(status):
    fakes = FakeProviders().queue("groq", error(status, message="nope"))
    with pytest.raises(LLMUnavailable):
        await fake_llm(fakes).complete(USER)
    assert fakes.requests["ollama"] == []


async def test_nothing_left_is_llm_unavailable():
    no_fallback = FakeProviders().queue("groq", error(503))
    with pytest.raises(LLMUnavailable):
        await fake_llm(no_fallback, llm_fallback_provider="").complete(USER)
    both_down = FakeProviders().queue("groq", httpx2.ConnectError("x")).queue("ollama", httpx2.ConnectError("x"))
    with pytest.raises(LLMUnavailable):
        await fake_llm(both_down).complete(USER)
    with pytest.raises(LLMUnavailable, match="no LLM provider"):
        await fake_llm(FakeProviders(), groq_api_key="", llm_fallback_provider="").complete(USER)


# ---------- repairs and retries ----------

async def test_tool_use_failed_is_repaired_once():
    fakes = FakeProviders().queue("groq", error(400, "tool_use_failed", "priority must be a string"), chat("fixed"))
    result = await fake_llm(fakes).complete(USER, tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])
    assert result.text == "fixed"
    retry = fakes.requests["groq"][1]["messages"]
    assert retry[-1]["role"] == "user" and "priority must be a string" in retry[-1]["content"]

    twice = FakeProviders().queue("groq", error(400, "tool_use_failed"), error(400, "tool_use_failed"))
    with pytest.raises(LLMUnavailable, match="rejected twice"):
        await fake_llm(twice).complete(USER, tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])


async def test_an_empty_gpt_oss_reply_cut_off_by_length_is_retried_with_double_tokens():
    fakes = FakeProviders().queue("groq", chat("", finish="length"), chat("answer"))
    result = await fake_llm(fakes).complete(USER, tier="smart")
    assert result.text == "answer"
    assert [r["max_tokens"] for r in fakes.requests["groq"]] == [800, 1600]


# ---------- complete_json ----------

async def test_complete_json_validates_the_reply():
    fakes = FakeProviders().queue("groq", chat('{"intent": "new_issue", "serial_number": "VX15-Q8M2D5"}'))
    value, result = await fake_llm(fakes).complete_json(USER, Triage)
    assert value == Triage(intent="new_issue", serial_number="VX15-Q8M2D5")
    sent = fakes.requests["groq"][0]
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"][-1]["role"] == "system" and "JSON schema" in sent["messages"][-1]["content"]
    assert result.model == "groq:qwen/qwen3.8-27b"


async def test_complete_json_repairs_an_invalid_reply_once():
    fakes = FakeProviders().queue("groq", chat('{"intent": "new_issue"'), chat('{"intent": "new_issue", "serial_number": null}'))
    value, _ = await fake_llm(fakes).complete_json(USER, Triage)
    assert value.intent == "new_issue"
    retry = fakes.requests["groq"][1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"intent": "new_issue"'}
    assert retry[-1]["role"] == "user" and "not valid" in retry[-1]["content"]


async def test_complete_json_gives_up_after_one_repair():
    fakes = FakeProviders().queue("groq", chat("not json"), chat('{"intent": 5}'))
    with pytest.raises(LLMUnavailable, match="no valid JSON"):
        await fake_llm(fakes).complete_json(USER, Triage)


async def test_groq_json_validate_failed_is_repaired_too():
    fakes = FakeProviders().queue("groq", error(400, "json_validate_failed", "bad json"),
                                  chat('{"intent": "smalltalk", "serial_number": null}'))
    value, _ = await fake_llm(fakes).complete_json(USER, Triage)
    assert value.intent == "smalltalk"


# ---------- concurrency ----------

async def test_at_most_four_provider_calls_run_at_once():
    fakes = FakeProviders(delay=0.05).queue("groq", *[chat() for _ in range(10)])
    llm = fake_llm(fakes)
    await asyncio.gather(*(llm.complete(USER) for _ in range(10)))
    assert fakes.max_in_flight == 4
