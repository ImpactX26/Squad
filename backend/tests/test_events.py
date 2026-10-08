"""Event bus (§9), POST /internal/events and WS /ws/staff. No database: the token check is faked."""

import asyncio
import logging
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws import get_token_authenticator
from app.core.config import Settings
from app.core.events import EventBus, ListenerOverflow, bus
from app.core.logging import RedactTokens
from app.main import create_app
from app.schemas.events import EVENT_TYPES

KEY = "test-internal-key"


# ---------- the bus ----------


def test_the_event_names_are_the_sixteen_of_section_9():
    assert len(EVENT_TYPES) == 16
    assert {"ticket.created", "message.received", "payment.paid", "stock.low", "notification.created"} <= EVENT_TYPES


async def test_a_listener_gets_type_data_and_a_timezone_aware_ts():
    events = EventBus()
    with events.listen() as listener:
        published = events.publish("ticket.created", {"ticket_number": "SR-2026-00001"})
        received = await listener.get()
    assert received == published
    assert (received.type, received.data) == ("ticket.created", {"ticket_number": "SR-2026-00001"})
    assert received.ts.tzinfo is not None
    assert events.listener_count == 0


async def test_handlers_run_and_a_failing_one_harms_nothing():
    events = EventBus()
    seen = []

    async def broken(event):
        raise RuntimeError("boom")

    async def recorder(event):
        seen.append(event.data["n"])

    events.subscribe("payment.paid", broken)
    events.subscribe("payment.paid", recorder)
    events.publish("payment.paid", {"n": 1})
    events.publish("ticket.updated", {"n": 2})  # nobody subscribed to this one
    await events.drain()
    assert seen == [1]


def test_an_unknown_event_name_is_refused():
    events = EventBus()
    with pytest.raises(ValueError):
        events.publish("ticket.deleted", {})

    async def handler(event):
        pass

    with pytest.raises(ValueError):
        events.subscribe("ticket.deleted", handler)


async def test_a_listener_that_falls_behind_is_told_to_reconnect():
    events = EventBus(max_queue=2)
    with events.listen() as listener:
        for n in range(3):
            events.publish("ticket.updated", {"n": n})
        with pytest.raises(ListenerOverflow):
            await listener.get()


# ---------- the routes ----------


@pytest.fixture
def client():
    app = create_app(Settings(_env_file=None, internal_api_key=KEY))

    async def authenticate(token: str):
        return SimpleNamespace(id="staff-1", role="agent") if token == "good" else None

    app.dependency_overrides[get_token_authenticator] = lambda: authenticate
    with TestClient(app) as test_client:
        yield test_client


def wait_for_listeners(count: int) -> None:
    # The socket is accepted before the token check finishes, and leaves the bus just after it closes.
    deadline = time.monotonic() + 5
    while bus.listener_count != count:
        assert time.monotonic() < deadline, f"{bus.listener_count} sockets are listening, expected {count}"
        time.sleep(0.01)


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Key": "wrong"}])
def test_internal_events_needs_the_key(client, headers):
    response = client.post("/internal/events", json={"type": "ticket.created", "data": {}}, headers=headers)
    assert response.status_code == 401


def test_internal_events_is_503_without_a_configured_key():
    app = create_app(Settings(_env_file=None, internal_api_key=""))
    with TestClient(app) as c:
        response = c.post("/internal/events", json={"type": "ticket.created", "data": {}}, headers={"X-Internal-Key": ""})
    assert response.status_code == 503


def test_internal_events_refuses_an_unknown_event_name(client):
    response = client.post("/internal/events", json={"type": "ticket.deleted", "data": {}}, headers={"X-Internal-Key": KEY})
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/ws/staff", "/ws/staff?token=bad"])
def test_ws_staff_closes_1008_without_a_valid_token(client, path):
    with client.websocket_connect(path) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1008


def test_an_mcp_event_reaches_every_signed_in_dashboard(client):
    with client.websocket_connect("/ws/staff?token=good") as first, \
            client.websocket_connect("/ws/staff?token=good") as second:
        wait_for_listeners(2)
        response = client.post(
            "/internal/events",
            json={"type": "ticket.created", "data": {"ticket_number": "SR-2026-00001", "source_channel": "telegram"}},
            headers={"X-Internal-Key": KEY},
        )
        assert response.status_code == 202
        for socket in (first, second):
            message = socket.receive_json()
            assert set(message) == {"type", "data", "ts"}
            assert message["type"] == "ticket.created"
            assert message["data"] == {"ticket_number": "SR-2026-00001", "source_channel": "telegram"}
            assert datetime.fromisoformat(message["ts"]).tzinfo is not None
        assert message == response.json()
    wait_for_listeners(0)


def test_an_event_published_while_the_token_is_checked_is_not_lost():
    app = create_app(Settings(_env_file=None, internal_api_key=KEY))

    async def slow_authenticate(token: str):
        await asyncio.sleep(0.5)  # a database round trip
        return SimpleNamespace(id="staff-1", role="agent")

    app.dependency_overrides[get_token_authenticator] = lambda: slow_authenticate
    with TestClient(app) as c, c.websocket_connect("/ws/staff?token=good") as socket:
        # No waiting: the socket was listening before it was accepted.
        c.post("/internal/events", json={"type": "ticket.updated", "data": {"n": 1}}, headers={"X-Internal-Key": KEY})
        assert socket.receive_json()["data"] == {"n": 1}


def test_a_token_in_a_logged_websocket_path_is_redacted():
    # The shape of uvicorn's websocket log line (logger "uvicorn.error").
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:60739", "/ws/staff?token=eyJhbGciOi.secret.sig&x=1"), None,
    )
    assert RedactTokens().filter(record)
    message = record.getMessage()
    assert "eyJhbGciOi" not in message
    assert "/ws/staff?token=[redacted]&x=1" in message
