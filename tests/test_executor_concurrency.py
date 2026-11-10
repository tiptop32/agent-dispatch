"""Executor CLI slots cover roots, reviews, nested work, and cancellation."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    LimitGroupSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult, RouteDecision, RouterKind
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

A = "omp/a"
B = "omp/b"
WAIT = 3
QUEUE_WAIT = 0.06


@pytest.fixture(autouse=True)
def _repository_template(git_repo):
    """Provide a committed repository for each independent working copy."""


@asynccontextmanager
async def lab(tmp_path, *, executors=None, groups=None, group_capacity=8):
    settings = Settings(
        server=ServerSettings(
            data_dir=tmp_path,
            max_concurrent_tasks=8,
            max_concurrent_per_group=group_capacity,
            max_concurrent_reviews=4,
        ),
        routing=RoutingSettings(
            fallback_executor=[A, B],
            corporate_perimeter=False,
            exclude_source_agent=False,
            review_only={},
            max_hops=5,
            max_children=10,
            preferred_executor_max_wait_seconds=0.03,
        ),
        execution=ExecutionSettings(workspace_mode="in_place"),
        executors=executors
        or {
            A: ExecutorSettings(adapter="omp", model="provider-a/a", max_concurrent=1),
            B: ExecutorSettings(adapter="omp", model="provider-b/b", max_concurrent=1),
        },
        limit_groups=groups or {},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    router = FakeRouter(
        RouteDecision(executor=A, confidence=1.0, scores={A: 1.0}, router=RouterKind.jev)
    )
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    try:
        yield settings, storage, adapters, dispatcher
    finally:
        await dispatcher.shutdown()
        await storage.close()


def req(repo, **kwargs):
    return DispatchRequest(
        task="fix the implementation",
        cwd=str(repo),
        source_agent="cli",
        allow_escalation=False,
        **kwargs,
    )


def directory(tmp_path, name):
    path = tmp_path / name
    if path != tmp_path / "repo":
        shutil.copytree(tmp_path / "repo", path)
    return path


async def occupy(dispatcher, adapter, repo, **kwargs):
    adapter.gate = asyncio.Event()
    adapter.started = asyncio.Event()
    task = await dispatcher.submit(req(repo, executor=adapter.name, **kwargs))
    await asyncio.wait_for(adapter.started.wait(), WAIT)
    return task


async def assert_queued(adapter):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(adapter.started.wait(), QUEUE_WAIT)


async def wait_for_spawn(storage, task_id):
    async with asyncio.timeout(WAIT):
        while not any(  # noqa: ASYNC110
            event["kind"] == "spawn" for event in await storage.list_events(task_id)
        ):
            await asyncio.sleep(0.01)


def observe_executor_wait(dispatcher, monkeypatch):
    waiting = asyncio.Event()
    original = dispatcher._executor_concurrency.slot

    @asynccontextmanager
    async def slot(task_id, executor, **kwargs):
        waiting.set()
        async with original(task_id, executor, **kwargs):
            yield

    monkeypatch.setattr(dispatcher._executor_concurrency, "slot", slot)
    return waiting


@pytest.mark.asyncio
async def test_executor_serializes_roots_and_records_executor_wait(tmp_path, monkeypatch):
    async with lab(tmp_path) as (_, storage, adapters, d):
        adapter = adapters[A]
        first = await occupy(d, adapter, directory(tmp_path, "first"))
        waiting = observe_executor_wait(d, monkeypatch)
        adapter.started = asyncio.Event()
        second = await d.submit(req(directory(tmp_path, "second"), executor=A))
        await asyncio.wait_for(waiting.wait(), WAIT)
        await assert_queued(adapter)
        adapter.gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        done = await d.wait(second.task_id, WAIT)
        assert done.status == "completed"
        slots = [
            event["payload"]
            for event in await storage.list_events(second.task_id)
            if event["kind"] == "slot" and "executor_wait_seconds" in event["payload"]
        ]
        assert slots and slots[-1]["executor"] == A
        assert slots[-1]["executor_wait_seconds"] >= QUEUE_WAIT


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 2])
async def test_unlimited_or_larger_executor_capacity_keeps_roots_parallel(tmp_path, limit):
    executors = {
        A: ExecutorSettings(adapter="omp", model="provider/a", max_concurrent=limit),
        B: ExecutorSettings(adapter="omp", model="other/b"),
    }
    async with lab(tmp_path, executors=executors) as (_, _, adapters, d):
        adapter = adapters[A]
        first = await occupy(d, adapter, directory(tmp_path, "first"))
        adapter.started = asyncio.Event()
        second = await d.submit(req(directory(tmp_path, "second"), executor=A))
        await asyncio.wait_for(adapter.started.wait(), WAIT)
        assert len(adapter.calls) == 2
        adapter.gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        assert (await d.wait(second.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_group_override_replaces_default_but_still_bounds_the_group(tmp_path):
    executors = {name: ExecutorSettings(adapter="omp", model=f"shared/{name}") for name in (A, B)}
    async with lab(
        tmp_path,
        executors=executors,
        groups={"omp:shared": LimitGroupSettings(max_concurrent=2)},
        group_capacity=1,
    ) as (_, _, adapters, d):
        first = await occupy(d, adapters[A], directory(tmp_path, "first"))
        second = await occupy(d, adapters[B], directory(tmp_path, "second"))
        adapters[A].started = asyncio.Event()
        third = await d.submit(req(directory(tmp_path, "third"), executor=A))
        await assert_queued(adapters[A])
        adapters[B].gate.set()
        assert (await d.wait(second.task_id, WAIT)).status == "completed"
        await asyncio.wait_for(adapters[A].started.wait(), WAIT)
        adapters[A].gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        assert (await d.wait(third.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,hop", [("review", 0), ("task", 1), ("review", 1)])
async def test_review_and_unrelated_nested_work_take_executor_slots(tmp_path, kind, hop):
    async with lab(tmp_path) as (_, _, adapters, d):
        parent = await d.submit(req(directory(tmp_path, "parent"), executor=B))
        assert (await d.wait(parent.task_id, WAIT)).status == "completed"
        first = await occupy(d, adapters[A], directory(tmp_path, "first"))
        adapters[A].started = asyncio.Event()
        second = await d.submit(
            req(
                directory(tmp_path, "second"),
                executor=A,
                kind=kind,
                hop=hop,
                parent_task_id=parent.task_id if hop else None,
            )
        )
        await assert_queued(adapters[A])
        adapters[A].gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        done = await d.wait(second.task_id, WAIT)
        assert done.status == "completed"
        assert adapters[A].calls[-1].read_only is (kind == "review")


@pytest.mark.asyncio
async def test_nested_same_executor_inherits_active_ancestor_through_other_executor(tmp_path):
    repo = directory(tmp_path, "repo")
    async with lab(tmp_path, group_capacity=1) as (_, _, adapters, d):
        nested = []

        async def run_a(ctx):
            if not nested:
                nested.append("parent")
                child = await d.submit(req(repo, executor=B, parent_task_id=ctx.task_id, hop=1))
                assert (await d.wait(child.task_id, WAIT)).status == "completed"

        async def run_b(ctx):
            grandchild = await d.submit(req(repo, executor=A, parent_task_id=ctx.task_id, hop=2))
            assert (await d.wait(grandchild.task_id, WAIT)).status == "completed"

        adapters[A].on_execute = run_a
        adapters[B].on_execute = run_b
        root = await d.submit(req(repo, executor=A))
        done = await d.wait(root.task_id, WAIT)
        assert done.status == "completed"
        assert len(adapters[A].calls) == 2 and len(adapters[B].calls) == 1


@pytest.mark.asyncio
async def test_cross_executor_nested_cycle_fails_one_child_and_unblocks_both_roots(tmp_path):
    repos = {A: directory(tmp_path, "first"), B: directory(tmp_path, "second")}
    entered = {A: asyncio.Event(), B: asyncio.Event()}
    children = []
    async with lab(tmp_path) as (_, storage, adapters, d):

        def execute(name, other):
            async def run(ctx):
                record = await storage.get_task(ctx.task_id)
                if record.parent_task_id:
                    return
                entered[name].set()
                await entered[other].wait()
                child = await d.submit(
                    req(repos[name], executor=other, hop=1, parent_task_id=ctx.task_id)
                )
                children.append(await d.wait(child.task_id, WAIT))

            return run

        adapters[A].on_execute = execute(A, B)
        adapters[B].on_execute = execute(B, A)
        first = await d.submit(req(repos[A], executor=A))
        second = await d.submit(req(repos[B], executor=B))
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        assert (await d.wait(second.task_id, WAIT)).status == "completed"
        assert sorted(child.status for child in children) == ["completed", "failed"]
        failed = next(child for child in children if child.status == "failed")
        assert "executor capacity deadlock" in failed.result.error
        assert len(adapters[A].calls) + len(adapters[B].calls) == 3


@pytest.mark.asyncio
async def test_cancelled_nested_wait_does_not_leave_a_false_cycle(tmp_path):
    repos = {A: directory(tmp_path, "first"), B: directory(tmp_path, "second")}
    async with lab(tmp_path) as (_, storage, adapters, d):
        first = await occupy(d, adapters[A], repos[A])
        second = await occupy(d, adapters[B], repos[B])
        waiting = await d.submit(req(repos[A], executor=B, hop=1, parent_task_id=first.task_id))
        await wait_for_spawn(storage, waiting.task_id)
        adapters[B].started = asyncio.Event()
        await assert_queued(adapters[B])
        await d.cancel(waiting.task_id)

        adapters[A].started = asyncio.Event()
        reverse = await d.submit(req(repos[B], executor=A, hop=1, parent_task_id=second.task_id))
        await wait_for_spawn(storage, reverse.task_id)
        await assert_queued(adapters[A])
        assert (await storage.get_task(reverse.task_id)).status == "running"
        adapters[A].gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        assert (await d.wait(reverse.task_id, WAIT)).status == "completed"
        adapters[B].gate.set()
        assert (await d.wait(second.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_inherited_child_keeps_slot_after_parent_cli_returns(tmp_path):
    repo = directory(tmp_path, "repo")
    child_started = asyncio.Event()
    child_release = asyncio.Event()
    children = []
    async with lab(tmp_path) as (_, _, adapters, d):

        async def run(ctx):
            if not children:
                children.append(None)
                child = await d.submit(req(repo, executor=A, parent_task_id=ctx.task_id, hop=1))
                children[0] = child
                await child_started.wait()
            else:
                child_started.set()
                await child_release.wait()

        adapters[A].on_execute = run
        parent = await d.submit(req(repo, executor=A))
        assert (await d.wait(parent.task_id, WAIT)).status == "completed"
        adapters[A].started = asyncio.Event()
        other = await d.submit(req(directory(tmp_path, "other"), executor=A))
        await assert_queued(adapters[A])
        child_release.set()
        assert (await d.wait(children[0].task_id, WAIT)).status == "completed"
        assert (await d.wait(other.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_nested_self_review_fails_fast_when_parent_holds_only_slot(tmp_path):
    repo = directory(tmp_path, "repo")
    children = []
    async with lab(tmp_path) as (_, _, adapters, d):

        async def run(ctx):
            child = await d.submit(
                req(repo, executor=A, kind="review", hop=1, parent_task_id=ctx.task_id)
            )
            children.append(await d.wait(child.task_id, WAIT))

        adapters[A].on_execute = run
        parent = await d.submit(req(repo, executor=A))
        assert (await d.wait(parent.task_id, WAIT)).status == "completed"
        assert len(children) == 1 and children[0].status == "failed"
        assert "executor capacity deadlock" in children[0].result.error
        assert len(adapters[A].calls) == 1


@pytest.mark.asyncio
async def test_nested_self_review_waits_for_unrelated_holder(tmp_path):
    executors = {A: ExecutorSettings(adapter="omp", model="provider/a", max_concurrent=2)}
    executors[B] = ExecutorSettings(adapter="omp", model="other/b")
    async with lab(tmp_path, executors=executors) as (_, storage, adapters, d):
        unrelated = await occupy(d, adapters[A], directory(tmp_path, "unrelated"))
        release = adapters[A].gate
        adapters[A].gate = None
        review_started = asyncio.Event()
        child_submitted = asyncio.Event()
        children = []

        async def run(ctx):
            if ctx.cwd == str(tmp_path / "unrelated"):
                return
            if ctx.read_only:
                review_started.set()
                return
            child = await d.submit(
                req(
                    directory(tmp_path, "repo"),
                    executor=A,
                    kind="review",
                    hop=1,
                    parent_task_id=ctx.task_id,
                )
            )
            children.append(child)
            child_submitted.set()
            await review_started.wait()

        adapters[A].on_execute = run
        parent = await d.submit(req(directory(tmp_path, "repo"), executor=A))
        await asyncio.wait_for(child_submitted.wait(), WAIT)
        await wait_for_spawn(storage, children[0].task_id)
        await asyncio.sleep(QUEUE_WAIT)
        assert (await storage.get_task(children[0].task_id)).status == "running"
        assert not review_started.is_set()
        release.set()
        await asyncio.wait_for(review_started.wait(), WAIT)
        assert (await d.wait(children[0].task_id, WAIT)).status == "completed"
        assert (await d.wait(parent.task_id, WAIT)).status == "completed"
        assert (await d.wait(unrelated.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "same_group,holder_kind", [(False, "review"), (True, "review"), (False, "task")]
)
@pytest.mark.parametrize("wait_window", [0, 0.03])
async def test_executor_wait_does_not_pin_root_or_group_capacity(
    tmp_path, same_group, holder_kind, wait_window
):
    executors = {
        A: ExecutorSettings(adapter="omp", model="shared/a", max_concurrent=1),
        B: ExecutorSettings(adapter="omp", model="shared/b" if same_group else "other/b"),
    }
    async with lab(tmp_path, executors=executors, group_capacity=1) as (
        settings,
        storage,
        adapters,
        d,
    ):
        settings.routing.preferred_executor_max_wait_seconds = wait_window
        d._semaphore = asyncio.Semaphore(2 if holder_kind == "task" else 1)
        holder = await occupy(d, adapters[A], directory(tmp_path, "holder"), kind=holder_kind)
        queued = await d.submit(req(directory(tmp_path, "queued"), executor=A))
        await asyncio.sleep(QUEUE_WAIT)
        free = await d.submit(req(directory(tmp_path, "free"), executor=B))
        done = await d.wait(free.task_id, WAIT)
        assert done.status == "completed"
        assert (await storage.get_task(queued.task_id)).result is None
        adapters[A].gate.set()
        assert (await d.wait(holder.task_id, WAIT)).status == "completed"
        assert (await d.wait(queued.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_group", [False, True])
async def test_busy_executor_triggers_same_tier_backup_after_window(tmp_path, same_group):
    executors = {
        A: ExecutorSettings(adapter="omp", model="shared/a", max_concurrent=1),
        B: ExecutorSettings(adapter="omp", model="shared/b" if same_group else "other/b"),
    }
    async with lab(tmp_path, executors=executors) as (_, storage, adapters, d):
        holder = await occupy(d, adapters[A], directory(tmp_path, "holder"), kind="review")
        task = await d.submit(req(directory(tmp_path, "queued")))
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == B
        slots = [
            e["payload"]
            for e in await storage.list_events(task.task_id)
            if e["kind"] == "slot" and "queue_fallback" in e["payload"]
        ]
        assert slots[-1]["wait_seconds"] >= 0.03
        assert done.decision.meta["queue_fallback"]["preferred_executor"] == A
        adapters[A].gate.set()
        assert (await d.wait(holder.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_self_review_reacquires_slot_after_coding_cli_finishes(tmp_path, git_repo):
    executors = {
        A: ExecutorSettings(
            adapter="omp",
            model="corporate/a",
            corporate=True,
            max_concurrent=1,
            perimeter_review_by=A,
        ),
        B: ExecutorSettings(adapter="omp", model="other/b"),
    }
    async with lab(tmp_path, executors=executors, group_capacity=1) as (_, _, adapters, d):

        async def run(ctx):
            if ctx.read_only:
                adapters[A].result = ExecutionResult(
                    status="completed", executor=A, model=None, summary="reviewed", verdict="pass"
                )
            else:
                Path(ctx.cwd, "a.py").write_text("x = 2\n")  # noqa: ASYNC240
                adapters[A].result = ExecutionResult(
                    status="completed",
                    executor=A,
                    model=None,
                    summary="fixed",
                    changed_files=["a.py"],
                )

        adapters[A].on_execute = run
        task = await d.submit(req(git_repo, executor=A, prod=True))
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed"
        assert done.result.meta["review"]["verdict"] == "pass"
        assert [ctx.read_only for ctx in adapters[A].calls] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["holder", "waiter"])
async def test_cancellation_does_not_leak_or_overrelease_executor_slots(tmp_path, cancel):
    async with lab(tmp_path) as (_, _, adapters, d):
        adapter = adapters[A]
        first = await occupy(d, adapter, directory(tmp_path, "first"))
        adapter.started = asyncio.Event()
        second = await d.submit(req(directory(tmp_path, "second"), executor=A))
        await assert_queued(adapter)
        await d.cancel(first.task_id if cancel == "holder" else second.task_id)
        if cancel == "waiter":
            third = await d.submit(req(directory(tmp_path, "third"), executor=A))
            await assert_queued(adapter)
            adapter.gate.set()
            assert (await d.wait(first.task_id, WAIT)).status == "completed"
            assert (await d.wait(third.task_id, WAIT)).status == "completed"
        else:
            await asyncio.wait_for(adapter.started.wait(), WAIT)
            adapter.gate.set()
            assert (await d.wait(second.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_adapter_exception_releases_executor_capacity(tmp_path):
    async with lab(tmp_path) as (_, _, adapters, d):
        adapters[A].raise_exc = RuntimeError("broken CLI")
        first = await d.submit(req(directory(tmp_path, "first"), executor=A))
        failed = await d.wait(first.task_id, WAIT)
        assert failed.status == "failed" and "broken CLI" in failed.result.error
        adapters[A].raise_exc = None
        second = await d.submit(req(directory(tmp_path, "second"), executor=A))
        assert (await d.wait(second.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_queue_fallback_skips_executor_saturated_by_review(tmp_path):
    async with lab(tmp_path, group_capacity=1) as (_, _, adapters, d):
        first = await occupy(d, adapters[A], directory(tmp_path, "first"))
        review = await occupy(d, adapters[B], directory(tmp_path, "review"), kind="review")
        adapters[A].started = asyncio.Event()
        queued = await d.submit(req(directory(tmp_path, "queued")))
        await assert_queued(adapters[A])
        assert len(adapters[B].calls) == 1
        adapters[A].gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        done = await d.wait(queued.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == A
        assert "queue_fallback" not in done.decision.meta
        adapters[B].gate.set()
        assert (await d.wait(review.task_id, WAIT)).status == "completed"


@pytest.mark.asyncio
async def test_report_recovery_reacquires_executor_slot_for_its_cli(tmp_path, monkeypatch):
    first_dir = directory(tmp_path, "first")
    other_dir = directory(tmp_path, "other")
    first_started, first_release = asyncio.Event(), asyncio.Event()
    other_started, other_release = asyncio.Event(), asyncio.Event()
    report_started = asyncio.Event()
    async with lab(tmp_path) as (_, storage, adapters, d):

        async def run(ctx):
            if ctx.resume_session:
                report_started.set()
                adapters[A].result = ExecutionResult(
                    status="completed", executor=A, model=None, summary="recovered"
                )
            elif ctx.cwd == str(first_dir):
                first_started.set()
                await first_release.wait()
                adapters[A].result = ExecutionResult(
                    status="partial",
                    executor=A,
                    model=None,
                    summary="",
                    changed_files=["a.py"],
                    meta={"parse_error": "no result block", "session_id": "session"},
                )
            else:
                other_started.set()
                await other_release.wait()
                adapters[A].result = ExecutionResult(
                    status="completed", executor=A, model=None, summary="other"
                )

        adapters[A].on_execute = run
        first = await d.submit(req(first_dir, executor=A))
        await asyncio.wait_for(first_started.wait(), WAIT)
        adapters[A].started = asyncio.Event()
        waiting = observe_executor_wait(d, monkeypatch)
        other = await d.submit(req(other_dir, executor=A))
        await asyncio.wait_for(waiting.wait(), WAIT)
        await assert_queued(adapters[A])
        first_release.set()
        await asyncio.wait_for(other_started.wait(), WAIT)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(report_started.wait(), QUEUE_WAIT)
        other_release.set()
        assert (await d.wait(other.task_id, WAIT)).status == "completed"
        done = await d.wait(first.task_id, WAIT)
        assert done.status == "completed"
        assert done.result.meta["report_recovery"] == "recovered"
        assert [ctx.resume_session for ctx in adapters[A].calls] == [None, None, "session"]


@pytest.mark.asyncio
async def test_executor_unavailable_after_executor_wait_is_not_started(tmp_path):
    async with lab(tmp_path) as (_, _, adapters, d):
        first = await occupy(d, adapters[A], directory(tmp_path, "first"))
        adapters[A].started = asyncio.Event()
        queued = await d.submit(req(directory(tmp_path, "queued"), executor=A))
        await assert_queued(adapters[A])
        d.availability.trip(A, "quota", "test", 60)
        adapters[A].gate.set()
        assert (await d.wait(first.task_id, WAIT)).status == "completed"
        done = await d.wait(queued.task_id, WAIT)
        assert done.status == "failed"
        assert "became unavailable while waiting for a slot" in done.result.error
        assert len(adapters[A].calls) == 1


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("scope", ["executor", "group"])
def test_concurrency_limits_reject_values_below_one(scope, value):
    with pytest.raises(ValidationError):
        if scope == "executor":
            ExecutorSettings(adapter="claude", max_concurrent=value)
        else:
            LimitGroupSettings(max_concurrent=value)
