from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_dispatch.models import DispatchRequest, ExecutionResult, Usage
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,decisions",
    [
        ("error-continue", ["error", "continue"]),
        ("error-error", ["error", "error"]),
        ("error-continue-error", ["error", "continue", "error"]),
    ],
)
async def test_no_verdict_streak_controls_worker_and_descendant_lifecycle(
    tmp_path, monkeypatch, git_repo, mode, decisions
):
    async with running(tmp_path, monkeypatch, mode=mode, delay=0, interval_seconds=1) as (
        dispatcher,
        storage,
        adapters,
        control,
    ):
        adapters["worker"].result = ExecutionResult(
            status="completed",
            executor="worker",
            model="worker-model",
            summary="worker finished normally",
            usage=Usage(input_tokens=777, output_tokens=11, cost_usd=0.003),
        )
        adapters["other"].gate = asyncio.Event()
        root = await dispatcher.submit(request(git_repo))

        async def wait_notes(count):
            async with asyncio.timeout(8):
                while True:
                    notes = [
                        event["payload"]
                        for event in await storage.list_events(root.task_id)
                        if event["kind"] == "supervision"
                    ]
                    if len(notes) >= count:
                        return notes
                    await asyncio.sleep(0.01)

        first = (await wait_notes(1))[0]
        assert first["decision"] == "error"
        assert first["evidence"] == []
        assert first["check"] == 1
        assert first["model"] == "agent-lb/gpt-6.1-sol"
        assert "no_accounts" in first["reason"]
        # A later completion must not hide a transient failed result or ancestor gate.
        live = await storage.get_task(root.task_id)
        assert live.status == "running"
        assert live.result is None
        assert not (control / "worker-late").exists()
        child = await dispatcher.submit(
            DispatchRequest(
                task="Child: keep working",
                cwd=str(git_repo),
                executor="other",
                parent_task_id=root.task_id,
                hop=1,
                source_agent="cli",
                kind="review",
            )
        )
        await asyncio.wait_for(adapters["other"].started.wait(), 8)
        assert (await dispatcher.get(child.task_id)).status == "running"
        notes = await wait_notes(len(decisions))
        assert [note["decision"] for note in notes[: len(decisions)]] == decisions
        # The judge sees the last real verdict as previous_check, never a provider failure.
        for number in range(2, len(decisions) + 1):
            sent = json.loads((control / f"payload-{number}.json").read_text())["payload"]
            verdicts = [d for d in decisions[: number - 1] if d != "error"]
            previous = sent["previous_check"]
            assert (previous or {}).get("decision") == (verdicts[-1] if verdicts else None)

        if mode == "error-error":
            done = await dispatcher.wait(root.task_id, 8)
            assert done.status == "failed"
            assert done.result.meta["error_code"] == "supervision_error"
            assert done.result.meta["supervision"]["consecutive_errors"] == 2
            assert "check 2" in done.result.error
            assert not (control / "worker-late").exists()
            child_done = await dispatcher.get(child.task_id)
            assert child_done.status == "cancelled"
            assert child_done.result.meta["supervision_origin"] == root.task_id
        else:
            live = await storage.get_task(root.task_id)
            assert live.status == "running"
            assert live.result is None
            assert (await dispatcher.get(child.task_id)).status == "running"
            (control / "finish-worker").touch()
            done = await dispatcher.wait(root.task_id, 8)
            assert done.status == "completed"
            assert done.result.summary == "worker finished normally"
            assert done.result.model == "worker-model"
            assert "supervision" not in done.result.meta
            assert (control / "worker-late").read_text() == "worker kept writing"
            adapters["other"].gate.set()
            assert (await dispatcher.wait(child.task_id, 8)).status == "completed"

        # Extra continue ticks may land before finish-worker under load; accounting must
        # still bill every finished check exactly once.
        accounting = done.result.meta["supervision_usage"]
        finished = accounting["checks_finished"]
        assert finished >= len(decisions)
        assert accounting["checks_started"] == finished
        assert accounting["usage"]["input_tokens"] == 100 * finished
        assert accounting["usage"]["output_tokens"] == 20 * finished
        assert accounting["usage"]["cost_usd"] == pytest.approx(0.001 * len(decisions))
        assert done.result.usage.cost_usd == pytest.approx(0.003 + 0.001 * len(decisions))
