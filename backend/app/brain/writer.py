"""The Writer: polish an agent's rough note into a reply (ARCHITECTURE.md §7.3, §4.6).

MODEL_FAST, no tools at all (`ROLE_SERVERS["writer"]` is empty). The rules are the §7.3 ones:
keep every fact, number, date and commitment exactly; add no new promises or facts; polite,
clear and short; match the customer's language.

A model outage is not a failure here. The agent's own words are already a sendable reply, so
`polish()` hands them back with `polished=False` and the reason, and the composer sends them
unchanged. Nothing polished is ever sent without the agent seeing it (§7.3 step 3).
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass

from app.brain.llm import LLM, LLMUnavailable, default_llm
from app.brain.prompts import load as load_prompt
from app.brain.runtime import AiRunRecord, RunLogger, log_ai_run, log_run_in_background

log = logging.getLogger(__name__)

ROLE = "writer"
TRIGGER = "polish"

# The polished reply is a message, not an essay; this also caps what one call can cost (§4.5).
MAX_TOKENS = 400
MAX_INPUT_CHARS = 4000


@dataclass(frozen=True)
class Polished:
    """What the composer shows next to the agent's original (§7.3 step 3)."""

    text: str
    polished: bool             # False when the model could not be reached, or added nothing
    original: str
    model: str | None = None
    latency_ms: int = 0
    reason: str | None = None   # why it was not polished
    warning: str | None = None  # polished, but check this before sending


async def polish(
    text: str,
    *,
    ticket_id: uuid.UUID | str | None = None,
    context: str | None = None,
    llm: LLM | None = None,
    log_run: RunLogger | None = None,
) -> Polished:
    """Rewrite `text` as a customer-ready reply. Never raises for a model outage.

    `context` is a short line about the ticket (device, fault) so the rewrite stays on topic. It
    is reference material, not permission to add anything: the prompt forbids new facts, and the
    check below rejects a rewrite that invented a number the agent never wrote.
    """
    original = text.strip()
    if not original:
        return Polished(text=text, polished=False, original=text, reason="nothing to polish")

    llm = llm or default_llm()
    start = time.monotonic()
    messages = [{"role": "system", "content": load_prompt("writer_polish")}]
    if context:
        messages.append({"role": "system", "content": f"About this ticket: {context[:500]}"})
    messages.append({"role": "user", "content": original[:MAX_INPUT_CHARS]})

    error: str | None = None
    try:
        result = await llm.complete(messages, tier="fast", max_tokens=MAX_TOKENS)
    except LLMUnavailable as e:
        # §7.3 and §15: the agent's note is still a reply. Hand it back untouched.
        error = f"{type(e).__name__}: {e}"[:500]
        log.warning("polish unavailable, returning the agent's own text: %s", e)
        _log(log_run, ticket_id, None, None, None, start, error)
        return Polished(text=original, polished=False, original=original,
                        latency_ms=_ms(start), reason="No model provider is reachable right now.")

    candidate = _strip_preamble(result.text)
    _log(log_run, ticket_id, f"{result.provider}:{result.model}",
         result.input_tokens, result.output_tokens, start, None)

    if not candidate:
        return Polished(text=original, polished=False, original=original,
                        model=f"{result.provider}:{result.model}", latency_ms=_ms(start),
                        reason="The model returned nothing.")

    # §7.3 forbids new facts. The agent reviews the rewrite side by side before anything is
    # sent (§7.3 step 3), so a suspect number is flagged for them rather than thrown away: the
    # human is the guard, and discarding the rewrite would just lose good work to a false alarm.
    invented = invented_numbers(original, candidate)
    warning = None
    if invented:
        log.warning("polish introduced %s, which the note did not contain", invented)
        warning = (f"This rewrite mentions {', '.join(invented)}, which your note did not. "
                   f"Check it before sending.")

    return Polished(text=candidate, polished=True, original=original,
                    model=f"{result.provider}:{result.model}", latency_ms=_ms(start),
                    warning=warning)


# ---------- guards ----------

# Any run of digits, keeping decimals and thousands separators together.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def invented_numbers(original: str, candidate: str) -> list[str]:
    """Numbers in the rewrite that the agent's note never had (§7.3 "add no new facts").

    Prices, dates and quantities are the facts that matter and the ones a reader acts on. Digits
    are compared with separators stripped, so "3200" and "3,200" are the same number and a number
    that merely repeats is fine.

    Zeros are ignored, because they come from reformatting rather than from invention: a rewrite
    turning "10am-1pm" into "10:00 AM to 1:00 PM" has added no fact, only punctuation.
    """
    had = {_digits(match) for match in _NUMBER.findall(original)}
    invented = []
    for match in _NUMBER.findall(candidate):
        digits = _digits(match)
        if digits == "0" or digits in had or digits in invented:
            continue
        invented.append(digits)
    return invented


def _digits(number: str) -> str:
    """A number as comparable digits: separators dropped, leading zeros dropped, "00" -> "0"."""
    return number.replace(",", "").replace(".", "").lstrip("0") or "0"


def _strip_preamble(text: str) -> str:
    """Drop a "Here's the polished version:" lead-in, and any quotes wrapped round the whole reply."""
    cleaned = text.strip()
    cleaned = re.sub(r"(?is)\A(?:here(?:'s| is)[^\n:]*:|polished(?: version)?:|rewritten:)\s*", "", cleaned)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    return cleaned.strip()


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def _log(
    log_run: RunLogger | None,
    ticket_id: uuid.UUID | str | None,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    start: float,
    error: str | None,
) -> None:
    log_run_in_background(log_run or log_ai_run, AiRunRecord(
        role=ROLE, trigger=TRIGGER, ticket_id=ticket_id, model=model,
        input_tokens=tokens_in, output_tokens=tokens_out, tool_calls=[],
        latency_ms=_ms(start), error=error,
    ))
    