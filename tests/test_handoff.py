from __future__ import annotations

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
from agent_dispatch.models import DispatchRequest, ExecutionResult, Usage
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter


def interrupted(name="first", error="timeout", files=None):
    return ExecutionResult(
        status="partial" if error == "timeout" else "failed",
        executor=name,
        model=None,
        summary=f"{name} implemented the first part",
        changed_files=["a.py"] if files is None else files,
        error=error,
    )


async def make_dispatcher(
    tmp_path, *, mode="worktree", chain=None, results=None, prod=False, review=False, budget=None
):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path / "data"),
        routing=RoutingSettings(
            fallback_executor="first", corporate_perimeter=False, max_chain_cost_usd=budget
        ),
        execution=ExecutionSettings(workspace_mode=mode),
        executors={
            name: ExecutorSettings(
                adapter="omp",
                model="fake/model",
                description=name,
                corporate=prod and name != "external",
                review_by="reviewer" if review and name == "second" else None,
            )
            for name in ("first", "second", "third", "external", "reviewer")
        },
        escalation={"first": ["second"]} if chain is None else chain,
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name)) for name in settings.executors
    }
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return storage, adapters, dispatcher


def write_file(text, name="a.py"):
    async def execute(ctx):
        (Path(ctx.cwd) / name).write_text(text)

    return execute


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worktree", "in_place"])
@pytest.mark.parametrize("error", ["timeout", "stalled: idle", "stalled: looping"])
@pytest.mark.parametrize("child_edits", [False, True])
async def test_interrupted_work_is_continued_and_inherited_files_are_verified_and_reviewed(
    tmp_path, git_repo, mode, error, child_edits
):
    results = {
        "first": interrupted(error=error),
        "second": ExecutionResult(
            status="completed",
            executor="second",
            model=None,
            summary="finished",
            changed_files=["new.py"] if child_edits else [],
        ),
        "reviewer": ExecutionResult(
            status="completed", executor="reviewer", model=None, summary="good", verdict="pass"
        ),
    }
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, mode=mode, results=results, review=True
    )
    adapters["first"].on_execute = write_file("x = 2\n")

    async def continue_work(ctx):
        assert (Path(ctx.cwd) / "a.py").read_text() == "x = 2\n"
        assert "Continue the started work" in ctx.prompt
        assert "first implemented the first part" in ctx.prompt
        if child_edits:
            (Path(ctx.cwd) / "new.py").write_text("done = True\n")

    adapters["second"].on_execute = continue_work
    first = await dispatcher.submit(
        DispatchRequest(
            task="finish the feature",
            cwd=str(git_repo),
            executor="first",
            context_mode="prompt",
            verify=["test \"$(cat a.py)\" = 'x = 2'"],
        )
    )
    done = await dispatcher.wait(first.task_id, 10)
    parent = await storage.get_task(first.task_id)
    assert done.status == "completed"
    assert done.result.changed_files == (["a.py", "new.py"] if child_edits else ["a.py"])
    assert done.result.verification.result == "passed"
    assert done.result.meta["review"]["verdict"] == "pass"
    assert len(adapters["reviewer"].calls) == 1
    assert done.result.meta["handoff"] == {
        "from_task": first.task_id,
        "mode": "patch" if mode == "worktree" else "in_place",
        "apply_failed": False,
    }
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    if mode == "worktree":
        assert parent.result.meta["integration_held"] == "escalated"
        assert done.result.meta["integrated"] is True
        events = await storage.list_events(first.task_id)
        assert not any(e["kind"] == "integrate" and e["payload"].get("ok") for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["end", "permission", "perimeter", "budget", "review_only"])
