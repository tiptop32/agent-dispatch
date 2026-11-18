import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_dispatch.dispatch.context_blocks import ContextSpan
from agent_dispatch.dispatch.context_compression import StepOutcome, StepStart
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    RouteDecision,
    TaskRecord,
    TaskStatus,
)
from agent_dispatch.telemetry import Storage


def make_task(
    *,
    task_id: str | None = None,
    parent: str | None = None,
    status: str = "queued",
    created: datetime | None = None,
    assigned: bool = False,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id or uuid4().hex,
        parent_task_id=parent,
        escalated_from=None,
        root_agent="cli",
        source_agent="cli",
        hop=0,
        request=DispatchRequest(task="x", cwd="/tmp"),
        status=status,
        decision=decision() if assigned else None,
        result=None,
        log_path="/tmp/x.log",
        created_at=created or datetime.now(UTC),
        started_at=None,
        finished_at=None,
    )


def decision() -> RouteDecision:
    return RouteDecision(executor="omp", confidence=0.9, scores={"omp": 0.9}, router="jev")


@pytest.fixture
async def storage(tmp_path: Path):
    db = Storage(tmp_path / "nested" / "telemetry.db")
    await db.open()
    yield db
    await db.close()


async def test_open_creates_db_schema_and_version(tmp_path: Path):
    db = Storage(tmp_path / "a" / "telemetry.db")
    await db.open()
    assert db.path.exists()
    assert db._db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    await db.close()


