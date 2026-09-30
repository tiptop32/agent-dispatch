"""Прогон правил диспатчера по живой телеметрии: что изменилось бы на прошлых задачах.

Бесплатно и детерминированно: только чтение `dispatch.db`, без CLI и сети.
Меряет три вещи, каждая привязана к правилу в коде:

- остывание (`executors/health.py`): сколько запусков ушло к исполнителю, который
  уже упал на сбое, не связанном с задачей, и сколько минут они заняли;
- удержание интеграции (`Dispatcher._finish_worktree(hold=...)`): сколько раз
  недоделка ушла в рабочую копию перед эскалацией и чем кончилась интеграция
  следующего исполнителя;
- отчёт исполнителя (`result_parser._coerce`): сколько `partial` дал только
  формат отчёта (уверенность словами, `null` в необязательных полях).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent_dispatch.executors.health import classify_failure
from agent_dispatch.models import ExecutionResult

#: Ошибки валидации, которые `_coerce` теперь исправляет сам.
_FORMAT_ERRORS = ("is not of type 'number'", "None is not of type")


@dataclass
class Task:
    task_id: str
    executor: str
    status: str
    created_at: datetime
    finished_at: datetime | None
    duration_ms: int
    escalated_from: str | None
    result: ExecutionResult | None


def load(db_path: Path) -> list[Task]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select task_id, coalesce(executor, ''), status, created_at, finished_at,"
            " coalesce(duration_ms, 0), escalated_from, result_json"
            " from tasks order by created_at"
        ).fetchall()
    finally:
        conn.close()
    return [
        Task(
            task_id=row[0],
            executor=row[1],
            status=row[2],
            created_at=datetime.fromisoformat(row[3]),
            finished_at=datetime.fromisoformat(row[4]) if row[4] else None,
            duration_ms=row[5],
            escalated_from=row[6],
            result=ExecutionResult.model_validate_json(row[7]) if row[7] else None,
        )
        for row in rows
    ]


def replay(tasks: list[Task], cooldown_seconds: int = 900) -> dict:
    by_id = {task.task_id: task for task in tasks}
    failures: list[dict] = []
    avoidable: list[dict] = []
    # Исполнитель -> момент, до которого он остывал бы.
    down_until: dict[str, datetime] = {}
    for task in tasks:
        until = down_until.get(task.executor)
        if until is not None and task.created_at < until:
            avoidable.append(
                {
                    "task_id": task.task_id[:8],
                    "executor": task.executor,
                    "status": task.status,
                    "minutes": round(task.duration_ms / 60000, 1),
                }
            )
        kind = classify_failure(task.result) if task.result else None
        if kind is not None and task.finished_at is not None:
            failures.append({"task_id": task.task_id[:8], "executor": task.executor, "kind": kind})
            down_until[task.executor] = task.finished_at + timedelta(seconds=cooldown_seconds)

    held: list[dict] = []
    for child in tasks:
        parent = by_id.get(child.escalated_from or "")
        if parent is None or parent.result is None:
            continue
        if parent.result.changed_files and parent.result.meta.get("integrated") is True:
            child_meta = child.result.meta if child.result else {}
            held.append(
                {
                    "parent": parent.task_id[:8],
                    "child": child.task_id[:8],
                    "child_integrated": child_meta.get("integrated"),
                }
            )

    partial = [task for task in tasks if task.status == "partial" and task.result]
    format_only = [
        task.task_id[:8]
        for task in partial
        if any(error in task.result.meta.get("parse_error", "") for error in _FORMAT_ERRORS)
    ]
    return {
        "tasks": len(tasks),
        "cooldown_seconds": cooldown_seconds,
        "executor_failures": failures,
        "avoidable_runs": avoidable,
        "avoidable_minutes": round(sum(run["minutes"] for run in avoidable), 1),
        "integrations_to_hold": held,
        "integration_conflicts_prevented": sum(
            1 for row in held if row["child_integrated"] is False
        ),
        "partial": len(partial),
        "partial_from_report_format": format_only,
    }


def render(report: dict) -> str:
    lines = [
        f"tasks: {report['tasks']}",
        f"executor failures (would trip a {report['cooldown_seconds']}s cooldown): "
        f"{len(report['executor_failures'])}",
    ]
    lines += [
        f"  {row['task_id']} {row['executor']}: {row['kind']}"
        for row in report["executor_failures"]
    ]
    lines.append(
        f"runs sent to an executor that was already down: {len(report['avoidable_runs'])}, "
        f"{report['avoidable_minutes']} min"
    )
    lines += [
        f"  {row['task_id']} {row['executor']} {row['status']} {row['minutes']} min"
        for row in report["avoidable_runs"]
    ]
    lines.append(
        f"half-done work integrated before an escalation: {len(report['integrations_to_hold'])}, "
        f"next executor's integration failed: {report['integration_conflicts_prevented']}"
    )
    lines.append(
        f"partial results: {report['partial']}, "
        f"from report format only: {len(report['partial_from_report_format'])}"
    )
    return "\n".join(lines)
