from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.dispatch.escalation import next_executor, should_escalate
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    GuardReason,
    RouterKind,
)
from agent_dispatch.models import TestsInfo as ResultTestsInfo
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 2.0


def _settings(tmp_path: Path, *, disabled: set[str] | None = None) -> Settings:
    disabled = disabled or set()
    names = {"codex": "codex", "claude": "claude", "opencode/kimi": "opencode"}
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="codex"),
        executors={
            name: ExecutorSettings(
                adapter=adapter,
                model="kimi" if adapter == "opencode" else None,
                description=name,
                enabled=name not in disabled,
            )
            for name, adapter in names.items()
        },
        escalation={
            "opencode/kimi": ["codex", "claude"],
            "codex": ["claude"],
        },
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(
            ExecutionResult(status="failed", executor="codex", model=None, summary="failed"),
            "failed",
            id="failed-status",
        ),
        pytest.param(
            ExecutionResult(
                status="completed",
                executor="codex",
                model=None,
                summary="retry elsewhere",
                needs_escalation=True,
            ),
            "needs_escalation",
            id="explicit-request",
        ),
        pytest.param(
            ExecutionResult(
                status="needs_escalation", executor="codex", model=None, summary="retry elsewhere"
            ),
            "needs_escalation",
            id="needs-escalation-status",
        ),
        pytest.param(
            ExecutionResult(
                status="completed",
                executor="codex",
                model=None,
                summary="tests failed",
                tests=ResultTestsInfo(result="failed"),
            ),
            "tests_failed",
            id="failed-tests",
        ),
        pytest.param(
            ExecutionResult(status="completed", executor="codex", model=None, summary="ok"),
            None,
            id="completed",
        ),
        pytest.param(
            # Исполнитель не отчитался и ничего не тронул: работы нет, и без
            # эскалации задача застревает навсегда.
            ExecutionResult(
                status="partial",
                executor="codex",
                model=None,
                summary="raw stdout tail",
                changed_files=[],
                meta={"parse_error": "no result block"},
            ),
            "no_result",
            id="partial-without-report-or-changes",
        ),
        pytest.param(
            # Отчёт не разобрался, но работа есть: судить о ней должен вызывающий
            # по дифу, повторный запуск лёг бы поверх.
            ExecutionResult(
                status="partial",
                executor="codex",
                model=None,
                summary="raw stdout tail",
                changed_files=["src/a.py", "tests/test_a.py"],
                meta={"parse_error": "no result block"},
            ),
            None,
            id="partial-without-report-but-with-changes",
        ),
        pytest.param(
            # Честный самоотчёт «сделал частично» это не потеря результата.
            ExecutionResult(
                status="partial", executor="codex", model=None, summary="did half of it"
            ),
            None,
            id="partial-self-reported",
        ),
    ],
)
def test_should_escalate_returns_expected_reason(result, expected):
    assert should_escalate(result) == expected


@pytest.mark.parametrize(
    ("current", "tried", "disabled", "expected"),
    [
        pytest.param("opencode/kimi", ["opencode/kimi"], set(), "codex", id="first"),
        pytest.param("opencode/kimi", ["opencode/kimi", "codex"], set(), "claude", id="skip-tried"),
        pytest.param("opencode/kimi", ["opencode/kimi"], {"codex"}, "claude", id="skip-disabled"),
        pytest.param("claude", ["claude"], set(), None, id="no-chain"),
        pytest.param(
            "opencode/kimi",
            ["opencode/kimi", "codex", "claude"],
            set(),
            None,
            id="exhausted",
        ),
    ],
)
def test_next_executor_returns_first_enabled_untried(tmp_path, current, tried, disabled, expected):
    assert next_executor(current, _settings(tmp_path, disabled=disabled), tried) == expected


def test_next_executor_skips_missing_configuration(tmp_path):
    settings = _settings(tmp_path)
    settings.escalation["codex"] = ["missing", "claude"]
    assert next_executor("codex", settings, ["codex"]) == "claude"


def _result(
    executor: str,
    status: str = "completed",
    *,
    needs_escalation: bool = False,
    tests_failed: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        executor=executor,
        model=None,
        summary=status,
        needs_escalation=needs_escalation,
        tests=ResultTestsInfo(result="failed") if tests_failed else None,
    )


