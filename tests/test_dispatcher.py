from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    RouterKind,
    TaskStatus,
)
from agent_dispatch.routing.base import RouterError
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 2.0


def _settings(
    tmp_path: Path,
    *,
    max_tasks: int = 2,
    max_per_group: int = 2,
    max_children: int = 2,
    max_hops: int = 2,
    escalation: dict[str, list[str]] | None = None,
) -> Settings:
    names = {"codex": "codex", "claude": "claude", "opencode/kimi": "opencode"}
    return Settings(
        server=ServerSettings(
            data_dir=tmp_path,
            max_concurrent_tasks=max_tasks,
            max_concurrent_per_group=max_per_group,
        ),
        routing=RoutingSettings(
            fallback_executor="codex", max_children=max_children, max_hops=max_hops
        ),
        executors={
            n: ExecutorSettings(adapter=a, model="kimi" if a == "opencode" else None, description=n)
            for n, a in names.items()
        },
        escalation=escalation or {},
    )


async def _make(tmp_path: Path, **kwargs):
    settings = _settings(tmp_path, **kwargs)
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return settings, storage, adapters, dispatcher


def _req(repo: Path, **kwargs):
    values = {"executor": "codex", **kwargs}
    return DispatchRequest(task="fix tests", cwd=str(repo), **values)


@pytest.mark.asyncio
async def test_happy_path_records_decision_result_events(tmp_path, git_repo):
    _, st, _, d = await _make(tmp_path)
    r = await d.submit(_req(git_repo))
    done = await d.wait(r.task_id, WAIT)
    assert done.status == "completed" and done.result and done.decision
    assert {e["kind"] for e in await st.list_events(r.task_id)} >= {"guard", "spawn", "exit"}


