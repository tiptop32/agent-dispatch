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
from agent_dispatch.executors.process import ProcessOutcome
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.executors.result_parser import normalize
from agent_dispatch.models import DispatchRequest, ExecutionResult, TaskStatus, Usage
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter


class FreshFakeAdapter(FakeAdapter):
    async def execute(self, ctx):
        return (await super().execute(ctx)).model_copy(deep=True)


def result(executor="worker", **updates):
    return ExecutionResult.model_validate(
        {
            "status": "completed",
            "executor": executor,
            "model": None,
            "summary": "done",
            "changed_files": ["a.py"],
            "meta": {"session_id": "work-session"},
            **updates,
        }
    )


async def make_dispatcher(
    tmp_path, *, rounds=1, reviewers="first", execution=None, routing=None, corporate=False
):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path / "data"),
        execution=ExecutionSettings(review_fix_rounds=rounds, **(execution or {})),
        routing=RoutingSettings(
            fallback_executor="worker", corporate_perimeter=False, **(routing or {})
        ),
        executors={
            "worker": ExecutorSettings(
                adapter="omp",
                model="test/worker",
                corporate=corporate,
                review_by=None if corporate else reviewers,
                perimeter_review_by=reviewers if corporate else None,
            ),
            "first": ExecutorSettings(adapter="omp", model="test/first", corporate=corporate),
            "second": ExecutorSettings(adapter="omp", model="test/second", corporate=corporate),
            "senior": ExecutorSettings(adapter="omp", model="test/senior", corporate=corporate),
        },
        escalation={"worker": ["senior"]},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FreshFakeAdapter(name, result=result(name, changed_files=[], meta={}))
        for name in settings.executors
    }
    adapters["worker"].result = result(usage=Usage(cost_usd=0.1))
    for name in ("first", "second"):
        adapters[name].result = result(name, changed_files=[], verdict="pass", meta={})
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return storage, adapters, dispatcher


def request(repo, **updates):
    return DispatchRequest(task="repair the parser", cwd=str(repo), executor="worker", **updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_mode", ["in_place", "worktree"])
async def test_failed_review_resumes_and_merges_the_fix(tmp_path, git_repo, workspace_mode):
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, execution={"workspace_mode": workspace_mode}
    )
    adapters["first"].result = result(
        "first", changed_files=[], verdict="fail", summary="repair return type"
    )

    async def work(ctx):
        if len(adapters["worker"].calls) == 1:
            (Path(ctx.cwd) / "a.py").write_text("x = 'bad'\n")
            return
        assert ctx.resume_session == "work-session"
        assert ctx.cwd == adapters["worker"].calls[0].cwd
        assert "repair return type" in ctx.prompt
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")
        (Path(ctx.cwd) / "fixed.py").write_text("fixed = True\n")
        adapters["worker"].result = result(changed_files=["fixed.py"], usage=Usage(cost_usd=0.2))
        adapters["first"].result = result("first", changed_files=[], verdict="pass")

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(request(git_repo, verify=["test -f a.py"]))
    done = await dispatcher.wait(task.task_id, 10)
    assert done.task_id == task.task_id and done.status == TaskStatus.completed
    assert done.result.changed_files == ["a.py", "fixed.py"]
    assert done.result.usage.cost_usd == pytest.approx(0.3)
    assert done.result.verification.result == "passed"
    assert done.result.meta["review"]["verdict"] == "pass"
    [repair] = done.result.meta["fix_rounds"]
    assert repair["trigger"] == "review_failed"
    assert repair["stage"] == "first" and repair["round"] == 1
    assert repair["verdict"] == "pass"
    assert repair["usage"]["cost_usd"] == 0.2
    assert len(adapters["first"].calls) == 2
    assert not adapters["senior"].calls
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert await storage.count_children(task.task_id) == 0
    await storage.close()


@pytest.mark.asyncio
async def test_exhausted_fix_escalates_with_all_findings(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path)
    adapters["first"].result = result(
        "first", verdict="fail", changed_files=[], summary="first finding"
    )

    async def review(ctx):
        if len(adapters["first"].calls) == 2:
            adapters["first"].result = result(
                "first", verdict="fail", changed_files=[], summary="second finding"
            )

    adapters["first"].on_execute = review
    task = await dispatcher.submit(request(git_repo, context="caller notes"))
    done = await dispatcher.wait(task.task_id, 10)
    original = await storage.get_task(task.task_id)
    assert done.decision.executor == "senior"
    assert "first finding" in done.request.context and "second finding" in done.request.context
    assert original.result.meta["fix_rounds"][0]["verdict"] == "fail"
    events = await storage.list_events(task.task_id)
    assert any(
        e["kind"] == "escalate" and e["payload"]["reason"] == "review_failed" for e in events
    )
    await storage.close()


