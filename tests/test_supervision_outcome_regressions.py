from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_dispatch.models import ExecutionResult
from tests.test_supervision import request, running


@pytest.mark.asyncio
async def test_cancel_counts_worker_receipt_separately_from_finished_checks(
    tmp_path, monkeypatch, git_repo
):
    async with running(tmp_path, monkeypatch, mode="continue", interval_seconds=1) as (
        dispatcher,
        storage,
        _,
        _,
    ):
        task = await dispatcher.submit(
            request(git_repo, source_agent="omp", workspace_mode="worktree")
        )
        async with asyncio.timeout(8):
            while not any(  # noqa: ASYNC110
                event["kind"] == "supervision" and event["payload"].get("decision") == "continue"
                for event in await storage.list_events(task.task_id)
            ):
                await asyncio.sleep(0.01)
        done = await dispatcher.cancel(task.task_id)
        assert done.status == "cancelled"
        assert done.result.usage.input_tokens == 877
        assert done.result.usage.output_tokens == 31
        assert done.result.usage.cost_usd == pytest.approx(0.004)
        accounting = done.result.meta["supervision_usage"]
        assert accounting["checks_finished"] == 1
        assert accounting["usage"]["input_tokens"] == 100
        assert accounting["usage"]["output_tokens"] == 20
        assert accounting["usage"]["cost_usd"] == pytest.approx(0.001)
        assert Path(done.result.meta["worktree"]).is_dir()  # noqa: ASYNC240
        assert (git_repo / "a.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_supervised_automatic_review_preserves_reviewer_changes_in_parent_patch(
    tmp_path, monkeypatch, git_repo
):
    async with running(tmp_path, monkeypatch, mode="continue-stop") as (dispatcher, _, adapters, _):
        dispatcher.settings.executors["worker"].review_by = "other"

        async def work(ctx):
            (Path(ctx.cwd) / "a.py").write_text("worker change\n")

        async def review(ctx):
            (Path(ctx.cwd) / "reviewer.txt").write_text("reviewer change\n")
            await asyncio.Event().wait()

        adapters["worker"].on_execute = work
        adapters["worker"].result = ExecutionResult(
            status="completed",
            executor="worker",
            model=None,
            summary="done",
            changed_files=["a.py"],
        )
        adapters["other"].on_execute = review
        task = await dispatcher.submit(request(git_repo, workspace_mode="worktree"))
        done = await dispatcher.wait(task.task_id, 8)
        assert done.status == "failed"
        assert done.result.meta["failure_class"] == "supervision_stopped"
        assert set(done.result.changed_files) == {"a.py", "reviewer.txt"}
        assert done.result.meta["diff_stat"]["files"] == 2
        patch = Path(done.result.meta["patch"]).read_text()  # noqa: ASYNC240
        assert "worker change" in patch
        assert "reviewer change" in patch
        assert done.result.meta["integrated"] is False
        assert (git_repo / "a.py").read_text() == "x = 1\n"
        assert not (git_repo / "reviewer.txt").exists()