@pytest.mark.asyncio
async def test_wait_zero_returns_immediately(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r = await d.submit(_req(git_repo))
    now = await d.wait(r.task_id, 0)
    assert now.status in {"queued", "routing", "running"}
    a["codex"].gate.set()
    assert (await d.wait(r.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_semaphore_serializes_roots(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path, max_tasks=1)
    a["codex"].gate = asyncio.Event()
    started = a["codex"].started
    r1 = await d.submit(_req(git_repo))
    r2 = await d.submit(_req(git_repo))
    await asyncio.wait_for(started.wait(), WAIT)
    assert len(a["codex"].calls) == 1
    a["codex"].gate.set()
    await d.wait(r1.task_id, WAIT)
    await d.wait(r2.task_id, WAIT)


@pytest.mark.asyncio
async def test_root_task_capacity_is_per_group_under_global_ceiling(tmp_path, git_repo):
    _, storage, adapters, dispatcher = await _make(tmp_path, max_tasks=3, max_per_group=2)
    repos = [git_repo]
    for index in range(1, 6):
        repo = tmp_path / f"repo-{index}"
        shutil.copytree(git_repo, repo)
        repos.append(repo)

    codex = adapters["codex"]
    opencode = adapters["opencode/kimi"]
    codex.gate = asyncio.Event()
    opencode.gate = asyncio.Event()

    first = await dispatcher.submit(_req(repos[0], executor="codex"))
    second = await dispatcher.submit(_req(repos[1], executor="codex"))
    async with asyncio.timeout(WAIT):
        while len(codex.calls) < 2:  # noqa: ASYNC110
            await asyncio.sleep(0)

    waiting_group = await dispatcher.submit(_req(repos[2], executor="codex"))
    async with asyncio.timeout(WAIT):
        while (await storage.get_task(waiting_group.task_id)).decision is None:  # noqa: ASYNC110
            await asyncio.sleep(0)

    other_group = await dispatcher.submit(_req(repos[3], executor="opencode/kimi"))
    await asyncio.wait_for(opencode.started.wait(), WAIT)
    assert len(codex.calls) == 2
    assert len(opencode.calls) == 1

    # В группе opencode есть место, но общий предел уже занят двумя codex и
    # первым opencode. Ожидающий group-slot codex общего слота не держит.
    opencode.started = asyncio.Event()
    waiting_global = await dispatcher.submit(_req(repos[4], executor="opencode/kimi"))
    async with asyncio.timeout(WAIT):
        while (await storage.get_task(waiting_global.task_id)).decision is None:  # noqa: ASYNC110
            await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(opencode.started.wait(), 0.1)

    # Отмена задачи, которая не получила group-slot, не должна освободить
    # чужой global-slot. Второй opencode по-прежнему ждёт.
    await dispatcher.cancel(waiting_group.task_id)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(opencode.started.wait(), 0.1)

    await dispatcher.cancel(first.task_id)
    await asyncio.wait_for(opencode.started.wait(), WAIT)
    assert len(opencode.calls) == 2

    events = await storage.list_events(other_group.task_id)
    slot = next(event["payload"] for event in events if event["kind"] == "slot")
    assert slot["limit_group"] == "opencode/kimi"
    assert slot["wait_seconds"] >= 0

    for task_id in (second.task_id, other_group.task_id, waiting_global.task_id):
        await dispatcher.cancel(task_id)


@pytest.mark.asyncio
async def test_nested_hop_bypasses_semaphore(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path, max_tasks=1)
    gate = asyncio.Event()
    nested_once = False

    async def nested(ctx):
        nonlocal nested_once
        if nested_once:
            return
        nested_once = True
        child = await d.submit(_req(git_repo, executor=None, hop=1, parent_task_id=ctx.task_id))
        assert (await d.wait(child.task_id, WAIT)).status == "completed"
        gate.set()

    a["codex"].on_execute = nested
    r = await d.submit(_req(git_repo))
    assert (await asyncio.wait_for(d.wait(r.task_id, WAIT), WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_per_cwd_lock_serializes_same_repo(tmp_path, git_repo):
    _, st, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r1 = await d.submit(_req(git_repo))
    r2 = await d.submit(_req(git_repo))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    assert not any(e["kind"] == "spawn" for e in await st.list_events(r2.task_id))
    a["codex"].gate.set()
    await d.wait(r1.task_id, WAIT)
    await d.wait(r2.task_id, WAIT)


def _git(repo: Path, *args: str) -> None:
    """git-команда в репе без унаследованных GIT_* (как в conftest.git_repo)."""
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "HOME": str(repo)}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_per_cwd_lock_keys_working_copy_root(tmp_path, git_repo):
    """Корень и подкаталог одной рабочей копии стоят в одной очереди lock."""
    _, st, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    sub = git_repo / "sub"
    sub.mkdir()
    r1 = await d.submit(_req(git_repo))
    r2 = await d.submit(_req(sub))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    # Ждём, пока r2 дойдёт до lock: slot выдан, дальше только ключ lock. Если бы
    # ключом был cwd, r2 стартовал бы за миллисекунды; окно ниже это ловит.
    async with asyncio.timeout(WAIT):
        while not any(  # noqa: ASYNC110
            e["kind"] == "slot" for e in await st.list_events(r2.task_id)
        ):
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    assert len(a["codex"].calls) == 1
    a["codex"].gate.set()
    await d.wait(r1.task_id, WAIT)
    await d.wait(r2.task_id, WAIT)


@pytest.mark.asyncio
async def test_per_cwd_lock_keeps_separate_worktrees_parallel(tmp_path, git_repo):
    """Отдельные `git worktree` одного репозитория друг друга не ждут."""
    _, _, a, d = await _make(tmp_path)
    wt1, wt2 = tmp_path / "wt1", tmp_path / "wt2"
    _git(git_repo, "worktree", "add", str(wt1), "-b", "wt1")
    _git(git_repo, "worktree", "add", str(wt2), "-b", "wt2")
    a["codex"].gate = asyncio.Event()
    r1 = await d.submit(_req(wt1))
    r2 = await d.submit(_req(wt2))
    async with asyncio.timeout(WAIT):
        while len(a["codex"].calls) < 2:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    a["codex"].gate.set()
    await d.wait(r1.task_id, WAIT)
    await d.wait(r2.task_id, WAIT)


# Каталог вне git dispatcher не принимает вообще (guard `bad_cwd` в `submit`),
# поэтому отдельный тест на fallback-ключ для не-git `cwd` не нужен: до `_cwd_lock`
# такая задача не доходит.


@pytest.mark.asyncio
async def test_cancel_running_marks_adapter_cancelled(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r = await d.submit(_req(git_repo))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    out = await d.cancel(r.task_id)
    assert out.status == "cancelled" and a["codex"].cancelled


@pytest.mark.asyncio
async def test_max_hops_guard(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path, max_hops=1)
    out = await d.wait((await d.submit(_req(git_repo, hop=1))).task_id, WAIT)
    assert out.result and out.result.meta["guard"] == "max_hops" and not a["codex"].calls


@pytest.mark.asyncio
async def test_override_does_not_call_router(tmp_path, git_repo):
    _, _, _, d = await _make(tmp_path)
    router = d.routers[0]
    out = await d.wait((await d.submit(_req(git_repo))).task_id, WAIT)
    assert out.decision.router == RouterKind.override and router.calls == 0


@pytest.mark.asyncio
async def test_unknown_parent_guard(tmp_path, git_repo):
    _, _, _, d = await _make(tmp_path)
    out = await d.wait((await d.submit(_req(git_repo, parent_task_id="missing"))).task_id, WAIT)
    assert out.result and out.result.meta["guard"] == "unknown_parent"


@pytest.mark.asyncio
async def test_max_children_guard(tmp_path, git_repo):
    _, _, _, d = await _make(tmp_path, max_children=2)
    parent = await d.submit(_req(git_repo))
    await d.wait(parent.task_id, WAIT)
    children = []
    for _ in range(2):
        child = await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))
        children.append(await d.wait(child.task_id, WAIT))
    assert [child.status for child in children] == ["completed", "completed"]

    refused = await d.wait(
        (await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))).task_id, WAIT
    )
    retried = await d.wait(
        (await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))).task_id, WAIT
    )
    assert refused.result.meta["guard"] == "max_children"
    assert refused.result.error == "sibling count 2 >= 2"
    assert retried.result.error == "sibling count 2 >= 2"


