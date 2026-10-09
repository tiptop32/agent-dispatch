"""Потолки расходов: цепочка эскалации и локальные сутки."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_dispatch.config import (
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
    load_settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    GuardReason,
    RouteDecision,
    RouterKind,
    SourceAgent,
    TaskRecord,
    TaskStatus,
    Usage,
)
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 5.0


def _record(
    task_id: str,
    cost: float | None,
    *,
    parent: str | None = None,
    finished: datetime | None = None,
) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_task_id=parent,
        escalated_from=None,
        root_agent=SourceAgent.cli,
        source_agent=SourceAgent.cli,
        hop=1 if parent else 0,
        request=DispatchRequest(task="t", cwd="."),
        status=TaskStatus.completed,
        decision=None,
        result=ExecutionResult(
            status="completed",
            executor="claude",
            model=None,
            summary="",
            usage=Usage(cost_usd=cost) if cost is not None else None,
        ),
        log_path="",
        created_at=now,
        started_at=now,
        finished_at=finished or now,
    )


async def _storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    return storage


@pytest.mark.asyncio
async def test_tree_cost_sums_tasks_and_all_their_subtasks(tmp_path):
    storage = await _storage(tmp_path)
    for record in (
        _record("root", 1.0),
        _record("child", 0.5, parent="root"),
        _record("grandchild", 0.25, parent="child"),
        _record("unpriced", None, parent="root"),
        _record("other", 9.0),
    ):
        await storage.insert_task(record)

    assert await storage.tree_cost(["root"]) == pytest.approx(1.75)
    assert await storage.tree_cost(["child", "other"]) == pytest.approx(9.75)
    assert await storage.tree_cost([]) == 0.0


@pytest.mark.asyncio
async def test_cost_since_counts_finished_tasks_and_router_decisions(tmp_path):
    storage = await _storage(tmp_path)
    now = datetime.now(UTC)
    await storage.insert_task(_record("today", 1.5, finished=now))
    await storage.insert_task(_record("yesterday", 7.0, finished=now - timedelta(days=1)))
    decision = RouteDecision(
        executor="claude", confidence=0.9, scores={}, router=RouterKind.jev, cost_usd=0.01
    )
    await storage.add_decision(None, decision, ["claude"])

    assert await storage.cost_since(now - timedelta(hours=1)) == pytest.approx(1.51)


def _settings(tmp_path: Path, **routing) -> Settings:
    return Settings(
        server=ServerSettings(data_dir=tmp_path / "data"),
        routing=RoutingSettings(fallback_executor="codex", **routing),
        executors={
            "codex": ExecutorSettings(adapter="codex", description="codex"),
            "claude": ExecutorSettings(adapter="claude", description="claude"),
        },
        escalation={"codex": ["claude"]},
    )


async def _dispatcher(tmp_path: Path, settings: Settings, results: dict[str, ExecutionResult]):
    storage = await _storage(tmp_path)
    adapters = {name: FakeAdapter(name, result=results.get(name)) for name in settings.executors}
    router = FakeRouter()
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    return storage, adapters, router, dispatcher


def _failed(cost: float) -> ExecutionResult:
    return ExecutionResult(
        status="failed", executor="codex", model=None, summary="", usage=Usage(cost_usd=cost)
    )


@pytest.mark.asyncio
async def test_chain_cost_cap_stops_escalation(tmp_path, git_repo):
    settings = _settings(tmp_path, max_chain_cost_usd=0.5)
    _, adapters, _, dispatcher = await _dispatcher(tmp_path, settings, {"codex": _failed(0.8)})
    record = await dispatcher.submit(
        DispatchRequest(task="fix", cwd=str(git_repo), executor="codex")
    )
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id
    assert done.status == TaskStatus.failed
    assert done.result.meta["budget"] == {"chain_cost_usd": 0.8, "limit_usd": 0.5}
    assert done.result.meta["escalation_chain"] == ["codex"]
    assert not adapters["claude"].calls


@pytest.mark.asyncio
async def test_chain_cost_cap_names_itself_when_the_result_was_not_failed(tmp_path, git_repo):
    asked = ExecutionResult(
        status="needs_escalation",
        executor="codex",
        model=None,
        summary="",
        usage=Usage(cost_usd=0.8),
    )
    settings = _settings(tmp_path, max_chain_cost_usd=0.5)
    _, _, _, dispatcher = await _dispatcher(tmp_path, settings, {"codex": asked})
    record = await dispatcher.submit(
        DispatchRequest(task="fix", cwd=str(git_repo), executor="codex")
    )
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.status == TaskStatus.failed
    assert done.result.error == (
        "escalation exhausted: needs_escalation; chain cost $0.80 reached max_chain_cost_usd"
    )


@pytest.mark.asyncio
async def test_chain_cost_counts_subtasks_of_the_link(tmp_path, git_repo):
    settings = _settings(tmp_path, max_chain_cost_usd=1.0)
    storage, adapters, _, dispatcher = await _dispatcher(
        tmp_path, settings, {"codex": _failed(0.2)}
    )

    async def spawn_priced_child(ctx):
        await storage.insert_task(_record("sub", 0.9, parent=ctx.task_id))

    adapters["codex"].on_execute = spawn_priced_child
    record = await dispatcher.submit(
        DispatchRequest(task="fix", cwd=str(git_repo), executor="codex")
    )
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.status == TaskStatus.failed
    assert done.result.meta["budget"]["chain_cost_usd"] == pytest.approx(1.1)
    assert not adapters["claude"].calls


@pytest.mark.asyncio
async def test_chain_under_the_cap_escalates_as_before(tmp_path, git_repo):
    settings = _settings(tmp_path, max_chain_cost_usd=5.0)
    _, adapters, _, dispatcher = await _dispatcher(tmp_path, settings, {"codex": _failed(0.8)})
    record = await dispatcher.submit(
        DispatchRequest(task="fix", cwd=str(git_repo), executor="codex")
    )
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id != record.task_id and done.status == TaskStatus.completed
    assert adapters["claude"].calls


@pytest.mark.asyncio
async def test_daily_limit_refuses_new_tasks_before_routing(tmp_path, git_repo):
    settings = _settings(tmp_path, daily_cost_limit_usd=1.0)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings, {})
    await storage.insert_task(_record("earlier", 1.2))
    record = await dispatcher.submit(DispatchRequest(task="fix", cwd=str(git_repo)))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.status == TaskStatus.failed
    assert done.result.meta["guard"] == GuardReason.budget.value
    assert done.result.error.startswith("daily cost limit reached: $1.20 of daily_cost_limit_usd")
    assert router.calls == 0
    assert not adapters["codex"].calls and not adapters["claude"].calls


@pytest.mark.asyncio
async def test_daily_limit_ignores_yesterday(tmp_path, git_repo):
    settings = _settings(tmp_path, daily_cost_limit_usd=1.0)
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, settings, {})
    await storage.insert_task(_record("old", 5.0, finished=datetime.now(UTC) - timedelta(days=2)))
    record = await dispatcher.submit(
        DispatchRequest(task="fix", cwd=str(git_repo), executor="codex")
    )
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.status == TaskStatus.completed
    assert adapters["codex"].calls


def test_budgets_are_off_by_default(tmp_config_dir):
    routing = load_settings().routing
    assert routing.max_chain_cost_usd is None and routing.daily_cost_limit_usd is None


@pytest.mark.parametrize("field", ["max_chain_cost_usd", "daily_cost_limit_usd"])
def test_budget_must_be_positive(field):
    with pytest.raises(ValueError):
        RoutingSettings(**{field: 0})
