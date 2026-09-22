from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_dispatch.models import DispatchRequest, ExecutionResult, RouteDecision, TaskRecord


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("storage is not open")
        return self._conn

    async def open(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        schema = (Path(__file__).with_name("schema.sql")).read_text()
        conn.executescript(schema)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _task_values(task: TaskRecord) -> tuple[Any, ...]:
        decision = task.decision
        result = task.result
        return (
            task.task_id,
            task.parent_task_id,
            task.escalated_from,
            getattr(task.root_agent, "value", task.root_agent),
            getattr(task.source_agent, "value", task.source_agent),
            task.hop,
            task.request.cwd,
            getattr(task.status, "value", task.status),
            decision.executor if decision else None,
            result.model if result else None,
            task.request.model_dump_json(),
            decision.model_dump_json() if decision else None,
            result.model_dump_json() if result else None,
            task.log_path,
            _dt(task.created_at),
            _dt(task.started_at),
            _dt(task.finished_at),
            None,
        )

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
        decision = (
            RouteDecision.model_validate_json(row["decision_json"])
            if row["decision_json"]
            else None
        )
        result = (
            ExecutionResult.model_validate_json(row["result_json"]) if row["result_json"] else None
        )
        return TaskRecord(
            task_id=row["task_id"],
            parent_task_id=row["parent_task_id"],
            escalated_from=row["escalated_from"],
            root_agent=row["root_agent"],
            source_agent=row["source_agent"],
            hop=row["hop"],
            request=DispatchRequest.model_validate_json(row["request_json"]),
            status=row["status"],
            decision=decision,
            result=result,
            log_path=row["log_path"],
            created_at=_parse_dt(row["created_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
        )

    async def insert_task(self, task: TaskRecord) -> None:
        async with self._write_lock:
            self._db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._task_values(task),
            )
            self._db.commit()

    async def update_task(self, task: TaskRecord) -> None:
        values = self._task_values(task)
        duration = None
        if task.started_at and task.finished_at:
            duration = max(0, int((task.finished_at - task.started_at).total_seconds() * 1000))
        async with self._write_lock:
            cur = self._db.execute(
                "UPDATE tasks SET parent_task_id=?, escalated_from=?, root_agent=?, "
                "source_agent=?, "
                "hop=?, cwd=?, status=?, executor=?, model=?, request_json=?, decision_json=?, "
                "result_json=?, log_path=?, created_at=?, started_at=?, finished_at=?, "
                "duration_ms=? WHERE task_id=?",
                values[1:17] + (duration, values[0]),
            )
            if cur.rowcount == 0:
                self._db.rollback()
                raise KeyError(task.task_id)
            self._db.commit()

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    async def task_exists(self, task_id: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            is not None
        )

    async def count_children(self, parent_task_id: str, exclude_escalated: bool = False) -> int:
        """Дети родителя без cancelled; с exclude_escalated не считаются ретраи эскалации."""
        query = "SELECT COUNT(*) FROM tasks WHERE parent_task_id=? AND status != 'cancelled'"
        if exclude_escalated:
            query += " AND escalated_from IS NULL"
        row = self._db.execute(query, (parent_task_id,)).fetchone()
        return int(row[0])

    async def ancestors(self, task_id: str) -> list[str]:
        result: list[str] = []
        current = task_id
        for _ in range(32):
            row = self._db.execute(
                "SELECT parent_task_id FROM tasks WHERE task_id=?", (current,)
            ).fetchone()
            if not row or row[0] is None:
                break
            result.append(row[0])
            current = row[0]
        return result

    async def list_tasks(self, since: datetime | None = None, limit: int = 100) -> list[TaskRecord]:
        if since is None:
            rows = self._db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (_dt(since), limit),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    async def add_decision(
        self, task_id: str | None, decision: RouteDecision, candidates: list[str]
    ) -> int:
        async with self._write_lock:
            cur = self._db.execute(
                "INSERT INTO routing_decisions(task_id,router,candidates_json,choice,confidence,"
                "scores_json,judgments_json,guard_reason,latency_ms,cost_usd,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    decision.router.value,
                    json.dumps(candidates),
                    decision.executor,
                    decision.confidence,
                    json.dumps(decision.scores),
                    json.dumps(
                        {k: v.model_dump(mode="json") for k, v in decision.judgments.items()}
                    ),
                    decision.reason.value if decision.reason else None,
                    decision.latency_ms,
                    decision.cost_usd,
                    _dt(datetime.now(UTC)),
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

    async def add_event(self, task_id: str, kind: str, payload: dict) -> int:
        async with self._write_lock:
            cur = self._db.execute(
                "INSERT INTO events(task_id,ts,kind,payload_json) VALUES (?,?,?,?)",
                (task_id, _dt(datetime.now(UTC)), kind, json.dumps(payload)),
            )
            self._db.commit()
            return int(cur.lastrowid)

    async def list_events(self, task_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id,task_id,ts,kind,payload_json FROM events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "ts": row["ts"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    async def export(self, since: datetime | None = None) -> AsyncIterator[dict]:
        tasks = await self.list_tasks(since=since, limit=2**31 - 1)
        for task in reversed(tasks):
            decision = task.decision
            if decision is None:
                row = self._db.execute(
                    "SELECT * FROM routing_decisions WHERE task_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (task.task_id,),
                ).fetchone()
                decision = self._decision_from_row(row) if row else None
            yield {
                "task": task.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json") if decision else None,
                "events": await self.list_events(task.task_id),
            }
        rows = self._db.execute(
            "SELECT * FROM routing_decisions WHERE task_id IS NULL"
            + (" AND created_at >= ?" if since else "")
            + " ORDER BY created_at",
            ((_dt(since),) if since else ()),
        ).fetchall()
        for row in rows:
            yield {
                "task": None,
                "decision": self._decision_from_row(row).model_dump(mode="json"),
                "events": [],
            }

    def _decision_from_row(self, row: sqlite3.Row) -> RouteDecision:
        return RouteDecision(
            executor=row["choice"],
            confidence=row["confidence"],
            scores=json.loads(row["scores_json"]),
            router=row["router"],
            reason=row["guard_reason"],
            judgments={k: v for k, v in json.loads(row["judgments_json"]).items()},
            latency_ms=row["latency_ms"],
            cost_usd=row["cost_usd"],
        )