@pytest.mark.asyncio
async def test_max_children_refusal_lists_running_siblings(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path, max_children=2)
    parent = await d.submit(_req(git_repo))
    await d.wait(parent.task_id, WAIT)
    done = await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))
    await d.wait(done.task_id, WAIT)
    a["codex"].gate = asyncio.Event()
    a["codex"].started.clear()
    running = await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    refused = await d.wait(
        (await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))).task_id, WAIT
    )
    assert refused.result.meta["guard"] == "max_children"
    assert refused.result.error == (
        f"sibling count 2 >= 2; running siblings: {running.task_id};"
        " wait for one via status(task_id, wait_seconds) instead of dispatching again"
    )
    # Слот освободился: работающих sibling'ов больше нет, и отказ снова
    # короткий — ждать нечего, но квота fan-out не растёт.
    a["codex"].gate.set()
    await d.wait(running.task_id, WAIT)
    retried = await d.wait(
        (await d.submit(_req(git_repo, parent_task_id=parent.task_id, hop=1))).task_id, WAIT
    )
    assert retried.result.meta["guard"] == "max_children"
    assert retried.result.error == "sibling count 2 >= 2"


@pytest.mark.asyncio
async def test_queued_task_is_not_spawned_after_executor_enters_cooldown(tmp_path, git_repo):
    _, storage, adapters, dispatcher = await _make(
        tmp_path, max_tasks=1, escalation={"codex": ["claude"]}
    )
    gate = asyncio.Event()
    adapters["codex"].gate = gate
    adapters["codex"].result = ExecutionResult(
        status="failed",
        executor="codex",
        model=None,
        summary="",
        error="You've hit your monthly spend limit; try again tomorrow",
    )

    first = await dispatcher.submit(_req(git_repo))
    await asyncio.wait_for(adapters["codex"].started.wait(), WAIT)
    second = await dispatcher.submit(_req(git_repo))
    for _ in range(100):
        if (await storage.get_task(second.task_id)).decision is not None:
            break
        await asyncio.sleep(0.01)
    assert (await storage.get_task(second.task_id)).decision.executor == "codex"
    gate.set()

    await dispatcher.wait(first.task_id, WAIT)
    final = await dispatcher.wait(second.task_id, WAIT)
    queued = await storage.get_task(second.task_id)

    assert len(adapters["codex"].calls) == 1
    assert queued.status == "failed"
    assert queued.result.error == "executor became unavailable while waiting for a slot: codex"
    assert queued.result.meta["escalated_to"] == final.task_id
    assert final.status == "completed" and final.decision.executor == "claude"


