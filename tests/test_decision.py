import pytest

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import DispatchRequest, Judgment, RouteDecision, RouterKind
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
@pytest.mark.parametrize("router_kind", [RouterKind.jev, RouterKind.claude_local])
async def test_capability_ceiling_caps_every_router_choice(router_kind):
    class StrongChoice:
        name = router_kind.value

        async def decide(self, req, candidates):
            return RouteDecision(
                router=router_kind,
                executor="strong",
                capability="strong" if router_kind == RouterKind.jev else None,
                confidence=0.95,
                scores={"strong": 0.95, "balanced": 0.05},
            )

    candidates = {
        "balanced": ExecutorSettings(adapter="codex", tier="balanced"),
        "strong": ExecutorSettings(adapter="claude", tier="strong"),
    }
    settings = Settings.model_validate(
        {"routing": {"fallback_executor": "balanced"}, "executors": candidates}
    )

    decision, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd=".", max_capability="balanced"),
        candidates,
        settings,
        [StrongChoice()],
    )

    assert decision.executor == "balanced"
    assert decision.capability == "balanced"
    assert decision.meta["capability_uncapped"] == "strong"
    assert (
        "capability capped: strong -> balanced (max_capability)" in decision.meta["selection_notes"]
    )


@pytest.mark.asyncio
async def test_capability_ceiling_keeps_corporate_and_judgment_narrowing():
    class NarrowedChoice:
        name = "jev"

        async def decide(self, req, candidates):
            return RouteDecision(
                router=RouterKind.jev,
                executor="internal",
                capability="strong",
                confidence=0.95,
                scores={"internal": 0.95},
                judgments={
                    "corporate_data": Judgment(
                        kind="noul", value=True, confidence=0.95, probabilities={}
                    ),
                    "judgment": Judgment(
                        kind="noul", value=True, confidence=0.95, probabilities={}
                    ),
                },
            )

    candidates = {
        "internal": ExecutorSettings(adapter="opencode", model="x", tier="fast", corporate=True),
        "balanced": ExecutorSettings(adapter="codex", tier="balanced"),
        "strong": ExecutorSettings(adapter="claude", tier="strong"),
    }
    settings = Settings.model_validate(
        {"routing": {"fallback_executor": "balanced"}, "executors": candidates}
    )

    decision, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd=".", max_capability="balanced"),
        candidates,
        settings,
        [NarrowedChoice()],
    )

    assert decision.executor == "internal"


@pytest.mark.asyncio
async def test_capped_decision_notes_describe_the_new_choice_not_the_old_one():
    # Живой случай 2026-10-15: после потолка в notes оставалось «sent to a
    # reasoning agent», хотя задача ушла к balanced-исполнителю.
    low_corporate = "corporate_data answered yes but only at confidence 0.10"

    class JudgmentChoice:
        name = "jev"

        async def decide(self, req, candidates):
            return RouteDecision(
                router=RouterKind.jev,
                executor="opus",
                capability="strong",
                confidence=0.95,
                scores={"opus": 0.95},
                judgments={
                    "judgment": Judgment(kind="noul", value=True, confidence=0.9, probabilities={})
                },
                meta={
                    "selection_notes": [
                        "task needs judgment rather than execution; sent to a reasoning agent",
                        low_corporate,
                    ]
                },
            )

    candidates = {
        "x5": ExecutorSettings(adapter="opencode", model="x", tier="balanced"),
        "opus": ExecutorSettings(adapter="claude", tier="strong"),
    }
    settings = Settings.model_validate(
        {"routing": {"fallback_executor": "x5"}, "executors": candidates}
    )

    decision, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd=".", max_capability="balanced"),
        candidates,
        settings,
        [JudgmentChoice()],
    )

    notes = decision.meta["selection_notes"]
    assert decision.executor == "x5"
    assert not any("sent to a reasoning agent" in note for note in notes)
    assert any("no reasoning agent within max_capability" in note for note in notes)
    assert low_corporate in notes


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


@pytest.mark.asyncio
async def test_all_routers_failed_with_perimeter_picks_corporate_executor():
    """Оба роутера упали, периметр включён: fallback не выводит задачу за периметр."""
    candidates = {
        "internal": ExecutorSettings(adapter="opencode", model="m", corporate=True),
        "codex": ExecutorSettings(adapter="codex"),
    }
    settings = Settings.model_validate(
        {"routing": {"fallback_executor": "codex"}, "executors": candidates}
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), candidates, settings, [Bad(), Bad()]
    )
    assert d.executor == "internal"
    assert d.meta["perimeter"] == "unknown"
    assert [e.reason for e in events].count("router_unavailable") == 2