async def test_timeout_without_eligible_next_link_keeps_partial_and_integrates(
    tmp_path, git_repo, stop
):
    result = interrupted()
    if stop == "budget":
        result.usage = Usage(cost_usd=1)
    chain = (
        {"first": []}
        if stop == "end"
        else {"first": ["external" if stop == "perimeter" else "second"]}
    )
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path,
        chain=chain,
        results={"first": result},
        prod=stop == "perimeter",
        budget=0.5 if stop == "budget" else None,
    )
    if stop == "review_only":
        # The root must still be admitted; restrict only the next link's adapter.
        dispatcher.settings.executors["second"].adapter = "claude"
        dispatcher.settings.routing.review_only = {"omp": ["claude"]}
    adapters["first"].on_execute = write_file("x = 2\n")
    first = await dispatcher.submit(
        DispatchRequest(
            task="finish",
            cwd=str(git_repo),
            executor="first",
            source_agent="omp",
            allow_escalation=stop != "permission",
            prod=stop == "perimeter",
        )
    )
    done = await dispatcher.wait(first.task_id, 10)
    assert done.task_id == first.task_id
    assert done.status == "partial" and done.result.error == "timeout"
    assert done.result.meta["integrated"] is True
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert not adapters["second"].calls and not adapters["external"].calls


@pytest.mark.asyncio
async def test_three_links_apply_only_immediate_parent_patch_and_keep_prod_rule(
    tmp_path, git_repo, monkeypatch
):
    results = {"first": interrupted(), "second": interrupted("second", files=["new.py"])}
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path,
        prod=True,
        chain={"first": ["second"], "second": ["external", "third"]},
        results=results,
    )
    adapters["first"].on_execute = write_file("x = 2\n")
    adapters["second"].on_execute = write_file("part = 2\n", "new.py")
    applied = []
    real_apply = worktree.apply_patch

    def apply(cwd, patch):
        applied.append((str(cwd), patch.name))
        return real_apply(cwd, patch)

    monkeypatch.setattr(worktree, "apply_patch", apply)

    async def finish(ctx):
        assert (Path(ctx.cwd) / "a.py").read_text() == "x = 2\n"
        assert (Path(ctx.cwd) / "new.py").read_text() == "part = 2\n"
        assert "first implemented the first part" in ctx.prompt
        assert "second implemented the first part" in ctx.prompt
        assert "Prod data rule" in ctx.prompt

    adapters["third"].on_execute = finish
    first = await dispatcher.submit(
        DispatchRequest(task="finish", cwd=str(git_repo), executor="first", prod=True)
    )
    done = await dispatcher.wait(first.task_id, 10)
    second = await storage.get_task(done.escalated_from)
    assert done.status == "completed" and done.decision.meta["perimeter"] == "prod"
    assert done.result.changed_files == ["a.py", "new.py"]
    assert not adapters["external"].calls
    assert [name for _, name in applied] == [
        f"{first.task_id}.patch",
        f"{second.task_id}.patch",
        f"{done.task_id}.patch",
    ]


@pytest.mark.asyncio
async def test_conflicting_handoff_patch_rolls_back_before_child_starts(
    tmp_path, git_repo, monkeypatch
):
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, results={"first": interrupted()}
    )
    adapters["first"].on_execute = write_file("x = 2\n")
    real_apply = worktree.apply_patch

    def conflict(cwd, patch):
        # Replace the saved parent patch with a valid patch whose base conflicts
        # with HEAD; an added file preceding the conflict must roll back too.
        if patch.name != f"{root_id[0]}.patch":
            return real_apply(cwd, patch)
        patch.write_text(
            "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n"
            "+++ b/new.py\n@@ -0,0 +1 @@\n+temporary = True\n"
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 999\n+x = 3\n"
        )
        return real_apply(cwd, patch)

    monkeypatch.setattr(worktree, "apply_patch", conflict)
    root_id = []

    async def finish(ctx):
        assert (Path(ctx.cwd) / "a.py").read_text() == "x = 1\n"
        assert not (Path(ctx.cwd) / "new.py").exists()
        assert "workspace starts clean from HEAD" in ctx.prompt
        status = subprocess.run(  # noqa: ASYNC221
            ["git", "status", "--porcelain"],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""

    adapters["second"].on_execute = finish
    first = await dispatcher.submit(
        DispatchRequest(task="finish", cwd=str(git_repo), executor="first")
    )
    root_id.append(first.task_id)
    done = await dispatcher.wait(first.task_id, 10)
    assert done.status == "completed"
    assert done.result.meta["handoff"]["apply_failed"]
    assert done.result.changed_files == []
    assert (git_repo / "a.py").read_text() == "x = 1\n"