async def test_open_is_idempotent_and_preserves_data(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    await storage.open()
    assert await storage.get_task(task.task_id) == task


async def test_insert_get_round_trip(storage: Storage):
    task = make_task()
    task = task.model_copy(
        update={
            "decision": decision(),
            "result": ExecutionResult(status="completed", executor="omp", model="m", summary="ok"),
        }
    )
    await storage.insert_task(task)
    assert await storage.get_task(task.task_id) == task


async def test_update_changes_status_result_and_duration(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    start = datetime.now(UTC)
    finish = start + timedelta(milliseconds=125)
    updated = task.model_copy(
        update={
            # model_copy не валидирует: статус кладём уже как enum, иначе в базу
            # уедет сырая строка.
            "status": TaskStatus.completed,
            "result": ExecutionResult(
                status="completed", executor="omp", model=None, summary="done"
            ),
            "started_at": start,
            "finished_at": finish,
        }
    )
    await storage.update_task(updated)
    assert await storage.get_task(task.task_id) == updated
    assert (
        storage._db.execute(
            "SELECT duration_ms FROM tasks WHERE task_id=?", (task.task_id,)
        ).fetchone()[0]
        == 125
    )


async def test_update_missing_raises_key_error(storage: Storage):
    with pytest.raises(KeyError):
        await storage.update_task(make_task(task_id="missing"))


async def test_task_exists(storage: Storage):
    task = make_task()
    assert not await storage.task_exists(task.task_id)
    await storage.insert_task(task)
    assert await storage.task_exists(task.task_id)


async def test_count_children_excludes_unassigned_and_cancelled(storage: Storage):
    parent = make_task()
    await storage.insert_task(parent)
    assigned = make_task(parent=parent.task_id, assigned=True)
    await storage.insert_task(assigned)
    await storage.insert_task(make_task(parent=parent.task_id))
    await storage.insert_task(make_task(parent=parent.task_id, status="cancelled", assigned=True))
    assert await storage.count_children(parent.task_id) == 1
    assert await storage.count_children(parent.task_id, exclude_task_id=assigned.task_id) == 0


async def test_ancestors_chain_and_depth_cap(storage: Storage):
    previous = None
    ids = []
    for _ in range(40):
        task = make_task(parent=previous)
        await storage.insert_task(task)
        ids.append(task.task_id)
        previous = task.task_id
    ancestors = await storage.ancestors(ids[-1])
    assert ancestors == list(reversed(ids[-33:-1]))


async def test_list_tasks_since_and_limit(storage: Storage):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(3):
        await storage.insert_task(make_task(created=base + timedelta(days=i)))
    rows = await storage.list_tasks(since=base + timedelta(days=1), limit=1)
    assert len(rows) == 1 and rows[0].created_at == base + timedelta(days=2)


async def test_add_decision_addressed_and_unaddressed(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    first = await storage.add_decision(None, decision(), ["omp"])
    second = await storage.add_decision(task.task_id, decision(), ["omp", "claude"])
    assert first == 1 and second == 2


async def test_add_event_and_list_in_order(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    await storage.add_event(task.task_id, "spawn", {"n": 1})
    await storage.add_event(task.task_id, "exit", {"n": 2})
    events = await storage.list_events(task.task_id)
    assert [e["kind"] for e in events] == ["spawn", "exit"]
    assert events[0]["payload"] == {"n": 1}


async def test_export_includes_tasks_decisions_events_and_route_only(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    await storage.add_decision(task.task_id, decision(), ["omp"])
    await storage.add_event(task.task_id, "spawn", {"ok": True})
    await storage.add_decision(None, decision(), ["claude"])
    rows = [row async for row in storage.export()]
    assert len(rows) == 2
    assert rows[0]["task"]["task_id"] == task.task_id
    assert rows[0]["decision"]["executor"] == "omp"
    assert rows[0]["events"][0]["kind"] == "spawn"
    assert rows[1]["task"] is None


async def test_concurrent_events_are_not_lost(storage: Storage):
    task = make_task()
    await storage.insert_task(task)
    await asyncio.gather(*(storage.add_event(task.task_id, "guard", {"i": i}) for i in range(20)))
    assert len(await storage.list_events(task.task_id)) == 20


async def test_concurrent_inserts_are_not_lost(storage: Storage):
    tasks = [make_task() for _ in range(20)]
    await asyncio.gather(*(storage.insert_task(task) for task in tasks))
    assert len(await storage.list_tasks(limit=100)) == 20


async def test_count_children_can_exclude_escalation_retries(storage: Storage):
    parent = make_task()
    await storage.insert_task(parent)
    for escalated in (None, parent.task_id, None):
        child = make_task(parent=parent.task_id, assigned=True)
        child.escalated_from = escalated
        await storage.insert_task(child)
    await storage.insert_task(make_task(parent=parent.task_id, status="cancelled", assigned=True))
    assert await storage.count_children(parent.task_id) == 3
    assert await storage.count_children(parent.task_id, exclude_escalated=True) == 2


@pytest.mark.asyncio
async def test_cooldowns_survive_reopen_and_expire(tmp_path):
    from datetime import timedelta

    path = tmp_path / "db.sqlite"
    first = Storage(path)
    await first.open()
    now = datetime.now(UTC)
    await first.save_cooldown(
        "claude/opus",
        kind="quota",
        detail="weekly",
        origin="claude/opus",
        until=now + timedelta(hours=5),
        task_id="t1",
    )
    await first.save_cooldown(
        "claude/haiku",
        kind="quota",
        detail="weekly",
        origin="claude/opus",
        until=now + timedelta(hours=5),
    )
    await first.save_cooldown(
        "omp", kind="network", detail="x", origin="omp", until=now - timedelta(seconds=1)
    )
    await first.close()

    second = Storage(path)
    await second.open()
    rows = await second.active_cooldowns()
    assert [row["executor"] for row in rows] == ["claude/haiku", "claude/opus"]
    assert rows[1]["task_id"] == "t1" and rows[0]["origin"] == "claude/opus"
    # Повторное остывание того же исполнителя заменяет запись, а не дублирует.
    await second.save_cooldown(
        "claude/opus",
        kind="quota",
        detail="later",
        origin="claude/opus",
        until=now + timedelta(hours=1),
    )
    assert [r["detail"] for r in await second.active_cooldowns()] == ["weekly", "later"]
    await second.delete_cooldowns(["claude/haiku"])
    assert [r["executor"] for r in await second.active_cooldowns()] == ["claude/opus"]
    await second.delete_cooldowns()
    assert await second.active_cooldowns() == []
    await second.close()


def compression_start(index=0, started=None):
    return StepStart(index, f"b{index:04d}", ("b0000",), started or datetime.now(UTC), "jev")


def compression_outcome(index=0, *, cost=0.25, **updates):
    step = StepOutcome(
        index=index,
        candidate_id=f"b{index:04d}",
        status="succeeded",
        completed_at=datetime.now(UTC),
        latency_ms=10,
        jev_id="generation",
        response_model="observed",
        p_delete_safe=0.95,
        deleted=True,
        cost_usd=cost,
        input_tokens=10,
        output_tokens=2,
        is_byok=False,
    )
    return replace(step, **updates)


async def test_compression_ack_is_durable_before_async_lock_exit(storage):
    task = make_task()
    await storage.insert_task(task)
    exiting, release = asyncio.Event(), asyncio.Event()
    lock = storage._write_lock
    acknowledged = []

    class PausedExit:
        async def __aenter__(self):
            await lock.acquire()

        async def __aexit__(self, *args):
            exiting.set()
            await release.wait()
            lock.release()

    def acknowledge():
        assert lock.locked()
        with sqlite3.connect(storage.path) as connection:
            row = connection.execute("SELECT status FROM compression_steps").fetchone()
        assert row == ("started",)
        acknowledged.append(True)

    storage._write_lock = PausedExit()
    writer = asyncio.create_task(
        storage.start_compression_step(task.task_id, compression_start(), acknowledge=acknowledge)
    )
    await asyncio.wait_for(exiting.wait(), 1)
    assert acknowledged == [True] and not writer.done()
    release.set()
    await writer
    storage._write_lock = lock


async def test_compression_finalization_is_identical_only_and_never_reinserts(storage):
    task = make_task()
    await storage.insert_task(task)
    start = compression_start()
    await storage.start_compression_step(task.task_id, start, acknowledge=lambda: None)
    with pytest.raises(sqlite3.IntegrityError):
        await storage.start_compression_step(task.task_id, start, acknowledge=lambda: pytest.fail())
    terminal = compression_outcome()
    await storage.finish_compression_step(task.task_id, terminal)
    await storage.finish_compression_step(task.task_id, terminal)
    with pytest.raises(ValueError, match="Conflicting"):
        await storage.finish_compression_step(task.task_id, replace(terminal, cost_usd=0.5))
    with pytest.raises(KeyError):
        await storage.finish_compression_step(task.task_id, compression_outcome(1))
    assert await storage.tree_cost([task.task_id, task.task_id]) == 0.25
    assert len(await storage.compression_steps(task.task_id)) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"cost_usd": True},
        {"cost_usd": -1},
        {"cost_usd": float("inf")},
        {"p_delete_safe": float("nan")},
        {"p_delete_safe": 1.1},
        {"input_tokens": True},
        {"input_tokens": 9223372036854775808},
        {"output_tokens": -1},
        {"output_tokens": 1.5},
        {"deleted": 2},
        {"is_byok": "false"},
        {"latency_ms": -1},
    ],
)
async def test_compression_rejects_invalid_bindings_without_losing_pending(storage, updates):
    task = make_task()
    await storage.insert_task(task)
    await storage.start_compression_step(
        task.task_id, compression_start(), acknowledge=lambda: None
    )
    with pytest.raises(ValueError):
        await storage.finish_compression_step(task.task_id, compression_outcome(**updates))
    assert (await storage.compression_steps(task.task_id))[0]["status"] == "started"
    assert await storage.compression_cost_unknown(task_ids=[task.task_id])


async def test_compression_unknown_scopes_and_reported_zero(storage):
    task = make_task()
    await storage.insert_task(task)
    assert not await storage.compression_cost_unknown(task_ids=[])
    for kwargs in ({}, {"task_ids": [], "since": datetime.now(UTC)}):
        with pytest.raises(ValueError):
            await storage.compression_cost_unknown(**kwargs)
    await storage.start_compression_step(
        task.task_id, compression_start(), acknowledge=lambda: None
    )
    assert await storage.compression_cost_unknown(task_ids=[task.task_id])
    await storage.finish_compression_step(task.task_id, compression_outcome(cost=0))
    assert not await storage.compression_cost_unknown(task_ids=[task.task_id])
    assert await storage.tree_cost([task.task_id]) == 0


async def test_typed_private_provenance_survives_restart_without_public_wire_key(storage):
    task = make_task()
    task.request.context_compression = "jev"
    task.request.context = "caller\n\ndaemon"
    task.request._context_provenance = (
        ContextSpan(0, 6, "user", None),
        ContextSpan(6, 14, "daemon_review", "ancestor"),
    )
    await storage.insert_task(task)
    with sqlite3.connect(storage.path) as connection:
        raw = json.loads(connection.execute("SELECT request_json FROM tasks").fetchone()[0])
    assert raw["_context_provenance"]["version"] == 1
    assert len(raw["_context_provenance"]["context_sha256"]) == 64
    await storage.close()
    await storage.open()
    loaded = await storage.get_task(task.task_id)
    assert loaded.request._context_provenance == task.request._context_provenance
    assert "_context_provenance" not in loaded.model_dump_json()


@pytest.mark.parametrize("corruption", ["digest", "cover", "origin", "source", "version", "null"])
async def test_stored_provenance_is_validated_not_reclassified(storage, corruption):
    task = make_task()
    task.request.context_compression = "jev"
    task.request.context = "caller"
    task.request._context_provenance = (ContextSpan(0, 6, "user", None),)
    await storage.insert_task(task)
    raw = json.loads(storage._db.execute("SELECT request_json FROM tasks").fetchone()[0])
    value = raw["_context_provenance"]
    if corruption == "digest":
        raw["context"] = "edited"
    elif corruption == "cover":
        value["spans"][0]["end"] = 5
    elif corruption == "origin":
        value["spans"][0]["origin"] = "pinned"
    elif corruption == "source":
        value["spans"][0].update(origin="daemon_review", source_task_id="")
    elif corruption == "version":
        value["version"] = True
    else:
        raw["_context_provenance"] = None
    storage._db.execute("UPDATE tasks SET request_json=?", (json.dumps(raw),))
    storage._db.commit()
    loaded = await storage.get_task(task.task_id)
    assert loaded.request._context_provenance_invalid is True
    assert loaded.request._context_provenance is None
    loaded.status = TaskStatus.failed
    loaded.result = ExecutionResult(
        status="failed",
        executor="",
        model=None,
        summary="provenance refused",
        meta={"error_code": "compression_provenance", "executor_started": False},
    )
    await storage.update_task(loaded)
    preserved = json.loads(storage._db.execute("SELECT request_json FROM tasks").fetchone()[0])
    assert preserved == raw
    with pytest.raises(ValueError):
        await storage.insert_task(loaded.model_copy(update={"task_id": "new-invalid"}))
