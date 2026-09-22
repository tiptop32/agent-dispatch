import asyncio
import time
from datetime import UTC, datetime

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, Settings
from agent_dispatch.executors.claude import ClaudeAdapter
from agent_dispatch.executors.registry import AvailabilityCache, build_adapters
from agent_dispatch.models import Availability


def settings():
    return Settings(
        executors={
            "a": ExecutorSettings(adapter="claude"),
            "b": ExecutorSettings(adapter="codex", enabled=False),
            "c": ExecutorSettings(adapter="opencode", model="m"),
        },
        routing=RoutingSettings(fallback_executor="a"),
    )


def test_build_adapters():
    adapters = build_adapters(settings())
    assert set(adapters) == {"a", "c"}
    assert isinstance(adapters["a"], ClaudeAdapter)


def test_unavailable_empty_before_check():
    assert AvailabilityCache({}, 10).unavailable() == set()


@pytest.mark.asyncio
async def test_check_all_runs_adapters_concurrently():
    class Slow(Stub):
        async def check(self):
            await asyncio.sleep(0.2)
            return await super().check()

    cache = AvailabilityCache({"a": Slow(True), "b": Slow(True)}, 10)
    started = time.monotonic()
    await cache.check_all()
    assert time.monotonic() - started < 0.35


@pytest.mark.asyncio
async def test_cache_ttl_expiry_rechecks():
    now = [0.0]
    stub = Stub(True)
    cache = AvailabilityCache({"a": stub}, 1, clock=lambda: now[0])
    await cache.check_all()
    now[0] = 2
    await cache.check_all()
    assert stub.calls == 2


@pytest.mark.asyncio
async def test_cache_force_rechecks_live_value():
    stub = Stub(True)
    cache = AvailabilityCache({"a": stub}, 100)
    await cache.check_all()
    await cache.check_all(force=True)
    assert stub.calls == 2


def test_cache_unknown_executor_is_none():
    assert AvailabilityCache({}, 1).get("unknown") is None


def test_build_adapters_passes_base_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/test-home")
    adapter = build_adapters(settings())["a"]
    assert adapter.base_env["HOME"] == "/test-home"


class Stub:
    def __init__(self, value):
        self.value, self.calls = value, 0

    async def check(self):
        self.calls += 1
        return Availability(available=self.value, checked_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_availability_cache_ttl_force_and_unavailable():
    now = [0.0]
    a, b = Stub(True), Stub(False)
    cache = AvailabilityCache({"a": a, "b": b}, 10, clock=lambda: now[0])
    await cache.check_all()
    await cache.check_all()
    assert (a.calls, b.calls) == (1, 1)
    assert cache.unavailable() == {"b"} and cache.get("missing") is None
    await cache.check_all(force=True)
    assert a.calls == 2
    now[0] = 11
    await cache.check_all()
    assert a.calls == 3
