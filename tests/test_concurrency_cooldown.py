from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.health import classify_failure, omp_error
from agent_dispatch.executors.omp import OmpAdapter
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

FIXTURE = Path(__file__).parent / "fixtures" / "omp" / "x5_concurrency_429.ndjson"
BUSY = omp_error(FIXTURE.read_text())
WAIT = 5


async def _make(tmp_path, *, busy_seconds=120, failure_seconds=900):
    settings = Settings(
        server=ServerSettings(
            data_dir=tmp_path, max_concurrent_tasks=2, max_concurrent_per_group=1
        ),
        routing=RoutingSettings(
            fallback_executor="omp/large",
            corporate_perimeter=False,
            review_only={},
            busy_cooldown_seconds=busy_seconds,
            failure_cooldown_seconds=failure_seconds,
        ),
        executors={
            name: ExecutorSettings(adapter="omp", model=f"copilot-x5/{model}")
            for name, model in [("omp/code", "code"), ("omp/large", "large")]
        },
        escalation={"omp/code": ["omp/large"]},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    availability = AvailabilityCache(adapters, 60)
    dispatcher = Dispatcher(settings, storage, adapters, availability, [FakeRouter()])
    return settings, storage, adapters, availability, dispatcher


def _request(repo, executor="omp/code", **kwargs):
    return DispatchRequest(task="fix", cwd=str(repo), executor=executor, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BUSY, "Parallel processed requests limit exceed"])
@pytest.mark.parametrize("busy_seconds", [120, 17])
async def test_busy_cools_only_executor_and_persists_short_deadline(
    tmp_path, git_repo, error, busy_seconds
):
    _, storage, adapters, availability, dispatcher = await _make(
        tmp_path, busy_seconds=busy_seconds
    )
    adapters["omp/code"].result = ExecutionResult(
        status="failed", executor="omp/code", model=None, summary="", error=error
    )
    before = datetime.now(UTC)
    try:
        task = await dispatcher.submit(_request(git_repo, allow_escalation=False))
        done = await dispatcher.wait(task.task_id, WAIT)
        after = datetime.now(UTC)
        assert done.result.meta["executor_failure"] == "model_busy"
        assert done.result.meta["failure_class"] == "model_busy"
        assert set(availability.cooldowns()) == {"omp/code"}
        rows = await storage.active_cooldowns()
        assert len(rows) == 1 and rows[0]["executor"] == "omp/code"
        cooldown = availability.cooldown("omp/code")
        assert before + timedelta(seconds=busy_seconds) <= cooldown.until_utc
        assert cooldown.until_utc <= after + timedelta(seconds=busy_seconds)
        assert rows[0]["kind"] == "model_busy"
        neighbor = await dispatcher.submit(_request(git_repo, "omp/large"))
        assert (await dispatcher.wait(neighbor.task_id, WAIT)).status == "completed"
    finally:
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.asyncio
async def test_other_model_already_waiting_for_group_slot_still_runs(tmp_path, git_repo):
    _, storage, adapters, availability, dispatcher = await _make(tmp_path)
    adapters["omp/code"].gate = asyncio.Event()
    adapters["omp/code"].result = ExecutionResult(
        status="failed", executor="omp/code", model=None, summary="", error=BUSY
    )
    try:
        first = await dispatcher.submit(_request(git_repo, allow_escalation=False))
        await asyncio.wait_for(adapters["omp/code"].started.wait(), WAIT)
        waiting = await dispatcher.submit(_request(git_repo, "omp/large"))
        async with asyncio.timeout(WAIT):
            while (await storage.get_task(waiting.task_id)).decision is None:  # noqa: ASYNC110
                await asyncio.sleep(0)
        assert not adapters["omp/large"].started.is_set()
        adapters["omp/code"].gate.set()
        await dispatcher.wait(first.task_id, WAIT)
        done = await dispatcher.wait(waiting.task_id, WAIT)
        assert done.status == "completed"
        assert len(adapters["omp/large"].calls) == 1
        assert set(availability.cooldowns()) == {"omp/code"}
    finally:
        adapters["omp/code"].gate.set()
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [False, True])
async def test_real_cli_busy_error_escalates_only_without_changes(tmp_path, git_repo, changes):
    settings, storage, adapters, availability, dispatcher = await _make(tmp_path)
    cli = tmp_path / "busy-cli"
    cli.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "omp 18.16.0"; exit 0; fi\n'
        + ('printf "saved work\\n" > busy-work.txt\n' if changes else "")
        + f"cat {shlex.quote(str(FIXTURE))}\nexit 1\n"
    )
    cli.chmod(0o755)
    configured = settings.executors["omp/code"].model_copy(update={"command": str(cli)})
    adapters["omp/code"] = OmpAdapter("omp/code", configured, data_dir=tmp_path)
    try:
        submitted = await dispatcher.submit(_request(git_repo))
        final = await dispatcher.wait(submitted.task_id, WAIT)
        original = await storage.get_task(submitted.task_id)
        assert original.result.meta["executor_failure"] == "model_busy"
        assert set(availability.cooldowns()) == {"omp/code"}
        if changes:
            assert final.task_id == submitted.task_id and final.status == "partial"
            assert final.result.meta["cli_error"] is True
            assert final.result.changed_files == ["busy-work.txt"]
            assert (git_repo / "busy-work.txt").read_text() == "saved work\n"
            assert not adapters["omp/large"].calls
        else:
            assert original.status == "failed"
            assert final.task_id != submitted.task_id and final.status == "completed"
            assert final.decision.executor == "omp/large"
            events = await storage.list_events(submitted.task_id)
            assert any(
                event["kind"] == "escalate" and event["payload"]["reason"] == "executor_model_busy"
                for event in events
            )
    finally:
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.parametrize(
    "status,error", [("completed", BUSY), ("partial", BUSY), ("failed", "exit 1")]
)
def test_agent_busy_words_do_not_cool_executor(status, error):
    result = ExecutionResult(
        status=status,
        executor="omp/code",
        model=None,
        summary=BUSY,
        error=error,
    )
    assert classify_failure(result) is None


