"""Intake v1 (§7.1 without duplicates): a fake hub and a fake model (tests/llm_fakes.py), never a real one.

The last test runs the real pipeline against the database through the in-process MCP servers.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.brain import intake
from app.brain.intake import FALLBACK_REPLY_TICKET, INTAKE_TOOLS, extract_serial, handle_inbound, pick_plan
from app.brain.mcp_hub import MCPHub, ToolResult
from app.channels import identity
from app.channels.base import InboundMessage
from app.channels.inbound import FALLBACK_REPLY_NO_TICKET
from app.core.db import get_engine, get_sessionmaker
from mcp_servers import catalog_server, knowledge_server, messaging_server, tickets_server
from tests.llm_fakes import FakeProviders, chat, error, fake_llm
from tests.mcp_support import pool, published, rows  # noqa: F401 (fixtures)

CUSTOMER = uuid.uuid4()
CONVERSATION = uuid.uuid4()
TICKET = str(uuid.uuid4())
PRODUCT = str(uuid.uuid4())
MODEL = str(uuid.uuid4())

PLAYBOOK = ["Try another adapter and wall socket", "BIOS battery reset", "Battery health test"]


def device(owner=str(CUSTOMER), warranty="in_warranty", serial="VX15-Q8M2D5"):
    return {"found": True, "product_id": PRODUCT, "serial_number": serial, "model_id": MODEL, "model_number": "VX15",
            "brand": "Vertex", "name": "Vertex 15", "category": "laptop", "color": "Shadow Black",
            "warranty": {"status": warranty}, "owner_customer_id": owner}


class FakeHub:
    """Answers the intake tools from scripted data and records every call."""

    def __init__(self, devices=None, playbook=PLAYBOOK, refuse=()):
        self.devices = {"VX15-Q8M2D5": device()} if devices is None else devices
        self.playbook = playbook
        self.refuse = set(refuse)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments, *, allowed):
        assert allowed == INTAKE_TOOLS and name in INTAKE_TOOLS
        self.calls.append((name, arguments))
        if name in self.refuse:
            return ToolResult(name, False, {"error": "owned_by_another" if "link" in name else "refused"}, 1)
        data = {
            "catalog__lookup_serial": lambda: self.devices.get(arguments["serial_number"],
                                                               {"found": False, "serial_number": arguments["serial_number"]}),
            "tickets__create_ticket": lambda: {"ticket_id": TICKET, "ticket_number": "SR-2026-00042", "status": "new"},
            "knowledge__get_playbook": lambda: ({"found": True, "steps": [{"number": n, "step": s} for n, s in
                                                                         enumerate(self.playbook, start=1)]}
                                                if self.playbook and arguments["issue_type"] != "other"
                                                else {"found": False}),
        }.get(name, lambda: {"ok": True})()
        return ToolResult(name, True, data, 1)

    def args(self, name) -> list[dict]:
        return [a for n, a in self.calls if n == name]

    @property
    def replies(self) -> list[str]:
        return [a["text"] for a in self.args("messaging__send_reply")]

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


@pytest.fixture
def conversation(monkeypatch):
    """A fake identity layer: the resolved conversation, and every context written."""
    state = {"context": {}, "ticket_id": None, "repeat": False, "name": "Aman Verma", "written": []}

    async def resolve(inbound):
        return identity.Resolved(
            customer_id=CUSTOMER, conversation_id=CONVERSATION, message_id=uuid.uuid4(), channel=inbound.channel,
            external_thread_id=inbound.external_thread_id, ticket_id=state["ticket_id"], context=dict(state["context"]),
            customer_name=state["name"], customer_email=None, new_customer=False, new_conversation=False,
            repeat=state["repeat"])

    async def set_context(conversation_id, context, session=None):
        assert conversation_id == CONVERSATION
        state["context"] = context
        state["written"].append(context)

    monkeypatch.setattr(identity, "resolve", resolve)
    monkeypatch.setattr(identity, "set_context", set_context)
    return state


def message(body: str, channel="telegram") -> InboundMessage:
    return InboundMessage(channel, "555", "555", "Aman", body, [], uuid.uuid4().hex, {})


def extraction(intent="new_issue", category="hardware", issue_type="battery", summary="Battery not charging",
               urgency="high", serial=None) -> object:
    return chat(json.dumps({"intent": intent, "category": category, "issue_type": issue_type, "summary": summary,
                            "symptoms": [], "serial_number": serial, "model_number": None, "urgency": urgency,
                            "language": "en", "extracted_fields": {}}))


def notes(summary="Vertex 15 battery won't charge.", plan=("BIOS battery reset", "Battery health test")) -> object:
    return chat(json.dumps({"summary": summary, "plan": list(plan)}))


def model(*replies, fallback=False):
    fakes = FakeProviders().queue("groq", *replies)
    return fakes, fake_llm(fakes, **({} if fallback else {"llm_fallback_provider": ""}))


# ---------- the serial ----------


@pytest.mark.parametrize("written, serial", [
    ("My Vertex 15, serial VX15-Q8M2D5, won't charge", "VX15-Q8M2D5"),
    ("serial: vx15-q8m2d5.", "VX15-Q8M2D5"),
    ("(AX14-7F3K92)", "AX14-7F3K92"),
    ("I re-booted it and pre-loaded windows", None),
    ("my i7 laptop", None),
    ("AX14-7F3K921 is too long", None),
])
def test_extract_serial(written, serial):
    assert extract_serial(written) == serial


def test_the_plan_is_only_ever_playbook_steps():
    assert pick_plan(["battery health TEST ", "Open the case and resolder the IC"], PLAYBOOK) == (
        ["Battery health test"], "ai")
    assert pick_plan(["Something invented"], PLAYBOOK) == (PLAYBOOK, "playbook")
    assert pick_plan([], []) == ([], "playbook")


# ---------- the pipeline ----------


async def test_a_redelivered_message_does_nothing(conversation):
    conversation["repeat"] = True
    hub, (fakes, llm) = FakeHub(), model()
    await handle_inbound(message("hello"), hub=hub, llm=llm)
    assert hub.calls == [] and fakes.requests["groq"] == []


async def test_everything_in_one_message_opens_the_ticket_at_once(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(), notes())
    await handle_inbound(message("My Vertex 15, serial VX15-Q8M2D5, won't charge past 0%"), hub=hub, llm=llm)

    assert hub.names == ["catalog__lookup_serial", "tickets__create_ticket", "knowledge__get_playbook",
                         "tickets__update_summary", "tickets__set_diagnostic_plan", "messaging__send_reply"]
    [created] = hub.args("tickets__create_ticket")
    assert created == {
        "customer_id": str(CUSTOMER), "product_id": PRODUCT, "category": "hardware", "issue_type": "battery",
        "title": "Battery not charging", "description": "My Vertex 15, serial VX15-Q8M2D5, won't charge past 0%",
        "source_channel": "telegram", "conversation_id": str(CONVERSATION), "flags": [], "priority": "high",
    }
    assert hub.args("knowledge__get_playbook") == [{"issue_type": "battery", "category": "laptop", "model_id": MODEL}]
    assert hub.args("tickets__update_summary") == [{"ticket_id": TICKET, "summary": "Vertex 15 battery won't charge."}]
    assert hub.args("tickets__set_diagnostic_plan") == [
        {"ticket_id": TICKET, "steps": ["BIOS battery reset", "Battery health test"], "suggested_by": "ai"}]
    [reply] = hub.replies
    assert reply == ("Thanks, Aman. Your ticket is SR-2026-00042, for your Vertex 15 (serial VX15-Q8M2D5): "
                     "Battery not charging. An agent will look into it and reply to you here.")
    assert hub.args("messaging__send_reply")[0]["conversation_id"] == str(CONVERSATION)
    assert conversation["written"] == []  # nothing to remember
    # Two fast-tier JSON calls, nothing else.
    assert [r["model"] for r in fakes.requests["groq"]] == ["qwen/qwen3.8-27b"] * 2
    assert all(r["response_format"] == {"type": "json_object"} for r in fakes.requests["groq"])


async def test_no_serial_asks_for_it_and_the_serial_reply_opens_the_ticket(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(), notes())
    await handle_inbound(message("My laptop battery won't charge"), hub=hub, llm=llm)

    assert hub.names == ["messaging__send_reply"]
    assert "serial number" in hub.replies[0] and "wmic bios get serialnumber" in hub.replies[0]
    assert conversation["context"]["awaiting"] == "serial_number"
    assert conversation["context"]["issue"] == {"category": "hardware", "issue_type": "battery",
                                                "title": "Battery not charging",
                                                "text": "My laptop battery won't charge", "urgency": "high"}

    await handle_inbound(message("VX15-Q8M2D5"), hub=hub, llm=llm)
    [created] = hub.args("tickets__create_ticket")
    assert created["title"] == "Battery not charging" and created["description"] == "My laptop battery won't charge"
    assert conversation["context"] == {}  # the slot is closed
    assert "SR-2026-00042" in hub.replies[-1] and "VX15-Q8M2D5" in hub.replies[-1]
    assert len(fakes.requests["groq"]) == 2  # the bare serial cost no extraction


async def test_more_words_with_the_serial_are_added_to_the_issue(conversation):
    conversation["context"] = {"awaiting": "serial_number", "misses": 0, "asked_at": datetime.now(UTC).isoformat(),
                               "issue": {"category": "unknown", "issue_type": "other", "title": "Laptop problem",
                                         "text": "Hi, my laptop has a problem", "urgency": "medium"}}
    hub, (fakes, llm) = FakeHub(), model(extraction(summary="Battery drains in an hour"), notes())
    await handle_inbound(message("It's VX15-Q8M2D5 and the battery drains in an hour"), hub=hub, llm=llm)
    [created] = hub.args("tickets__create_ticket")
    assert created["issue_type"] == "battery" and created["title"] == "Battery drains in an hour"
    assert created["description"] == "Hi, my laptop has a problem\n\nIt's VX15-Q8M2D5 and the battery drains in an hour"


async def test_an_unknown_serial_is_asked_for_again_then_flagged_unverified(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(), notes(plan=()))
    await handle_inbound(message("Battery dead, serial VX15-ZZZZZZ"), hub=hub, llm=llm)
    assert "VX15-ZZZZZZ" in hub.replies[0] and hub.args("tickets__create_ticket") == []
    assert conversation["context"]["misses"] == 1

    await handle_inbound(message("Sorry, VX15-YYYYYY"), hub=hub, llm=llm)
    [created] = hub.args("tickets__create_ticket")
    assert created["product_id"] is None and created["flags"] == ["unverified_product"]
    assert "couldn't match a serial number" in hub.replies[-1] and "SR-2026-00042" in hub.replies[-1]
    assert hub.args("knowledge__get_playbook") == []  # no device, no playbook
    assert conversation["context"] == {}


async def test_two_replies_without_a_serial_open_an_unverified_ticket(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(), notes(plan=()))
    await handle_inbound(message("Battery dead"), hub=hub, llm=llm)
    await handle_inbound(message("no"), hub=hub, llm=llm)
    assert "couldn't see a serial number" in hub.replies[-1]
    await handle_inbound(message("dunno"), hub=hub, llm=llm)
    [created] = hub.args("tickets__create_ticket")
    assert created["flags"] == ["unverified_product"]


async def test_a_device_owned_by_someone_else_is_flagged_not_moved(conversation):
    hub = FakeHub(devices={"VX15-Q8M2D5": device(owner=str(uuid.uuid4()), warranty="out_of_warranty")})
    fakes, llm = model(extraction(), notes())
    await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert hub.args("catalog__link_product_to_customer") == []
    [created] = hub.args("tickets__create_ticket")
    assert created["product_id"] == PRODUCT and created["flags"] == ["ownership_mismatch", "out_of_warranty"]


async def test_a_device_with_no_owner_is_registered_to_the_customer(conversation):
    hub = FakeHub(devices={"VX15-Q8M2D5": device(owner=None)})
    fakes, llm = model(extraction(), notes())
    await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert hub.args("catalog__link_product_to_customer") == [{"product_id": PRODUCT, "customer_id": str(CUSTOMER)}]
    assert hub.args("tickets__create_ticket")[0]["flags"] == []


async def test_a_link_refused_meanwhile_flags_the_ticket(conversation):
    hub = FakeHub(devices={"VX15-Q8M2D5": device(owner=None)}, refuse={"catalog__link_product_to_customer"})
    fakes, llm = model(extraction(), notes())
    await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert hub.args("tickets__create_ticket")[0]["flags"] == ["ownership_mismatch"]


async def test_software_issues_get_two_self_help_tips_hardware_none(conversation):
    steps = ["Note the stop code", "Start in safe mode", "Run sfc /scannow"]
    hub = FakeHub(playbook=steps)
    fakes, llm = model(extraction(category="software", issue_type="os", summary="Blue screen on start"),
                       notes(plan=steps))
    await handle_inbound(message("VX15-Q8M2D5 keeps showing a blue screen"), hub=hub, llm=llm)
    assert hub.replies[-1].endswith("While you wait, you could try:\n1. Note the stop code\n2. Start in safe mode")


async def test_smalltalk_gets_a_greeting_and_no_ticket(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(intent="smalltalk", category="unknown", issue_type="other"))
    await handle_inbound(message("hi there"), hub=hub, llm=llm)
    assert hub.names == ["messaging__send_reply"] and "support assistant" in hub.replies[0]
    assert conversation["written"] == []


async def test_a_follow_up_on_the_conversation_ticket_is_acknowledged(conversation):
    conversation["ticket_id"] = uuid.UUID(TICKET)
    hub, (fakes, llm) = FakeHub(), model(extraction(intent="follow_up"))
    await handle_inbound(message("any update on my laptop?"), hub=hub, llm=llm)
    assert hub.names == ["messaging__send_reply"] and hub.replies == [intake.FOLLOW_UP]


async def test_a_follow_up_with_no_ticket_is_a_new_issue_after_all(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(intent="follow_up"), notes())
    await handle_inbound(message("still waiting on VX15-Q8M2D5, battery dead"), hub=hub, llm=llm)
    assert len(hub.args("tickets__create_ticket")) == 1


async def test_a_serial_slot_older_than_a_day_is_dropped(conversation):
    conversation["context"] = {"awaiting": "serial_number", "misses": 1,
                               "asked_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(), "issue": {}}
    hub, (fakes, llm) = FakeHub(), model(extraction(intent="smalltalk"))
    await handle_inbound(message("hello again"), hub=hub, llm=llm)
    assert conversation["context"] == {} and "support assistant" in hub.replies[0]


# ---------- no model (§4.6, §15) ----------


async def test_no_model_and_no_serial_sends_the_no_ticket_fallback(conversation):
    hub, (fakes, llm) = FakeHub(), model(error(503))
    await handle_inbound(message("my laptop is broken"), hub=hub, llm=llm)
    assert hub.names == ["messaging__send_reply"] and hub.replies == [FALLBACK_REPLY_NO_TICKET]
    assert conversation["written"] == []


async def test_no_model_with_a_known_serial_still_opens_the_ticket(conversation):
    hub, (fakes, llm) = FakeHub(), model(error(503))
    await handle_inbound(message("VX15-Q8M2D5 won't charge\nplease help"), hub=hub, llm=llm)
    [created] = hub.args("tickets__create_ticket")
    assert (created["category"], created["issue_type"], created["title"]) == ("unknown", "other",
                                                                              "VX15-Q8M2D5 won't charge")
    # No summary, no plan (issue type other has no playbook), and one model call in all: no second
    # wait on a dead provider.
    assert hub.args("tickets__update_summary") == [] and hub.args("tickets__set_diagnostic_plan") == []
    assert hub.replies == [FALLBACK_REPLY_TICKET.format(ticket_number="SR-2026-00042")]
    assert len(fakes.requests["groq"]) == 1


async def test_the_model_failing_after_the_ticket_keeps_the_playbook_plan(conversation):
    hub, (fakes, llm) = FakeHub(), model(extraction(), error(503))
    await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert hub.args("tickets__set_diagnostic_plan") == [{"ticket_id": TICKET, "steps": PLAYBOOK,
                                                         "suggested_by": "playbook"}]
    assert hub.replies == [FALLBACK_REPLY_TICKET.format(ticket_number="SR-2026-00042")]


async def test_the_fallback_provider_answers_when_groq_is_down(conversation):
    hub = FakeHub()
    fakes, llm = model(error(503), fallback=True)
    fakes.queue("ollama", extraction(), notes())
    fakes.queue("groq", error(503))
    await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert "Your ticket is SR-2026-00042" in hub.replies[0]


async def test_a_refused_ticket_raises_so_channels_send_their_fallback(conversation):
    hub, (fakes, llm) = FakeHub(refuse={"tickets__create_ticket"}), model(extraction())
    with pytest.raises(intake.IntakeError, match="create_ticket refused"):
        await handle_inbound(message("VX15-Q8M2D5 battery dead"), hub=hub, llm=llm)
    assert hub.replies == []


# ---------- the real thing ----------


async def test_the_happy_path_against_the_database(pool, published, rows):  # noqa: F811
    """A Telegram account already linked to the customer reports their laptop: a real ticket, plan and reply."""
    hub = MCPHub({"tickets": tickets_server.mcp, "catalog": catalog_server.mcp,
                  "knowledge": knowledge_server.mcp, "messaging": messaging_server.mcp})
    serial = f"TS{uuid.uuid4().int % 900 + 100}-{rows['tag']}"  # the fixture's own serial may have no digit
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO customer_identities (customer_id, channel, external_user_id, display_name)
               VALUES ($1, 'telegram', $2, 'Smoke')""", uuid.UUID(rows["customer_id"]), f"smoke-{rows['tag']}")
        product = await conn.fetchval(  # deleted with the fixture's model
            """INSERT INTO products (serial_number, model_id, warranty_until, customer_id)
               VALUES ($1, $2, current_date + 100, $3) RETURNING id""",
            serial, uuid.UUID(rows["model_id"]), uuid.UUID(rows["customer_id"]))
    inbound = InboundMessage("telegram", f"smoke-{rows['tag']}", f"smoke-{rows['tag']}", "Smoke",
                             f"My laptop {serial.lower()} won't charge", [], uuid.uuid4().hex, {})
    fakes, llm = model(extraction(), notes(plan=PLAYBOOK[:1]))
    try:
        await handle_inbound(inbound, hub=hub, llm=llm)
        async with get_sessionmaker()() as db:
            ticket = (await db.execute(text(
                """SELECT t.id, t.ticket_number, t.product_id, t.issue_type, t.priority, t.ai_summary, t.flags,
                          c.ticket_id AS conversation_ticket
                   FROM tickets t JOIN conversations c ON c.id = :conv WHERE t.customer_id = :customer"""),
                {"conv": uuid.UUID(rows["conversation_id"]), "customer": uuid.UUID(rows["customer_id"])})).one()
            reply = (await db.execute(text(
                """SELECT m.body, m.ticket_id, o.status FROM messages m JOIN outbox o ON o.message_id = m.id
                   WHERE m.conversation_id = :conv AND m.sender_type = 'ai'"""),
                {"conv": uuid.UUID(rows["conversation_id"])})).one()
            steps = (await db.execute(text(
                "SELECT step, suggested_by FROM diagnostic_steps WHERE ticket_id = :t ORDER BY position"),
                {"t": ticket.id})).all()
    finally:
        await get_engine().dispose()  # its connections belong to this test's event loop

    assert ticket.product_id == product and ticket.conversation_ticket == ticket.id
    assert (ticket.issue_type, ticket.priority, ticket.flags) == ("battery", "high", [])
    assert ticket.ai_summary == "Vertex 15 battery won't charge."
    # Queued; any API running against this database may already be delivering it.
    assert reply.ticket_id == ticket.id and reply.status in ("pending", "sent")
    assert ticket.ticket_number in reply.body and serial in reply.body
    assert [(event, data["ticket_number"]) for event, data in published if event == "ticket.created"] == [
        ("ticket.created", ticket.ticket_number)]
    if steps:  # the seeded laptop battery playbook, when the database is seeded
        assert steps[0].suggested_by in ("ai", "playbook")