async def _make_dispatcher(
    tmp_path: Path,
    *,
    results: dict[str, ExecutionResult] | None = None,
    disabled: set[str] | None = None,
    escalation: dict[str, list[str]] | None = None,
):
    settings = _settings(tmp_path, disabled=disabled)
    if escalation is not None:
        settings.escalation = escalation
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name)) for name in settings.executors
    }
    router = FakeRouter()
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    return storage, adapters, router, dispatcher


def _request(repo: Path, **updates) -> DispatchRequest:
    return DispatchRequest(
        task="fix tests", cwd=str(repo), executor="opencode/kimi", hop=0, **updates
    )


async def _wait_for_records(dispatcher: Dispatcher, storage: Storage, count: int):
    async with asyncio.timeout(WAIT):
        while True:
            records = await storage.list_tasks(limit=20)
            if len(records) >= count:
                for record in records:
                    await dispatcher.wait(record.task_id, WAIT)
                return await storage.list_tasks(limit=20)
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_failed_result_escalates_to_next_executor(tmp_path, git_repo):
    storage, adapters, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "opencode/kimi": _result("opencode/kimi", "failed"),
            "codex": _result("codex"),
        },
    )
    first = await dispatcher.submit(_request(git_repo))
    records = await _wait_for_records(dispatcher, storage, 2)
    parent = next(record for record in records if record.task_id == first.task_id)
    child = next(record for record in records if record.escalated_from == first.task_id)

    assert parent.status == "failed" and child.status == "completed"
    assert child.parent_task_id == parent.parent_task_id and child.hop == parent.hop
    assert child.decision.reason == GuardReason.escalated
    assert child.decision.router == RouterKind.fallback
    assert adapters["codex"].calls
    events = await storage.list_events(parent.task_id)
    assert any(
        event["kind"] == "escalate"
        and event["payload"] == {"from": "opencode/kimi", "to": "codex", "reason": "failed"}
        for event in events
    )


@pytest.mark.asyncio
async def test_completed_result_requesting_escalation_escalates(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "opencode/kimi": _result("opencode/kimi", needs_escalation=True),
            "codex": _result("codex"),
        },
    )
    await dispatcher.submit(_request(git_repo))

    records = await _wait_for_records(dispatcher, storage, 2)
    assert {record.decision.executor for record in records} == {"opencode/kimi", "codex"}


@pytest.mark.asyncio
async def test_failed_tests_escalate_completed_result(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "opencode/kimi": _result("opencode/kimi", tests_failed=True),
            "codex": _result("codex"),
        },
    )
    await dispatcher.submit(_request(git_repo))

    records = await _wait_for_records(dispatcher, storage, 2)
    assert any(record.escalated_from is not None for record in records)


@pytest.mark.asyncio
async def test_disallowed_escalation_keeps_single_record_and_no_event(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path, results={"opencode/kimi": _result("opencode/kimi", "failed")}
    )
    first = await dispatcher.submit(_request(git_repo, allow_escalation=False))
    done = await dispatcher.wait(first.task_id, WAIT)

    assert done.status == "failed"
    assert len(await storage.list_tasks(limit=20)) == 1
    assert not any(
        event["kind"] == "escalate" for event in await storage.list_events(first.task_id)
    )


@pytest.mark.asyncio
async def test_exhausted_chain_records_full_chain_on_final_failure(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={name: _result(name, "failed") for name in _settings(tmp_path).executors},
    )
    await dispatcher.submit(_request(git_repo))

    records = await _wait_for_records(dispatcher, storage, 3)
    final = next(record for record in records if record.decision.executor == "claude")
    assert len(records) == 3 and final.status == "failed"
    assert final.result.meta["escalation_chain"] == ["opencode/kimi", "codex", "claude"]


@pytest.mark.asyncio
async def test_executor_without_chain_does_not_create_child(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path, results={"claude": _result("claude", "failed")}
    )
    task = await dispatcher.submit(_request(git_repo).model_copy(update={"executor": "claude"}))
    await dispatcher.wait(task.task_id, WAIT)

    assert len(await storage.list_tasks(limit=20)) == 1
    assert not any(event["kind"] == "escalate" for event in await storage.list_events(task.task_id))


@pytest.mark.asyncio
async def test_escalation_children_do_not_call_router(tmp_path, git_repo):
    storage, _, router, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "opencode/kimi": _result("opencode/kimi", "failed"),
            "codex": _result("codex", "failed"),
            "claude": _result("claude"),
        },
    )
    await dispatcher.submit(_request(git_repo))

    await _wait_for_records(dispatcher, storage, 3)
    assert router.calls == 0