@pytest.mark.asyncio
async def test_failed_verification_feedback_is_rechecked_before_review(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path)

    async def work(ctx):
        if len(adapters["worker"].calls) == 2:
            assert "expected marker" in ctx.prompt
            assert "verification" in ctx.prompt.lower()
            assert not adapters["first"].calls
            (Path(ctx.cwd) / "marker").write_text("ok")

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(
        request(git_repo, verify=["test -f marker || { echo 'expected marker'; exit 1; }"])
    )
    done = await dispatcher.wait(task.task_id, 10)
    assert done.status == TaskStatus.completed and done.result.error is None
    assert done.result.verification.result == "passed"
    assert done.result.meta["fix_rounds"][0]["trigger"] == "verification_failed"
    assert len(adapters["first"].calls) == 1
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,rounds,cost_limit,expected_calls",
    [("fail", 0, None, 1), (None, 1, None, 1), ("fail", 1, 0.1, 1)],
)
async def test_disabled_unknown_and_budget_do_not_start_fix(
    tmp_path, git_repo, verdict, rounds, cost_limit, expected_calls
):
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, rounds=rounds, routing={"max_chain_cost_usd": cost_limit}
    )
    adapters["first"].result = result("first", verdict=verdict, changed_files=[])
    task = await dispatcher.submit(request(git_repo, allow_escalation=False))
    done = await dispatcher.wait(task.task_id, 10)
    assert len(adapters["worker"].calls) == expected_calls
    assert "fix_rounds" not in done.result.meta
    if cost_limit is not None:
        assert done.result.meta["budget"]["limit_usd"] == cost_limit
    await storage.close()


@pytest.mark.asyncio
async def test_parent_cancellation_interrupts_fix_and_prod_rule_is_present(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path, corporate=True)
    adapters["first"].result = result("first", verdict="fail", changed_files=[])
    fixing = asyncio.Event()

    async def work(ctx):
        if len(adapters["worker"].calls) == 2:
            assert "Prod data rule" in ctx.prompt
            fixing.set()
            await asyncio.Event().wait()

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(request(git_repo, prod=True))
    await asyncio.wait_for(fixing.wait(), 5)
    await dispatcher.cancel(task.task_id)
    assert adapters["worker"].cancelled
    assert (await storage.get_task(task.task_id)).status == TaskStatus.cancelled
    assert len(adapters["first"].calls) == 1
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("report_succeeds", [False, True])
async def test_repair_missing_report_preserves_blocker_even_after_report_recovery(
    tmp_path, git_repo, report_succeeds
):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path)
    adapters["first"].result = result("first", changed_files=[], verdict="fail", summary="bug")

    async def work(ctx):
        calls = len(adapters["worker"].calls)
        if calls == 2:
            adapters["worker"].result = result(
                status="partial",
                changed_files=["fixed.py"],
                meta={"session_id": "work-session", "parse_error": "no result block"},
            )
        elif calls == 3:
            if report_succeeds:
                adapters["worker"].result = result(changed_files=[])
                adapters["first"].result = result("first", changed_files=[], verdict="pass")
            else:
                adapters["worker"].result = result(status="failed", error="report unavailable")

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(request(git_repo, allow_escalation=False))
    done = await dispatcher.wait(task.task_id, 10)
    assert len(adapters["worker"].calls) == 3
    assert "fixed.py" in done.result.changed_files
    assert done.result.meta["review"]["verdict"] == "fail"
    assert len(adapters["first"].calls) == 1
    assert done.status == TaskStatus.partial
    assert done.result.meta["fix_rounds"][0]["verdict"] == "fail"
    assert done.result.meta["fix_rounds"][0]["report_recovery"] == (
        "recovered" if report_succeeds else "failed: report unavailable"
    )
    await storage.close()


