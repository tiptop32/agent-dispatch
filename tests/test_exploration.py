"""Случайное, но воспроизводимое разбиение трафика маршрутизации."""

import json
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    GuardReason,
    Judgment,
    RouteDecision,
    RouterKind,
)
from agent_dispatch.routing.decision import decide_with_fallback
from agent_dispatch.routing.exploration import (
    apply_exploration,
    exploration_fraction,
    is_explore_task,
)
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter


def test_hash_fraction_is_stable_across_calls_and_restarts() -> None:
    # Литерал посчитан отдельно по первым 8 байтам SHA-256, а не функцией под тестом.
    assert exploration_fraction("task-stable") == pytest.approx(0.32853873790551003)
    assert exploration_fraction("task-stable") == exploration_fraction("task-stable")


def test_hash_selection_respects_share_boundaries() -> None:
    assert not is_explore_task("a" * 32, 0.0)
    assert not is_explore_task("a" * 32, 0.23)
    assert is_explore_task("a" * 32, 0.24)
    assert is_explore_task("b" * 32, 1.0)


def _judgment(kind: str, value: str | bool, confidence: float = 0.95) -> Judgment:
    return Judgment(kind=kind, value=value, confidence=confidence, probabilities={})


def _settings(tmp_path: Path | None = None, *, share: float = 1.0) -> Settings:
    return Settings(
        server=ServerSettings(data_dir=tmp_path or Path(".")),
        routing=RoutingSettings(fallback_executor="codex/sol", explore_share=share, review_only={}),
        executors={
            "codex/luna": ExecutorSettings(adapter="codex", tier="fast"),
            "codex/sol": ExecutorSettings(adapter="codex", tier="balanced"),
            "claude/opus": ExecutorSettings(adapter="claude", tier="strong"),
        },
    )


def _jev_decision(
    *,
    executor: str = "claude/opus",
    reason: GuardReason | None = None,
    router: RouterKind = RouterKind.jev,
    corporate: bool | None = None,
) -> RouteDecision:
    judgments = {
        "capability": _judgment("choice", "strong"),
        "judgment": _judgment("noul", False),
    }
    if corporate is not None:
        judgments["corporate_data"] = _judgment("noul", corporate)
    return RouteDecision(
        executor=executor,
        confidence=0.95,
        scores={executor: 0.95, "codex/sol": 0.04},
        router=router,
        reason=reason,
        capability="strong",
        judgments=judgments,
    )


def test_eligible_tasks_record_both_choices_and_only_selected_hash_uses_static() -> None:
    settings = _settings(share=0.5)
    candidates = settings.executors
    request = DispatchRequest(task="x", cwd=".")

    explored = apply_exploration("a" * 32, request, _jev_decision(), candidates, settings)
    jev = apply_exploration("b" * 32, request, _jev_decision(), candidates, settings)

    assert explored.executor == "codex/sol" and explored.router == RouterKind.explore
    assert jev.executor == "claude/opus" and jev.router == RouterKind.jev
    for decision, arm in ((explored, "explore"), (jev, "jev")):
        assert decision.judgments["exploration_arm"].value == arm
        assert decision.judgments["exploration_jev_choice"].value == "claude/opus"
        assert decision.judgments["exploration_static_choice"].value == "codex/sol"


@pytest.mark.asyncio
async def test_exploration_logs_the_capped_jev_choice_and_keeps_static_baseline() -> None:
    settings = _settings(share=1.0)
    request = DispatchRequest(task="x", cwd=".", max_capability="balanced")
    capped, _ = await decide_with_fallback(
        request,
        settings.executors,
        settings,
        [FakeRouter(_jev_decision())],
    )

    explored = apply_exploration("a" * 32, request, capped, settings.executors, settings)

    assert capped.meta["capability_uncapped"] == "strong"
    assert explored.judgments["exploration_jev_choice"].value == "codex/sol"
    assert explored.judgments["exploration_static_choice"].value == "codex/sol"