@pytest.mark.asyncio
async def test_all_routers_failed_without_perimeter_keeps_fallback_chain():
    """Периметр выключен: прежний fallback по всей цепочке без сужения."""
    candidates = {
        "internal": ExecutorSettings(adapter="opencode", model="m", corporate=True),
        "codex": ExecutorSettings(adapter="codex"),
    }
    settings = Settings.model_validate(
        {
            "routing": {"fallback_executor": "codex", "corporate_perimeter": False},
            "executors": candidates,
        }
    )
    d, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), candidates, settings, [Bad(), Bad()]
    )
    assert d.executor == "codex"
    assert "perimeter" not in d.meta


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


@pytest.mark.asyncio
async def test_fallback_chain_skips_unavailable_head_without_warning():
    """Роутеры недоступны, голова цепочки остывает: берётся следующий из цепочки."""
    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["opencode/kimi", "codex"]},
            "executors": {
                "claude": {"adapter": "claude"},
                "codex": {"adapter": "codex"},
                "opencode/kimi": {"adapter": "opencode", "model": "kimi"},
            },
        }
    )
    d, events = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), {"claude": "c", "codex": "x"}, s, [Bad()]
    )
    assert d.executor == "codex" and "warning" not in d.meta and len(events) == 1


def _perimeter_settings():
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": ["external"]},
            "executors": {
                "external": {"adapter": "codex", "tier": "balanced"},
                "internal": {
                    "adapter": "opencode",
                    "model": "m",
                    "tier": "balanced",
                    "corporate": True,
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_single_corporate_candidate_asks_the_router_under_the_perimeter():
    """Один корпоративный кандидат: вопрос задаётся, «неизвестно» только при отказе.

    Короткий путь без вопроса либо выпускал бы эскалацию наружу, либо запирал
    всю линию задачи в периметре вслепую.
    """
    s = _perimeter_settings()
    pool = {"internal": s.executors["internal"]}
    router = Counting("jev", error=True)
    d, _ = await decide_with_fallback(DispatchRequest(task="x", cwd="."), pool, s, [router])
    assert router.calls == 1
    assert d.executor == "internal" and d.meta["perimeter"] == "unknown"

    class AnsweredNo:
        name = "jev"

        async def decide(self, req, candidates):
            return RouteDecision(
                router=RouterKind.jev,
                executor="internal",
                confidence=0.99,
                scores={"internal": 0.99},
                judgments={
                    "corporate_data": Judgment(
                        kind="noul", value=False, confidence=0.9, probabilities={"true": 0.05}
                    )
                },
            )

    d, _ = await decide_with_fallback(DispatchRequest(task="x", cwd="."), pool, s, [AnsweredNo()])
    assert d.executor == "internal" and "perimeter" not in d.meta


@pytest.mark.asyncio
async def test_single_external_candidate_asks_the_router_under_the_perimeter():
    """Единственный внешний кандидат при включённом периметре идёт через роутер."""
    s = _perimeter_settings()
    d, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd="."), {"external": s.executors["external"]}, s, [Bad()]
    )
    # Роутер упал, ответа нет: решение помечено, отказ выносит диспетчер.
    assert d.meta["perimeter"] == "unknown"
    assert any("no in-perimeter executor" in note for note in d.meta["selection_notes"])


@pytest.mark.asyncio
async def test_all_routers_failed_fallback_respects_max_capability_without_perimeter():
    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["strong"]},
            "executors": {
                "strong": {"adapter": "claude", "tier": "strong"},
                "cheap": {"adapter": "codex", "tier": "fast"},
            },
        }
    )
    d, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd=".", max_capability="fast"),
        {"strong": s.executors["strong"], "cheap": s.executors["cheap"]},
        s,
        [Bad()],
    )
    assert d.executor == "cheap"


@pytest.mark.asyncio
async def test_all_routers_failed_and_nothing_within_ceiling_uses_the_fallback_chain():
    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["second"]},
            "executors": {
                "first": {"adapter": "claude", "tier": "strong"},
                "second": {"adapter": "codex", "tier": "strong"},
            },
        }
    )
    d, _ = await decide_with_fallback(
        DispatchRequest(task="x", cwd=".", max_capability="fast"),
        {"first": s.executors["first"], "second": s.executors["second"]},
        s,
        [Bad()],
    )
    assert d.executor == "second"
    assert any("ceiling was not applied" in note for note in d.meta["selection_notes"])
