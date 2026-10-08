"""The adapter registry in app/channels/base.py."""

import pytest

from app.channels.base import Adapters


class FakeAdapter:
    def __init__(self, channel: str) -> None:
        self.channel = channel

    async def start(self) -> None: ...

    async def send(self, thread_id: str, text: str, meta: dict) -> str:
        return "1"

    async def typing(self, thread_id: str) -> None: ...


def test_one_adapter_per_channel():
    adapters = Adapters()
    telegram = FakeAdapter("telegram")
    adapters.add(telegram)
    adapters.add(FakeAdapter("web"))
    assert adapters.get("telegram") is telegram and adapters.get("discord") is None
    assert adapters.connected() == ["telegram", "web"]
    with pytest.raises(ValueError):
        adapters.add(FakeAdapter("telegram"))
    with pytest.raises(ValueError):
        adapters.add(FakeAdapter("sms"))
    adapters.remove("telegram")
    assert adapters.connected() == ["web"]
