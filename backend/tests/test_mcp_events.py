"""mcp_servers/common/events.py: POST {type, data} to BACKEND_URL/internal/events, never fail the tool."""

import json
import logging
import uuid

import httpx

from mcp_servers.common import events, settings


def settings_with(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings.get_settings.cache_clear()


async def test_publish_posts_type_and_data_with_the_internal_key(monkeypatch):
    settings_with(monkeypatch, BACKEND_URL="http://127.0.0.1:8000/", INTERNAL_API_KEY="test-internal-key")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["X-Internal-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(202)

    ticket_id = uuid.uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await events.publish("ticket.created", {"ticket_id": ticket_id}, client=client)
    settings.get_settings.cache_clear()

    assert ok is True
    assert seen == {
        "url": "http://127.0.0.1:8000/internal/events",
        "key": "test-internal-key",
        "body": {"type": "ticket.created", "data": {"ticket_id": str(ticket_id)}},
    }


async def test_publish_failure_is_a_warning_not_an_error(monkeypatch, caplog):
    settings_with(monkeypatch, BACKEND_URL="http://127.0.0.1:8000")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)  # the route isn't built yet

    with caplog.at_level(logging.WARNING, logger=events.log.name):
        for handler in (refused, not_found):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                assert await events.publish("ticket.updated", {"reason": "test"}, client=client) is False
    settings.get_settings.cache_clear()

    assert len(caplog.records) == 2
    assert all("ticket.updated not delivered" in r.getMessage() for r in caplog.records)
