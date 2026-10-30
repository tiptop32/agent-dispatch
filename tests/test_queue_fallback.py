"""Queue policy at the Dispatcher boundary, with real guards and gated adapters."""

import asyncio
import shutil
from contextlib import asynccontextmanager

import pytest

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, Judgment, RouteDecision, RouterKind
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

PREFERRED = "opencode/x5"
BACKUP = "codex/sol"
WAIT = 3
THRESHOLD = 0.03


@asynccontextmanager
async def lab(tmp_path, **routing):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path, max_concurrent_per_group=1),
        routing=RoutingSettings(
            fallback_executor=[PREFERRED, BACKUP],
            preferred_executor_max_wait_seconds=THRESHOLD,
            **routing,
        ),
        execution=ExecutionSettings(workspace_mode="in_place"),
        executors={
            PREFERRED: ExecutorSettings(adapter="opencode", model="copilot/x5", corporate=True),
            BACKUP: ExecutorSettings(adapter="codex"),
        },
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    try:
        yield settings, storage, adapters, dispatcher
    finally:
        await dispatcher.shutdown()
        await storage.close()


def req(repo, **kwargs):
    return DispatchRequest(
        task="fix tests", cwd=str(repo), source_agent="cli", allow_escalation=False, **kwargs
    )


async def occupy(dispatcher, adapter, repo, executor):
    adapter.gate = asyncio.Event()
    task = await dispatcher.submit(req(repo, executor=executor))
    await asyncio.wait_for(adapter.started.wait(), WAIT)
    return task


@pytest.mark.asyncio
async def test_saturated_preferred_switches_once_and_persists_actual_executor(tmp_path, git_repo):
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    async with lab(tmp_path, corporate_perimeter=False) as (_, storage, adapters, d):
        await occupy(d, adapters[PREFERRED], git_repo, PREFERRED)
        task = await d.submit(req(other))
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed"
        assert done.result.executor == done.decision.executor == BACKUP
        assert len(adapters[PREFERRED].calls) == len(adapters[BACKUP].calls) == 1
        slot = next(
            e["payload"] for e in await storage.list_events(task.task_id) if e["kind"] == "slot"
        )
        assert slot["queue_fallback"] is True
        assert slot["preferred_executor"] == PREFERRED and slot["executor"] == BACKUP
        assert slot["switch_reason"] == "preferred_group_timeout"
        assert slot["wait_seconds"] >= THRESHOLD
        assert done.decision.meta["queue_fallback"]["preferred_executor"] == PREFERRED


@pytest.mark.asyncio
async def test_free_preferred_keeps_priority(tmp_path, git_repo):
    async with lab(tmp_path, corporate_perimeter=False) as (_, storage, adapters, d):
        task = await d.submit(req(git_repo))
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == PREFERRED
        assert not adapters[BACKUP].calls
        slot = next(
            e["payload"] for e in await storage.list_events(task.task_id) if e["kind"] == "slot"
        )
        assert slot["queue_fallback"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction",
    [
        "perimeter",
        "tier",
        "source",
        "review_only",
        "disabled",
        "cooldown",
        "same_group",
        "override",
        "zero",
        "busy",
    ],
)
async def test_queue_fallback_never_bypasses_restrictions(tmp_path, git_repo, restriction):
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    async with lab(tmp_path, corporate_perimeter=restriction == "perimeter") as (
        settings,
        _,
        adapters,
        d,
    ):
        await occupy(d, adapters[PREFERRED], git_repo, PREFERRED)
        kwargs = {}
        if restriction == "tier":
            settings.executors[BACKUP].tier = "strong"
        elif restriction == "source":
            kwargs["source_agent"] = "codex"
        elif restriction == "review_only":
            settings.routing.review_only = {"cli": ["codex"]}
        elif restriction == "disabled":
            settings.executors[BACKUP].enabled = False
        elif restriction == "cooldown":
            d.availability.trip(BACKUP, "quota", "test", 60)
        elif restriction == "same_group":
            settings.executors[BACKUP].limit_group = settings.limit_group(PREFERRED)
        elif restriction == "override":
            kwargs["executor"] = PREFERRED
        elif restriction == "zero":
            settings.routing.preferred_executor_max_wait_seconds = 0
        elif restriction == "busy":
            third = tmp_path / "third"
            shutil.copytree(git_repo, third)
            await occupy(d, adapters[BACKUP], third, BACKUP)
        task = await d.submit(
            DispatchRequest(
                task="fix tests",
                cwd=str(other),
                source_agent=kwargs.pop("source_agent", "cli"),
                allow_escalation=False,
                **kwargs,
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(d.wait(task.task_id, WAIT), THRESHOLD * 4)
        assert len(adapters[BACKUP].calls) == (1 if restriction == "busy" else 0)
        adapters[PREFERRED].gate.set()
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == PREFERRED


@pytest.mark.asyncio
async def test_backup_cooling_down_during_wait_is_skipped(tmp_path, git_repo):
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    async with lab(tmp_path, corporate_perimeter=False) as (settings, storage, adapters, d):
        settings.routing.preferred_executor_max_wait_seconds = 0.15
        await occupy(d, adapters[PREFERRED], git_repo, PREFERRED)
        task = await d.submit(req(other))
        async with asyncio.timeout(WAIT):
            while (await storage.get_task(task.task_id)).decision is None:  # noqa: ASYNC110
                await asyncio.sleep(0)
        d.availability.trip(BACKUP, "quota", "test", 60)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapters[BACKUP].started.wait(), 0.25)
        adapters[PREFERRED].gate.set()
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == PREFERRED


@pytest.mark.asyncio
async def test_cancelling_preference_wait_does_not_leak_capacity(tmp_path, git_repo):
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    async with lab(tmp_path, corporate_perimeter=False) as (settings, storage, adapters, d):
        settings.routing.preferred_executor_max_wait_seconds = 10
        first = await occupy(d, adapters[PREFERRED], git_repo, PREFERRED)
        task = await d.submit(req(other))
        async with asyncio.timeout(WAIT):
            while (await storage.get_task(task.task_id)).decision is None:  # noqa: ASYNC110
                await asyncio.sleep(0)
        await d.cancel(task.task_id)
        assert not adapters[BACKUP].calls
        adapters[PREFERRED].gate.set()
        await d.wait(first.task_id, WAIT)
        next_task = await d.submit(req(other))
        done = await d.wait(next_task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == PREFERRED


@pytest.mark.asyncio
async def test_judgment_choice_keeps_reasoning_adapter(tmp_path, git_repo):
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    async with lab(tmp_path, corporate_perimeter=False) as (settings, _, adapters, d):
        settings.executors[PREFERRED].adapter = "claude"
        d.routers = [
            FakeRouter(
                RouteDecision(
                    executor=PREFERRED,
                    confidence=1,
                    scores={PREFERRED: 1},
                    router=RouterKind.jev,
                    judgments={
                        "judgment": Judgment(
                            kind="noul", value=True, confidence=1, probabilities={"yes": 1}
                        )
                    },
                )
            )
        ]
        await occupy(d, adapters[PREFERRED], git_repo, PREFERRED)
        task = await d.submit(req(other))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapters[BACKUP].started.wait(), THRESHOLD * 4)
        adapters[PREFERRED].gate.set()
        done = await d.wait(task.task_id, WAIT)
        assert done.status == "completed" and done.result.executor == PREFERRED
