"""Typed decisions: classify, yes/no, pick one (ARCHITECTURE.md §4.6).

DECISION_PROVIDER=llm (the default): one complete_json call on MODEL_FAST answers every question.
DECISION_PROVIDER=jev: one POST to Jev (TypeSafe's decision model) through the free gateway. On any
error, timeout, 429, or unparseable body, the same call takes the llm path. Jev is a third party,
so the state is redact()ed before it leaves: emails, phone numbers, and street addresses are
masked; serials and the issue text stay.

Decisions never authorize payment, dispatch, refunds, or stock changes. Those happen only from staff
actions and from payment.paid, whose only automatic source is a verified bank-alert match (§7.6,
app/payments/upi_verifier.py). A decision may classify, route, or flag; code decides what happens next.
"""

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.brain.llm import LLM, default_llm
from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 512

# ---------- §4.4 enums ----------

Intent = Literal["new_issue", "follow_up", "provide_info", "payment_details", "smalltalk", "other"]
Category = Literal["hardware", "software", "unknown"]
IssueType = Literal[
    "battery", "charging", "display", "keyboard", "audio", "overheating",
    "boot", "os", "driver", "performance", "connectivity", "other",
]
Urgency = Literal["low", "medium", "high"]

INTENT_OPTIONS: dict[str, str] = {
    "new_issue": "reports a new problem with a device",
    "follow_up": "asks about or chases a problem already reported",
    "provide_info": "gives details we asked for, such as a serial number, photo, or error code",
    "payment_details": "gives name, email, phone, or address for a booking or payment",
    "smalltalk": "greeting, thanks, or chit-chat with no request",
    "other": "anything else",
}
CATEGORY_OPTIONS: dict[str, str] = {
    "hardware": "a physical part is faulty, worn, or damaged",
    "software": "the OS, drivers, apps, or settings",
    "unknown": "not clear from the message",
}
ISSUE_TYPE_OPTIONS: dict[str, str] = {
    "battery": "battery drains fast, swells, or won't hold charge",
    "charging": "won't charge; adapter, cable, or port problems",
    "display": "screen, backlight, flicker, lines, or dead pixels",
    "keyboard": "keys or trackpad",
    "audio": "speakers, microphone, crackle, or a dead earcup",
    "overheating": "runs hot, loud fan, or thermal shutdown",
    "boot": "won't power on or start up",
    "os": "operating system errors, BSOD, or updates",
    "driver": "a device driver problem",
    "performance": "slow, freezing, or lagging",
    "connectivity": "Wi-Fi, Bluetooth, pairing, or ports",
    "other": "anything else",
}
URGENCY_OPTIONS: dict[str, str] = {
    "low": "minor or cosmetic; the device is usable",
    "medium": "the device is only partly usable",
    "high": "the device is unusable, data is at risk, or there is a safety sign such as swelling or burning smell",
}
# What a low-confidence answer counts as. Urgency has no unknown, so it gets the default ticket priority.
UNSURE: dict[str, str] = {"intent": "other", "category": "unknown", "issue_type": "other", "urgency": "medium"}

# ---------- serials ----------

# Same alphabet as SERIAL_ALPHABET in db/seed/seed.py (no I or O); tests/test_decide.py checks they match.
SERIAL_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
# Model number (2-4 uppercase letters/digits, starting with a letter like every seeded model), "-",
# then 6 serial characters. Not part of a longer token on either side.
_SERIAL = re.compile(rf"(?<![A-Z0-9-])[A-Z][A-Z0-9]{{1,3}}-[{SERIAL_ALPHABET}]{{6}}(?![A-Z0-9-])")


def extract_serial(text: str) -> str | None:
    """The serial-shaped token in `text`, e.g. "AX14-7F3K92". A regex, not a model.

    Prefers a candidate with a digit, so "re-charge" loses to a real serial in the same message.
    Still only a candidate: confirm it with catalog.lookup_serial.
    """
    candidates = _SERIAL.findall(text.upper())
    return next((c for c in candidates if any(ch.isdigit() for ch in c)), candidates[0] if candidates else None)


# ---------- redaction (before anything goes to Jev) ----------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d ().-]{7,}\d(?![\w-])")
_ADDRESS_LABEL = re.compile(r"(?im)^([ \t]*(?:service |home |delivery |billing )?address[ \t]*[:-]).*$")
_STREET = re.compile(
    r"(?i)\b\d+[a-z]?(?:[/-]\d+)?,?[ \t]+(?:[\w.'-]+,?[ \t]+){0,4}?"
    r"(?:street|st|road|rd|lane|avenue|ave|nagar|colony|layout|sector|cross|apartments?|residency|towers?|enclave|block)\b.*"
)