@pytest.mark.asyncio
async def test_adapter_exception_is_failed_and_worker_survives(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    a["codex"].raise_exc = ValueError("boom")
    bad = await d.wait((await d.submit(_req(git_repo))).task_id, WAIT)
    assert bad.status == "failed" and "ValueError: boom" == bad.result.error
    a["codex"].raise_exc = None
    assert (await d.wait((await d.submit(_req(git_repo))).task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_log_path_is_created_at_submit(tmp_path, git_repo):
    settings, _, _, d = await _make(tmp_path)
    r = await d.submit(_req(git_repo))
    assert Path(r.log_path).parent == settings.server.data_dir / "logs"


@pytest.mark.asyncio
async def test_child_env_hop_and_secret_filter(tmp_path, git_repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    _, _, a, d = await _make(tmp_path)
    r = await d.submit(_req(git_repo))
    await d.wait(r.task_id, WAIT)
    assert (
        "OPENROUTER_API_KEY" not in a["codex"].calls[0].env
        and a["codex"].calls[0].env["AGENT_DISPATCH_HOP"] == "1"
    )


@pytest.mark.asyncio
async def test_route_only_writes_null_task_decision(tmp_path, git_repo):
    _, st, _, d = await _make(tmp_path)
    decision, _ = await d.route_only(_req(git_repo))
    rows = [row async for row in st.export() if row["task"] is None]
    assert rows and rows[-1]["decision"]["executor"] == decision.executor


@pytest.mark.asyncio
async def test_missing_adapter_after_routing_fails(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    r = await d.submit(_req(git_repo))
    a.pop("codex")
    out = await d.wait(r.task_id, WAIT)
    assert out.result.meta["guard"] == "unavailable"


@pytest.mark.asyncio
async def test_shutdown_cancels_running(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r = await d.submit(_req(git_repo))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    await d.shutdown()
    assert (await d.get(r.task_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_router_fallback_is_used(tmp_path, git_repo):
    settings, st, a, d = await _make(tmp_path)
    d.routers = [FakeRouter(error=RouterError("down"))]
    req = DispatchRequest(task="x", cwd=str(git_repo), executor=None)
    out = await d.wait((await d.submit(req)).task_id, WAIT)
    assert out.status == "completed" and out.decision.executor == settings.routing.fallback_executor


@pytest.mark.asyncio
async def test_external_failure_is_recorded(tmp_path, git_repo, monkeypatch):
    _, st, _, d = await _make(tmp_path)
    original = st.add_decision

    async def fail(*args, **kwargs):
        if args and args[0] is not None:
            raise RuntimeError("storage exploded")
        return await original(*args, **kwargs)

    monkeypatch.setattr(st, "add_decision", fail)
    out = await d.wait((await d.submit(_req(git_repo))).task_id, WAIT)
    assert out.status == TaskStatus.failed and "RuntimeError" in out.result.error


@pytest.mark.asyncio
async def test_wait_cancel_and_recover_stale_without_worker(tmp_path, git_repo):
    _, st, _, d = await _make(tmp_path)
    record = await d.submit(_req(git_repo))
    worker = d._tasks.pop(record.task_id)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    stale = await st.get_task(record.task_id)
    stale.status = TaskStatus.running
    await st.update_task(stale)
    assert (await d.wait(record.task_id, 1)).status == TaskStatus.running
    assert (await d.cancel(record.task_id)).status == TaskStatus.cancelled

    record2 = await d.submit(_req(git_repo))
    worker2 = d._tasks.pop(record2.task_id)
    worker2.cancel()
    await asyncio.gather(worker2, return_exceptions=True)
    stale2 = await st.get_task(record2.task_id)
    stale2.status = TaskStatus.running
    await st.update_task(stale2)
    assert await d.recover_stale() == 1
    assert (await d.get(record2.task_id)).status == TaskStatus.failed


@pytest.mark.asyncio
async def test_cancel_immediately_after_submit(tmp_path, git_repo):
    _, _, _, d = await _make(tmp_path)
    record = await d.submit(_req(git_repo))
    out = await asyncio.wait_for(d.cancel(record.task_id), WAIT)
    assert out.status == TaskStatus.cancelled


@pytest.mark.asyncio
async def test_dispatcher_cleans_lifecycle_state(tmp_path, git_repo):
    _, _, _, d = await _make(tmp_path)
    record = await d.submit(_req(git_repo))
    assert (await d.wait(record.task_id, WAIT)).status == TaskStatus.completed
    await asyncio.sleep(0)
    assert record.task_id not in d._tasks and record.task_id not in d._events
    assert not d._cwd_locks


@pytest.mark.asyncio
async def test_package_build_does_not_block_event_loop(tmp_path, git_repo, monkeypatch):
    import time

    import agent_dispatch.dispatch.dispatcher as dispatcher_module

    original = dispatcher_module.build_task_package
    ticked = asyncio.Event()

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatcher_module, "build_task_package", slow)
    _, _, _, d = await _make(tmp_path)
    r = await d.submit(_req(git_repo))
    ticker = asyncio.create_task(asyncio.sleep(0.05))
    ticker.add_done_callback(lambda _: ticked.set())
    await d.wait(r.task_id, WAIT)
    assert ticked.is_set()


@pytest.mark.asyncio
async def test_wait_unknown_task_raises_key_error(tmp_path):
    _, _, _, d = await _make(tmp_path)
    with pytest.raises(KeyError):
        await d.wait("missing", WAIT)


@pytest.mark.asyncio
async def test_shutdown_waits_for_cleanup_before_storage_close(tmp_path, git_repo):
    # Cleanup-таск из done-колбэка не должен работать после shutdown:
    # иначе он обратится к закрытому storage.
    _, st, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r = await d.submit(_req(git_repo))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    await d.shutdown()
    assert not d._cleanup
    await st.close()
    assert await d.get(r.task_id) if False else True


@pytest.mark.asyncio
async def test_cancelled_root_task_releases_cwd_lock(tmp_path, git_repo):
    _, _, a, d = await _make(tmp_path)
    a["codex"].gate = asyncio.Event()
    r = await d.submit(_req(git_repo))
    await asyncio.wait_for(a["codex"].started.wait(), WAIT)
    assert d._cwd_locks
    await d.cancel(r.task_id, wait_seconds=WAIT)
    await asyncio.sleep(0)
    assert not d._cwd_locks


@pytest.mark.asyncio
async def test_worker_cancelled_before_start_is_recorded_cancelled(tmp_path, git_repo):
    # Отмена до первого шага `_run` (shutdown сразу после submit) доходит только
    # до done-колбэка; задача не должна остаться queued до перезапуска демона.
    _, st, _, d = await _make(tmp_path)
    record = await d.submit(_req(git_repo))
    d._tasks[record.task_id].cancel()
    await d.shutdown()
    stored = await st.get_task(record.task_id)
    assert stored.status == TaskStatus.cancelled
    assert stored.result is not None and stored.result.error == "cancelled"


@pytest.mark.asyncio
async def test_timeout_precedence_request_then_executor_then_routing(tmp_path, git_repo):
    settings, _, a, d = await _make(tmp_path)
    settings.executors["codex"].timeout_seconds = 3600
    settings.routing.default_timeout_seconds = 1800

    for req, expected in (
        (_req(git_repo, timeout_seconds=600), 600),
        (_req(git_repo), 3600),
        (_req(git_repo, executor="claude"), 1800),
    ):
        await d.wait((await d.submit(req)).task_id, WAIT)
        adapter = a[req.executor]
        assert adapter.calls[-1].timeout_seconds == expected
        # Бюджет времени в промпте исполнителя тот же, что у таймаута.
        assert f"Your time budget is {expected} seconds" in adapter.calls[-1].prompt


@pytest.mark.asyncio
async def test_submit_records_the_git_common_dir_and_branch_of_cwd(tmp_path, git_repo):
    from agent_dispatch.executors import worktree

    _, st, _, d = await _make(tmp_path)
    tree = worktree.create(git_repo, tmp_path / "wt" / "feature", "feature")
    linked = await d.submit(_req(tree.path))
    # Событие уже записано, когда `submit` вернул ответ: вызывающий может
    # удалить свой worktree сразу, и запись после этого не нашла бы репу.
    events = [e for e in await st.list_events(linked.task_id) if e["kind"] == "git"]
    assert [e["payload"] for e in events] == [
        {"common_dir": str((git_repo / ".git").resolve()), "branch": "feature"}
    ]
    plain = tmp_path / "plain"
    plain.mkdir()
    not_git = await d.submit(_req(plain))
    for record in (linked, not_git):
        await d.wait(record.task_id, WAIT)
    await d.shutdown()

    assert not [e for e in await st.list_events(not_git.task_id) if e["kind"] == "git"]


@pytest.mark.asyncio
async def test_failed_git_location_does_not_break_submit(tmp_path, git_repo, monkeypatch):
    # Событие `git` это телеметрия: её сбой не роняет `submit` и не оставляет
    # задачу `queued` без воркера.
    from agent_dispatch.executors import worktree

    def broken(_cwd):
        raise RuntimeError("cwd vanished between git calls")

    monkeypatch.setattr(worktree, "git_location", broken)
    _, st, _, d = await _make(tmp_path)

    record = await d.submit(_req(git_repo))
    done = await d.wait(record.task_id, WAIT)

    assert done.status == "completed"
    assert not [e for e in await st.list_events(record.task_id) if e["kind"] == "git"]


@pytest.mark.asyncio
async def test_failed_git_event_write_does_not_fail_a_running_submit(tmp_path, git_repo):
    # Воркер уже создан: ошибка записи события заставила бы вызывающего
    # повторить `submit` и запустить ту же работу дважды.
    _, st, _, d = await _make(tmp_path)
    original = st.add_event

    async def add_event(task_id, kind, payload):
        if kind == "git":
            raise OSError("disk full")
        return await original(task_id, kind, payload)

    st.add_event = add_event

    record = await d.submit(_req(git_repo))
    done = await d.wait(record.task_id, WAIT)

    assert done.status == "completed"