@pytest.mark.asyncio
async def test_escalation_cycle_does_not_retry_ancestor(tmp_path, git_repo):
    storage, adapters, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "codex": _result("codex", "failed"),
            "claude": _result("claude", "failed"),
        },
        escalation={"codex": ["claude"], "claude": ["codex"]},
    )
    task = await dispatcher.submit(_request(git_repo).model_copy(update={"executor": "codex"}))
    await dispatcher.wait(task.task_id, WAIT)

    records = await _wait_for_records(dispatcher, storage, 2)
    assert len(records) == 2
    assert len(adapters["codex"].calls) == 1 and len(adapters["claude"].calls) == 1


@pytest.mark.asyncio
async def test_disabled_executor_in_chain_is_skipped(tmp_path, git_repo):
    storage, adapters, _, dispatcher = await _make_dispatcher(
        tmp_path,
        disabled={"codex"},
        results={
            "opencode/kimi": _result("opencode/kimi", "failed"),
            "claude": _result("claude"),
        },
    )
    await dispatcher.submit(_request(git_repo))

    records = await _wait_for_records(dispatcher, storage, 2)
    child = next(record for record in records if record.escalated_from is not None)
    assert child.decision.executor == "claude"
    assert not adapters["codex"].calls


@pytest.mark.asyncio
async def test_wait_follows_escalation_chain_and_returns_child(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={
            "opencode/kimi": _result("opencode/kimi", "failed"),
            "codex": _result("codex"),
        },
    )
    parent = await dispatcher.submit(_request(git_repo))
    done = await dispatcher.wait(parent.task_id, WAIT)
    assert done.decision.executor == "codex"
    assert done.escalated_from == parent.task_id
    assert done.status == "completed"


@pytest.mark.asyncio
async def test_wait_timeout_returns_running_escalated_child(tmp_path, git_repo):
    storage, adapters, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={"opencode/kimi": _result("opencode/kimi", "failed"), "codex": _result("codex")},
    )
    gate = asyncio.Event()
    adapters["codex"].gate = gate
    parent = await dispatcher.submit(_request(git_repo))
    done = await dispatcher.wait(parent.task_id, 0.05)
    assert done.escalated_from == parent.task_id
    assert done.status in {"queued", "running"}
    gate.set()
    await dispatcher.wait(done.task_id, WAIT)


@pytest.mark.asyncio
async def test_escalation_children_do_not_consume_max_children_quota(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path,
        results={name: _result(name, "failed") for name in _settings(tmp_path).executors},
    )
    seed = await dispatcher.submit(_request(git_repo).model_copy(update={"executor": "claude"}))
    await dispatcher.wait(seed.task_id, WAIT)
    delegated = await dispatcher.submit(_request(git_repo, parent_task_id=seed.task_id))
    records = await _wait_for_records(dispatcher, storage, 4)
    chain = [
        record for record in records if record.task_id == delegated.task_id or record.escalated_from
    ]
    final = next(
        record
        for record in records
        if record.decision and record.decision.executor == "claude" and record.escalated_from
    )
    assert len(chain) >= 3
    assert final.result.meta["escalation_chain"] == ["opencode/kimi", "codex", "claude"]


@pytest.mark.asyncio
async def test_exhausted_escalation_marks_completed_result_failed(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path, results={"claude": _result("claude", needs_escalation=True)}
    )
    task = await dispatcher.submit(_request(git_repo).model_copy(update={"executor": "claude"}))
    done = await dispatcher.wait(task.task_id, WAIT)
    assert done.status == "failed"
    assert done.result.error == "escalation exhausted: needs_escalation"


@pytest.mark.asyncio
async def test_wait_zero_returns_parent_not_escalation_child(tmp_path, git_repo):
    storage, _, _, dispatcher = await _make_dispatcher(
        tmp_path, results={"opencode/kimi": _result("opencode/kimi", status="failed")}
    )
    parent = await dispatcher.submit(_request(git_repo))
    final = await dispatcher.wait(parent.task_id, WAIT)
    assert final.escalated_from == parent.task_id
    # Нулевое ожидание отдаёт саму задачу, даже когда ребёнок эскалации уже есть.
    zero = await dispatcher.wait(parent.task_id, 0)
    assert zero.task_id == parent.task_id
    await storage.close()