def redact(text: str) -> str:
    """Mask emails, phone numbers (10-15 digits), and street-address-like lines. Serials and issue text stay."""
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub(lambda m: "[phone]" if 10 <= sum(c.isdigit() for c in m.group()) <= 15 else m.group(), text)
    text = _ADDRESS_LABEL.sub(r"\1 [address]", text)
    return _STREET.sub("[address]", text)


# ---------- questions and answers ----------


class Question(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["choice", "noul"]
    instructions: str
    options: dict[str, str] = Field(default_factory=dict)  # choice only: {option: description}

    @model_validator(mode="after")
    def _choice_has_options(self) -> "Question":
        if self.type == "choice" and not self.options:
            raise ValueError("a choice question needs options")
        return self

    @classmethod
    def choice(cls, instructions: str, options: dict[str, str]) -> "Question":
        return cls(type="choice", instructions=instructions, options=options)

    @classmethod
    def noul(cls, instructions: str) -> "Question":
        """A yes/no question; the answer is p_yes."""
        return cls(type="noul", instructions=instructions)

    def to_jev(self) -> dict[str, Any]:
        if self.type == "choice":
            return {"type": "choice", "instructions": self.instructions, "criteria": self.options}
        return {"type": "noul", "instructions": self.instructions}


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["choice", "noul"]
    selected: str | None = None  # choice
    p_yes: float | None = None  # noul
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None  # None: the provider gives none, so treat it as confident
    provider: Literal["jev", "llm"]

    def is_confident(self, min_confidence: float) -> bool:
        return self.confidence is None or self.confidence >= min_confidence


class IntakeClassification(BaseModel):
    intent: Intent
    category: Category
    issue_type: IssueType
    urgency: Urgency
    low_confidence: list[str] = Field(default_factory=list)  # fields that fell back to UNSURE
    provider: Literal["jev", "llm"]


# ---------- the decider ----------


class _TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._items: OrderedDict[str, tuple[float, dict[str, Answer]]] = OrderedDict()

    def get(self, key: str) -> dict[str, Answer] | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: str, value: dict[str, Answer]) -> None:
        self._items[key] = (time.monotonic() + self._ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class Decider:
    """Answers typed questions with Jev or the LLM. The module-level functions use a shared instance."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLM | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._llm = llm
        self._http_client = http_client
        self._cache = _TTLCache(CACHE_TTL_SECONDS, CACHE_MAX_ENTRIES)

    @property
    def llm(self) -> LLM:
        return self._llm or default_llm()

    async def decide(self, state: str, questions: dict[str, Question]) -> dict[str, Answer]:
        provider = self.settings.decision_provider
        key = _cache_key(provider, state, questions)
        if (cached := self._cache.get(key)) is not None:
            return dict(cached)
        answers = None
        if provider == "jev":
            try:
                answers = await self._ask_jev(redact(state), questions)
            except Exception as e:  # any Jev failure: the llm path answers this call
                log.warning("Jev decision failed, using the LLM: %s: %s", type(e).__name__, str(e)[:200])
        if answers is None:
            answers = await self._ask_llm(state, questions)
        self._cache.put(key, answers)
        return dict(answers)

    async def classify_intake(self, text: str) -> IntakeClassification:
        """intent, category, issue_type, urgency with the §4.4 enums. Low-confidence answers count as UNSURE."""
        answers = await self.decide(text, {
            "intent": Question.choice("What does the customer want with this message?", INTENT_OPTIONS),
            "category": Question.choice("Is the problem hardware or software?", CATEGORY_OPTIONS),
            "issue_type": Question.choice("Which kind of problem is it?", ISSUE_TYPE_OPTIONS),
            "urgency": Question.choice("How urgent is it for the customer?", URGENCY_OPTIONS),
        })
        values: dict[str, str] = {}
        unsure: list[str] = []
        for field, answer in answers.items():
            if answer.is_confident(self.settings.decision_min_confidence) and answer.selected is not None:
                values[field] = answer.selected
            else:
                values[field] = UNSURE[field]
                unsure.append(field)
        return IntakeClassification(**values, low_confidence=unsure, provider=answers["intent"].provider)

    async def is_duplicate(self, new_text: str, ticket_text: str) -> float:
        """p(same problem). Only for the 0.60-0.70 similarity band of §7.2; the thresholds stay in code and settings."""
        state = f"Existing ticket:\n{ticket_text}\n\nNew customer message:\n{new_text}"
        answers = await self.decide(state, {
            "same_problem": Question.noul("The new customer message is about the same problem as the existing ticket."),
        })
        return answers["same_problem"].p_yes or 0.0

    async def _ask_jev(self, state: str, questions: dict[str, Question]) -> dict[str, Answer]:
        s = self.settings
        if not s.jev_api_key:
            raise RuntimeError("JEV_API_KEY is empty")
        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        response = await self._http_client.post(
            s.jev_base_url.rstrip("/") + "/" + s.jev_path.lstrip("/"),
            json={"model": s.jev_model, "state": state, "questions": {qid: q.to_jev() for qid, q in questions.items()}},
            headers={"Authorization": f"Bearer {s.jev_api_key}"},
            timeout=s.jev_timeout_seconds,
        )
        response.raise_for_status()
        return parse_jev_answers(response.json(), questions)

    async def _ask_llm(self, state: str, questions: dict[str, Question]) -> dict[str, Answer]:
        # Field names are q0, q1, ... with the question id as the JSON key, so any id is allowed.
        fields: dict[str, Any] = {}
        lines = []
        for i, (qid, q) in enumerate(questions.items()):
            if q.type == "choice":
                fields[f"q{i}"] = (Literal[tuple(q.options)], Field(alias=qid))
                options = "; ".join(f"{option} = {description}" for option, description in q.options.items())
                lines.append(f'- "{qid}": {q.instructions} Pick one: {options}')
            else:
                fields[f"q{i}"] = (bool, Field(alias=qid))
                lines.append(f'- "{qid}": {q.instructions} Answer true or false.')
        schema = create_model("Decisions", **fields)
        system = "Make quick decisions about the text the user sends. Answer every question:\n" + "\n".join(lines)
        parsed, _ = await self.llm.complete_json(
            [{"role": "system", "content": system}, {"role": "user", "content": state}], schema, tier="fast"
        )
        answers = {}
        for i, (qid, q) in enumerate(questions.items()):
            value = getattr(parsed, f"q{i}")
            if q.type == "choice":
                answers[qid] = Answer(type="choice", selected=value, provider="llm")
            else:
                answers[qid] = Answer(type="noul", p_yes=1.0 if value else 0.0, provider="llm")
        return answers


def parse_jev_answers(body: Any, questions: dict[str, Question]) -> dict[str, Answer]:
    """Tolerant parse of Jev's {"answers": {qid: {...}}}. Raises ValueError when any question is unanswered."""
    raw = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("no answers object in the Jev response")
    answers = {}
    for qid, q in questions.items():
        if qid not in raw:
            raise ValueError(f"Jev gave no answer for {qid!r}")
        answers[qid] = _jev_choice(raw[qid], q) if q.type == "choice" else _jev_noul(raw[qid])
    return answers


def _jev_choice(raw: Any, q: Question) -> Answer:
    if not isinstance(raw, dict):
        raise ValueError("choice answer is not an object")
    probabilities = _float_map(raw.get("probabilities"))
    selected = raw.get("choice", raw.get("selected"))
    if selected is None and probabilities:
        selected = max(probabilities, key=probabilities.__getitem__)
    if selected not in q.options:
        raise ValueError(f"choice {selected!r} is not an option")
    confidence = _number(raw.get("confidence"))
    if confidence is None:
        confidence = probabilities.get(selected)
    return Answer(type="choice", selected=selected, probabilities=probabilities, confidence=confidence, provider="jev")


def _jev_noul(raw: Any) -> Answer:
    value = raw.get("noul", raw.get("p_yes", raw.get("probability"))) if isinstance(raw, dict) else raw
    p_yes = _number(value)
    if p_yes is None or not 0.0 <= p_yes <= 1.0:
        raise ValueError(f"noul answer {value!r} is not a probability")
    confidence = _number(raw.get("confidence")) if isinstance(raw, dict) else None
    return Answer(
        type="noul",
        p_yes=p_yes,
        probabilities={"yes": p_yes, "no": 1.0 - p_yes},
        confidence=confidence if confidence is not None else max(p_yes, 1.0 - p_yes),
        provider="jev",
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(k): n for k, v in value.items() if (n := _number(v)) is not None}


def _cache_key(provider: str, state: str, questions: dict[str, Question]) -> str:
    payload = [provider, state, {qid: q.model_dump() for qid, q in questions.items()}]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@lru_cache
def default_decider() -> Decider:
    return Decider()


async def decide(state: str, questions: dict[str, Question]) -> dict[str, Answer]:
    return await default_decider().decide(state, questions)


async def classify_intake(text: str) -> IntakeClassification:
    return await default_decider().classify_intake(text)


async def is_duplicate(new_text: str, ticket_text: str) -> float:
    return await default_decider().is_duplicate(new_text, ticket_text)