@pytest.mark.parametrize(
    ("dispatch_request", "decision", "is_escalation"),
    [
        (DispatchRequest(task="x", cwd=".", executor="codex/sol"), _jev_decision(), False),
        (DispatchRequest(task="x", cwd=".", kind="review"), _jev_decision(), False),
        (DispatchRequest(task="x", cwd=".", followup_of="previous"), _jev_decision(), False),
        (DispatchRequest(task="x", cwd="."), _jev_decision(), True),
        (
            DispatchRequest(task="x", cwd="."),
            _jev_decision(reason=GuardReason.low_confidence),
            False,
        ),
        (
            DispatchRequest(task="x", cwd="."),
            _jev_decision(reason=GuardReason.low_margin),
            False,
        ),
        (
            DispatchRequest(task="x", cwd="."),
            _jev_decision(reason=GuardReason.router_unavailable, router=RouterKind.fallback),
            False,
        ),
        (
            DispatchRequest(task="x", cwd="."),
            _jev_decision(router=RouterKind.claude_local),
            False,
        ),
        (
            DispatchRequest(task="x", cwd="."),
            _jev_decision(reason=GuardReason.user_override, router=RouterKind.override),
            False,
        ),
    ],
)
def test_overrides_special_links_reviews_and_fallbacks_are_not_explored(
    dispatch_request: DispatchRequest, decision: RouteDecision, is_escalation: bool
) -> None:
    result = apply_exploration(
        "a" * 32,
        dispatch_request,
        decision,
        _settings().executors,
        _settings(),
        is_escalation=is_escalation,
    )

    assert result == decision
    assert "exploration_arm" not in result.judgments


def test_corporate_and_availability_guards_still_win() -> None:
    settings = _settings()
    settings.executors["codex/luna"].corporate = True
    request = DispatchRequest(task="x", cwd=".")
    corporate = apply_exploration(
        "a" * 32,
        request,
        _jev_decision(executor="codex/luna", corporate=True),
        settings.executors,
        settings,
    )
    unavailable = apply_exploration(
        "a" * 32,
        request,
        _jev_decision(),
        settings.executors,
        settings,
        unavailable={"codex/sol"},
    )

    assert corporate.executor == "codex/luna"
    assert unavailable.executor == "claude/opus" and unavailable.router == RouterKind.jev
    # Назначение не меняется: crossover остаётся в arm `explore`.
    assert unavailable.judgments["exploration_arm"].value == "explore"
    assert unavailable.judgments["exploration_crossover"].value == "codex/sol"
    assert "недоступен" in unavailable.meta["exploration_note"]


@pytest.mark.asyncio
async def test_dispatch_persists_explore_arm_and_jev_choice(tmp_path: Path, git_repo: Path) -> None:
    settings = _settings(tmp_path)
    storage = Storage(tmp_path / "dispatch.db")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    router = FakeRouter(_jev_decision())
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    try:
        task = await dispatcher.submit(DispatchRequest(task="x", cwd=str(git_repo)))
        done = await dispatcher.wait(task.task_id, 2)
        row = storage._db.execute(
            "select router, choice, judgments_json from routing_decisions where task_id=?",
            (task.task_id,),
        ).fetchone()
    finally:
        await dispatcher.shutdown()
        await storage.close()

    judgments = json.loads(row["judgments_json"])
    assert done.decision.executor == "codex/sol" and adapters["codex/sol"].calls
    assert row["router"] == "explore" and row["choice"] == "codex/sol"
    assert judgments["exploration_arm"]["value"] == "explore"
    assert judgments["exploration_jev_choice"]["value"] == "claude/opus"


@pytest.mark.asyncio
async def test_budget_guard_stops_before_jev_and_exploration(
    tmp_path: Path, git_repo: Path
) -> None:
    settings = _settings(tmp_path)
    settings.routing.daily_cost_limit_usd = 0.01
    storage = Storage(tmp_path / "dispatch.db")
    await storage.open()
    spent = _jev_decision().model_copy(update={"cost_usd": 0.01})
    await storage.add_decision(None, spent, list(settings.executors))
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    router = FakeRouter(_jev_decision())
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    try:
        task = await dispatcher.submit(DispatchRequest(task="x", cwd=str(git_repo)))
        done = await dispatcher.wait(task.task_id, 2)
        addressed = storage._db.execute(
            "select count(*) from routing_decisions where task_id=?", (task.task_id,)
        ).fetchone()[0]
    finally:
        await dispatcher.shutdown()
        await storage.close()

    assert done.result.meta["guard"] == "budget"
    assert router.calls == 0 and addressed == 0
