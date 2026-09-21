import pytest

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, RouteDecision, RouterKind
from agent_dispatch.routing.base import RouterError
from agent_dispatch.routing.decision import build_routers, decide_with_fallback


class Bad:
    name = "bad"

    async def decide(self, req, candidates):
        raise RouterError("no")


class Good:
    name = "good"

    async def decide(self, req, candidates):
        return RouteDecision(
            router=RouterKind.claude_local, executor="claude", confidence=1, scores={"claude": 1}
        )


@pytest.mark.asyncio
async def test_fallback_chain():
    s = Settings.model_validate(
        {"executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}}}
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), {"claude": "c", "codex": "d"}, s, [Bad(), Good()]
    )
    assert d.executor == "claude" and len(events) == 1


class Counting:
    def __init__(self, name, error=False):
        self.name, self.error, self.calls = name, error, 0

    async def decide(self, req, candidates):
        self.calls += 1
        if self.error:
            raise RouterError("no")
        return RouteDecision(
            router=RouterKind.jev, executor="claude", confidence=1, scores={"claude": 1}
        )


@pytest.mark.asyncio
async def test_jev_ok_has_no_events():
    s = Settings.model_validate(
        {"executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}}}
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), {"claude": "c", "codex": "d"}, s, [Counting("jev")]
    )
    assert d.router == "jev" and not events


@pytest.mark.asyncio
async def test_jev_error_then_claude_and_event():
    s = Settings.model_validate(
        {"executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}}}
    )
    good = Counting("claude_local")
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."),
        {"claude": "c", "codex": "d"},
        s,
        [Counting("jev", True), good],
    )
    assert d.executor == "claude" and len(events) == 1 and events[0].reason == "router_unavailable"


@pytest.mark.asyncio
async def test_both_errors_fallback_confidence_zero():
    s = Settings.model_validate(
        {"executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}}}
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."),
        {"claude": "c", "codex": "d"},
        s,
        [Counting("jev", True), Counting("claude_local", True)],
    )
    assert (
        d.executor == "codex"
        and d.confidence == 0
        and d.reason == "router_unavailable"
        and len(events) == 2
    )


@pytest.mark.asyncio
async def test_fallback_unavailable_uses_first_candidate_and_event():
    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": "codex"},
            "executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}},
        }
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."),
        {"claude": "c", "other": "o"},
        s,
        [Counting("jev", True), Counting("claude_local", True)],
    )
    assert d.executor == "claude" and "warning" in d.meta and len(events) == 3


@pytest.mark.asyncio
async def test_single_candidate_skips_routers():
    s = Settings.model_validate(
        {"executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}}}
    )
    router = Counting("jev")
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), {"claude": "c"}, s, [router]
    )
    assert d.reason == "single_candidate" and not events and router.calls == 0


def test_build_routers_by_backend():
    import httpx

    s = Settings.model_validate(
        {"router": {"backend": "jev"}, "executors": {"codex": {"adapter": "codex"}}}
    )
    assert [r.name for r in build_routers(s, httpx.AsyncClient())] == ["jev", "claude_local"]
    s = Settings.model_validate(
        {"router": {"backend": "claude_local"}, "executors": {"codex": {"adapter": "codex"}}}
    )
    assert [r.name for r in build_routers(s, httpx.AsyncClient())] == ["claude_local"]