@pytest.mark.parametrize("busy_seconds,failure_seconds", [(0, 900), (120, 0)])
@pytest.mark.asyncio
async def test_cooldown_disable_switches(tmp_path, git_repo, busy_seconds, failure_seconds):
    _, storage, adapters, availability, dispatcher = await _make(
        tmp_path, busy_seconds=busy_seconds, failure_seconds=failure_seconds
    )
    adapters["omp/code"].result = ExecutionResult(
        status="failed", executor="omp/code", model=None, summary="", error=BUSY
    )
    try:
        submitted = await dispatcher.submit(_request(git_repo, allow_escalation=False))
        await dispatcher.wait(submitted.task_id, WAIT)
        assert availability.cooldowns() == {}
        assert await storage.active_cooldowns() == []
    finally:
        await dispatcher.shutdown()
        await storage.close()


def test_busy_cooldown_rejects_negative_seconds():
    with pytest.raises(ValidationError):
        RoutingSettings(busy_cooldown_seconds=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,recheck,seconds",
    [
        ("429 Too Many Requests", False, 3600),
        ("429 Too Many Requests; try again in 2 hours", False, 7260),
        ("429 Too Many Requests; try again in 2 hours", True, 3600),
        ("quota exceeded; try again in 2 hours", False, 7260),
    ],
)
async def test_other_quotas_keep_group_reset_and_recheck_behavior(
    tmp_path, git_repo, error, recheck, seconds
):
    settings, storage, adapters, availability, dispatcher = await _make(tmp_path)
    settings.executors["omp/code"].limit_reset = "recheck" if recheck else "announced"
    adapters["omp/code"].result = ExecutionResult(
        status="failed", executor="omp/code", model=None, summary="", error=error
    )
    before = datetime.now(UTC)
    try:
        submitted = await dispatcher.submit(_request(git_repo, allow_escalation=False))
        done = await dispatcher.wait(submitted.task_id, WAIT)
        after = datetime.now(UTC)
        assert done.result.meta["executor_failure"] == "quota"
        assert set(availability.cooldowns()) == {"omp/code", "omp/large"}
        for cooldown in availability.cooldowns().values():
            assert before + timedelta(seconds=seconds) <= cooldown.until_utc
            assert cooldown.until_utc <= after + timedelta(seconds=seconds)
    finally:
        await dispatcher.shutdown()
        await storage.close()
