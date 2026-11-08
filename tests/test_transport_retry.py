"""Ограниченный повтор транспорта в `Dispatcher._execute` и его связь с эскалацией.

Повтор допускается только для адаптера, который помечает обрыв до первого ответа
(`reports_transport_before_output`), и только при неизменённой рабочей копии.
Эскалация подавляется, лишь когда такой повтор действительно был.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors import workspace
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 5.0


def _transport(executor: str) -> ExecutionResult:
    return ExecutionResult(
        status="failed",
        executor=executor,
        model=None,
        summary="",
        error="omp: no model response",
        meta={"transport_before_output": True},
    )


def _ok(executor: str) -> ExecutionResult:
    return ExecutionResult(status="completed", executor=executor, model=None, summary="ok")


class SequenceAdapter(FakeAdapter):
    """Отдаёт результаты по очереди, последний повторяется."""

    def __init__(self, name: str, results: list[ExecutionResult], *, transport_flag: bool):
        super().__init__(name)
        self.results = results
        self.reports_transport_before_output = transport_flag

    async def execute(self, ctx):
        self.calls.append(ctx)
        index = min(len(self.calls), len(self.results)) - 1
        return self.results[index].model_copy(deep=True)


async def _make(
    tmp_path: Path,
    first: list[ExecutionResult],
    *,
    attempts: int = 1,
    transport_flag: bool = True,
):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="omp/kimi"),
        execution=ExecutionSettings(
            transport_retry_attempts=attempts,
            transport_retry_backoff_seconds=0,
        ),
        executors={
            "omp/kimi": ExecutorSettings(adapter="omp", model="kimi", description="kimi"),
            "claude": ExecutorSettings(adapter="claude", description="claude"),
        },
        escalation={"omp/kimi": ["claude"]},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        "omp/kimi": SequenceAdapter("omp/kimi", first, transport_flag=transport_flag),
        "claude": FakeAdapter("claude", result=_ok("claude")),
    }
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return storage, adapters, dispatcher


async def _run(dispatcher: Dispatcher, storage: Storage, repo: Path):
    first = await dispatcher.submit(
        DispatchRequest(task="fix tests", cwd=str(repo), executor="omp/kimi")
    )
    parent = await dispatcher.wait(first.task_id, WAIT)
    async with asyncio.timeout(WAIT):
        while True:
            records = await storage.list_tasks(limit=20)
            if all(record.status not in ("queued", "running") for record in records):
                break
            await asyncio.sleep(0.01)
    parent = next(record for record in records if record.task_id == first.task_id)
    children = [record for record in records if record.escalated_from == first.task_id]
    return parent, children


@pytest.mark.asyncio
async def test_transport_failure_before_output_is_retried_once_and_succeeds(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path, [_transport("omp/kimi"), _ok("omp/kimi")])
    parent, children = await _run(dispatcher, storage, git_repo)

    assert parent.status == "completed" and not children
    assert len(adapters["omp/kimi"].calls) == 2
    assert parent.result.meta["retry_count"] == 1
    events = await storage.list_events(parent.task_id)
    assert [e["payload"]["attempt"] for e in events if e["kind"] == "retry"] == [1]


@pytest.mark.asyncio
async def test_repeated_transport_failure_after_retry_suppresses_escalation(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path, [_transport("omp/kimi")])
    parent, children = await _run(dispatcher, storage, git_repo)

    assert parent.status == "failed" and not children
    assert len(adapters["omp/kimi"].calls) == 2
    assert parent.result.meta["retry_count"] == 1
    assert parent.result.meta["escalation_suppressed"] == "transport_error"
    assert not adapters["claude"].calls


@pytest.mark.asyncio
async def test_transport_failure_without_retry_still_escalates(tmp_path, git_repo):
    # Повтор выключен: подавлять эскалацию нечем оправдать, задача идёт дальше.
    storage, adapters, dispatcher = await _make(tmp_path, [_transport("omp/kimi")], attempts=0)
    parent, children = await _run(dispatcher, storage, git_repo)

    assert parent.status == "failed"
    assert "escalation_suppressed" not in parent.result.meta
    assert len(adapters["omp/kimi"].calls) == 1
    assert [child.status for child in children] == ["completed"]
    assert adapters["claude"].calls


@pytest.mark.asyncio
async def test_transport_failure_after_workspace_change_escalates(tmp_path, git_repo):
    # Копия изменилась после запуска: повтор мог бы лечь поверх скрытой работы.
    class Dirtying(SequenceAdapter):
        async def execute(self, ctx):
            (Path(ctx.cwd) / "a.py").write_text("changed\n")
            return await super().execute(ctx)

    storage, adapters, dispatcher = await _make(tmp_path, [_transport("omp/kimi")])
    adapters["omp/kimi"] = Dirtying("omp/kimi", [_transport("omp/kimi")], transport_flag=True)
    dispatcher.adapters = adapters
    parent, children = await _run(dispatcher, storage, git_repo)

    assert len(adapters["omp/kimi"].calls) == 1
    assert "escalation_suppressed" not in parent.result.meta
    assert children


@pytest.mark.asyncio
async def test_adapter_without_transport_flag_skips_fingerprint_and_retry(
    tmp_path, git_repo, monkeypatch
):
    calls = []
    real = workspace.fingerprint

    def counting(cwd):
        calls.append(cwd)
        return real(cwd)

    monkeypatch.setattr(workspace, "fingerprint", counting)
    storage, adapters, dispatcher = await _make(
        tmp_path, [_transport("omp/kimi")], transport_flag=False
    )
    parent, children = await _run(dispatcher, storage, git_repo)

    assert not calls
    assert len(adapters["omp/kimi"].calls) == 1
    assert "escalation_suppressed" not in parent.result.meta
    assert children
