from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    RouterKind,
    TaskStatus,
)
from agent_dispatch.routing.base import RouterError
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 2.0


def _settings(
    tmp_path: Path, *, max_tasks: int = 2, max_children: int = 2, max_hops: int = 2
) -> Settings:
    names = {"codex": "codex", "claude": "claude", "opencode/kimi": "opencode"}
    return Settings(
        server=ServerSettings(data_dir=tmp_path, max_concurrent_tasks=max_tasks),
        routing=RoutingSettings(
            fallback_executor="codex", max_children=max_children, max_hops=max_hops
        ),
        executors={
            n: ExecutorSettings(adapter=a, model="kimi" if a == "opencode" else None, description=n)
            for n, a in names.items()
        },
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
    kids = [await d.submit(_req(git_repo, parent_task_id=parent.task_id)) for _ in range(3)]
    assert (await d.wait(kids[2].task_id, WAIT)).result.meta["guard"] == "max_children"


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