@pytest.mark.asyncio
async def test_successful_fix_clears_stale_interruption_flags(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path)
    initial = normalize(
        None,
        ProcessOutcome(
            exit_code=-15,
            stdout="partial work",
            stderr="",
            timed_out=True,
            duration_ms=100,
            timeout_seconds=30,
        ),
        ["a.py"],
        "worker",
        None,
    )
    initial.meta.update(
        {
            "session_id": "work-session",
            "stalled": True,
            "parse_error": "old parse failure",
            "cli_error": True,
            "failure_class": "timeout",
        }
    )
    adapters["worker"].result = initial

    async def work(ctx):
        if len(adapters["worker"].calls) == 2:
            adapters["worker"].result = result()
            (Path(ctx.cwd) / "marker").write_text("ok")

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(request(git_repo, verify=["test -f marker"]))
    done = await dispatcher.wait(task.task_id, 10)
    assert done.task_id == task.task_id and done.status == TaskStatus.completed
    assert not adapters["senior"].calls
    for key in (
        "timed_out",
        "stalled",
        "parse_error",
        "cli_error",
        "failure_cause",
        "failure_class",
        "timeout_type",
    ):
        assert key not in done.result.meta
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_verification_fix_verdict_is_not_overwritten_by_review(tmp_path, git_repo, repaired):
    from evals.replay.harness import fix_round, load

    storage, adapters, dispatcher = await make_dispatcher(tmp_path)
    adapters["first"].result = result("first", changed_files=[], verdict="fail", meta={})

    async def work(ctx):
        if repaired and len(adapters["worker"].calls) == 2:
            (Path(ctx.cwd) / "marker").write_text("ok")

    adapters["worker"].on_execute = work
    try:
        task = await dispatcher.submit(
            request(git_repo, verify=["test -f marker"], allow_escalation=False)
        )
        done = await dispatcher.wait(task.task_id, 10)
        assert done.result.meta["fix_rounds"][0]["verdict"] == ("pass" if repaired else "fail")
        assert done.result.verification.result == ("passed" if repaired else "failed")
        assert bool(adapters["first"].calls) is repaired
        measured = fix_round(load(tmp_path / "db.sqlite"))
        assert measured["recovered"] == int(repaired)
        assert measured["failed"] == int(not repaired)
    finally:
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ceiling,task_timeout,expected",
    [(None, 1800, 600), (45, 1800, 45), (600, 30, 30), (0, 1800, 1800)],
)
async def test_fix_round_timeout_and_elapsed_duration(
    tmp_path, git_repo, monkeypatch, ceiling, task_timeout, expected
):
    from types import SimpleNamespace

    from agent_dispatch.dispatch import dispatcher as module

    execution = {} if ceiling is None else {"review_fix_timeout_seconds": ceiling}
    storage, adapters, dispatcher = await make_dispatcher(tmp_path, execution=execution)
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    adapters["first"].result = result("first", changed_files=[], verdict="fail")

    async def fix(ctx):
        if len(adapters["worker"].calls) == 2:
            now[0] += 12.5
            adapters["first"].result = result("first", changed_files=[], verdict="pass")

    adapters["worker"].on_execute = fix
    try:
        task = await dispatcher.submit(request(git_repo, timeout_seconds=task_timeout))
        done = await dispatcher.wait(task.task_id, 10)
        assert done.status == TaskStatus.completed
        assert adapters["worker"].calls[1].timeout_seconds == expected
        assert done.result.meta["fix_rounds"][0]["duration_s"] == 12.5
    finally:
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["review_failed", "verification_failed"])
@pytest.mark.parametrize("failure", ["unavailable", "resume", "missing_report"])
async def test_fix_failure_preserves_work_and_escalation_reason(
    tmp_path, git_repo, trigger, failure
):
    from evals.replay.harness import fix_round, load, post_review

    storage, adapters, dispatcher = await make_dispatcher(tmp_path)
    adapters["first"].result = result(
        "first", changed_files=[], verdict="fail", summary="original finding"
    )
    error = {
        "unavailable": "executor became unavailable while waiting for a slot: worker",
        "resume": "failed --resume: session not found",
        "missing_report": None,
    }[failure]
    status = "partial" if failure == "missing_report" else "failed"

    async def fix(ctx):
        count = len(adapters["worker"].calls)
        if count == 2:
            (Path(ctx.cwd) / "fixed.py").write_text("attempted = True\n")
            adapters["worker"].result = result(
                status=status,
                error=error,
                changed_files=["fixed.py"],
                usage=Usage(cost_usd=0.2),
                meta={"session_id": "work-session", "parse_error": "no result block"},
            )
        elif count == 3:
            adapters["worker"].result = result(
                changed_files=[], usage=Usage(cost_usd=0.03), summary="report recovered"
            )

    adapters["worker"].on_execute = fix
    try:
        task = await dispatcher.submit(
            request(git_repo, verify=["false"] if trigger == "verification_failed" else [])
        )
        done = await dispatcher.wait(task.task_id, 10)
        original = await storage.get_task(task.task_id)
        assert done.decision.executor == "senior"
        assert original.result.status == "partial"
        assert original.result.summary == "done"
        assert original.result.changed_files == ["a.py", "fixed.py"]
        assert original.result.usage.cost_usd == pytest.approx(
            0.33 if failure == "missing_report" else 0.3
        )
        row = original.result.meta["fix_rounds"][0]
        assert row["verdict"] == "fail"
        assert row["status"] == status
        assert row["error"] == error
        assert row["parse_error"] == "no result block"
        events = await storage.list_events(task.task_id)
        assert any(e["kind"] == "escalate" and e["payload"]["reason"] == trigger for e in events)
        assert len(adapters["first"].calls) == int(trigger == "review_failed")
        measured = fix_round(load(tmp_path / "db.sqlite"))
        assert measured["failed"] == 1 and measured["recovered"] == 0
        if trigger == "review_failed":
            review = post_review(load(tmp_path / "db.sqlite"))
            assert review["verdicts"]["fail"] == 1
            assert review["review_failed"]["escalated"] == 1
    finally:
        await dispatcher.shutdown()
        await storage.close()
