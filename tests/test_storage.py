import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_dispatch.models import DispatchRequest, ExecutionResult, RouteDecision, TaskRecord
from agent_dispatch.telemetry import Storage


def make_task(
    *,
    task_id: str | None = None,
    parent: str | None = None,
    status: str = "queued",
    created: datetime | None = None,
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
        decision=None,
        result=None,
        log_path="/tmp/x.log",
        created_at=created or datetime.now(UTC),
        started_at=None,
        finished_at=None,
    )


def decision() -> RouteDecision:
    return RouteDecision(executor="codex", confidence=0.9, scores={"codex": 0.9}, router="jev")


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
            "result": ExecutionResult(
                status="completed", executor="codex", model="m", summary="ok"
            ),
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
            "status": "completed",
            "result": ExecutionResult(
                status="completed", executor="codex", model=None, summary="done"
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


async def test_count_children_excludes_cancelled(storage: Storage):
    parent = make_task()
    await storage.insert_task(parent)
    await storage.insert_task(make_task(parent=parent.task_id))
    await storage.insert_task(make_task(parent=parent.task_id, status="cancelled"))
    assert await storage.count_children(parent.task_id) == 1


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
    first = await storage.add_decision(None, decision(), ["codex"])
    second = await storage.add_decision(task.task_id, decision(), ["codex", "claude"])
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
    await storage.add_decision(task.task_id, decision(), ["codex"])
    await storage.add_event(task.task_id, "spawn", {"ok": True})
    await storage.add_decision(None, decision(), ["claude"])
    rows = [row async for row in storage.export()]
    assert len(rows) == 2
    assert rows[0]["task"]["task_id"] == task.task_id
    assert rows[0]["decision"]["executor"] == "codex"
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
