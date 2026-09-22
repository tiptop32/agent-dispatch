"""Режим worktree: исполнитель правит изолированную копию, результат возвращается патчем."""

from __future__ import annotations

import asyncio
import subprocess
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
from agent_dispatch.executors import worktree
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 5.0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


async def _make(tmp_path: Path, **execution):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path, max_concurrent_tasks=2),
        routing=RoutingSettings(fallback_executor="codex"),
        execution=ExecutionSettings(workspace_mode="worktree", **execution),
        executors={"codex": ExecutorSettings(adapter="codex", description="codex")},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {"codex": FakeAdapter("codex")}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return settings, storage, adapters, dispatcher


def _req(repo: Path, **kwargs):
    return DispatchRequest(task="fix tests", cwd=str(repo), executor="codex", **kwargs)


def _writes(text: str, name: str = "a.py"):
    async def on_execute(ctx):
        (Path(ctx.cwd) / name).write_text(text)

    return on_execute


@pytest.mark.asyncio
async def test_executor_runs_in_a_worktree_not_in_the_working_copy(tmp_path, git_repo):
    _, storage, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    ctx = adapters["codex"].calls[0]
    assert ctx.cwd != str(git_repo) and "worktrees" in ctx.cwd
    assert done.status == "completed"
    # Патч вернулся в рабочую копию.
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert done.result.meta["integrated"] is True
    kinds = {event["kind"] for event in await storage.list_events(record.task_id)}
    assert {"worktree", "integrate"} <= kinds


@pytest.mark.asyncio
async def test_worktree_is_removed_after_a_successful_run(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    await dispatcher.wait(record.task_id, WAIT)

    assert not Path(adapters["codex"].calls[0].cwd).exists()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_keep_worktrees_leaves_the_tree_for_inspection(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path, keep_worktrees=True)
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert Path(adapters["codex"].calls[0].cwd).exists()  # noqa: ASYNC240
    assert done.result.meta["worktree"] and done.result.meta["branch"]


@pytest.mark.asyncio
async def test_manual_integration_leaves_the_patch_and_does_not_touch_the_working_copy(
    tmp_path, git_repo
):
    _, _, adapters, dispatcher = await _make(tmp_path, integrate="manual")
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert (git_repo / "a.py").read_text() == "x = 1\n"
    assert done.result.meta["integrated"] is False
    assert done.result.meta["worktree"] and done.result.meta["branch"]
    # Патч обязан лежать на диске: режим просит отдать его человеку.
    assert Path(done.result.meta["patch"]).is_file()  # noqa: ASYNC240
    assert "a.py" in Path(done.result.meta["patch"]).read_text()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_result_is_committed_on_the_task_branch(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    sha = done.result.meta["commit"]
    assert len(sha) == 40
    assert _git(git_repo, "show", f"{sha}:a.py") == "x = 2\n"


@pytest.mark.asyncio
async def test_branch_mode_keeps_the_branch_and_leaves_the_working_copy_alone(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path, integrate="branch")
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    meta = done.result.meta
    assert meta["integrated"] is False
    assert (git_repo / "a.py").read_text() == "x = 1\n"
    # Каталог убран, результат остался коммитом на ветке.
    assert "worktree" not in meta
    assert _git(git_repo, "rev-parse", meta["branch"]).strip() == meta["commit"]
    assert _git(git_repo, "show", f"{meta['commit']}:a.py") == "x = 2\n"
    assert worktree.list_branches(git_repo, "agent-dispatch") == [meta["branch"]]


@pytest.mark.asyncio
async def test_empty_run_leaves_no_branch_behind(tmp_path, git_repo):
    _, _, _, dispatcher = await _make(tmp_path, integrate="branch")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.result.meta["integrated"] is True
    assert worktree.list_branches(git_repo, "agent-dispatch") == []


@pytest.mark.asyncio
async def test_conflicting_edit_keeps_the_worktree_and_reports_the_failure(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path)

    async def on_execute(ctx):
        (Path(ctx.cwd) / "a.py").write_text("from the worktree\n")
        (git_repo / "a.py").write_text("from the agent\n")

    adapters["codex"].on_execute = on_execute

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.result.meta["integrated"] is False
    assert done.result.meta["integration_error"]
    assert Path(done.result.meta["worktree"]).exists()  # noqa: ASYNC240
    assert Path(done.result.meta["patch"]).is_file()  # noqa: ASYNC240
    # Работа агента не затёрта.
    assert (git_repo / "a.py").read_text() == "from the agent\n"


@pytest.mark.asyncio
async def test_two_tasks_in_one_repository_do_not_wait_for_each_other(tmp_path, git_repo):
    _, storage, adapters, dispatcher = await _make(tmp_path)
    gate = asyncio.Event()
    running = asyncio.Semaphore(0)

    async def on_execute(ctx):
        running.release()
        await gate.wait()

    adapters["codex"].on_execute = on_execute

    first = await dispatcher.submit(_req(git_repo))
    second = await dispatcher.submit(_req(git_repo))
    # Обе задачи должны дойти до исполнителя, а не выстроиться в очередь по cwd.
    await asyncio.wait_for(running.acquire(), WAIT)
    await asyncio.wait_for(running.acquire(), WAIT)
    gate.set()

    assert (await dispatcher.wait(first.task_id, WAIT)).status == "completed"
    assert (await dispatcher.wait(second.task_id, WAIT)).status == "completed"
    trees = {call.cwd for call in adapters["codex"].calls}
    assert len(trees) == 2


@pytest.mark.asyncio
async def test_task_package_tells_the_executor_it_is_in_a_worktree(tmp_path, git_repo):
    _, _, adapters, dispatcher = await _make(tmp_path)

    record = await dispatcher.submit(_req(git_repo))
    await dispatcher.wait(record.task_id, WAIT)

    prompt = adapters["codex"].calls[0].prompt
    assert "worktree created from HEAD" in prompt
    assert str(git_repo) in prompt


@pytest.mark.asyncio
async def test_in_place_mode_still_runs_in_the_working_copy(tmp_path, git_repo):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="codex"),
        executors={"codex": ExecutorSettings(adapter="codex", description="codex")},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {"codex": FakeAdapter("codex")}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    adapters["codex"].on_execute = _writes("x = 2\n")

    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert adapters["codex"].calls[0].cwd == str(git_repo)
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert "worktree" not in done.result.meta


@pytest.mark.asyncio
async def test_request_can_override_the_configured_mode(tmp_path, git_repo):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="codex"),
        executors={"codex": ExecutorSettings(adapter="codex", description="codex")},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {"codex": FakeAdapter("codex")}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )

    record = await dispatcher.submit(_req(git_repo, workspace_mode="worktree"))
    await dispatcher.wait(record.task_id, WAIT)

    assert adapters["codex"].calls[0].cwd != str(git_repo)
