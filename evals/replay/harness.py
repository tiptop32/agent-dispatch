"""Прогон правил диспатчера по живой телеметрии: что изменилось бы на прошлых задачах.

Бесплатно и детерминированно: только чтение `dispatch.db`, без CLI и сети.
Меряет четыре вещи, каждая привязана к правилу в коде:

- остывание (`executors/health.py`): сколько запусков ушло к исполнителю, который
  уже упал на сбое, не связанном с задачей, и сколько минут они заняли. Лимит
  держит исполнителя до объявленного сброса и выводит всю группу лимита;
- правило ревью (`routing.review_only`): сколько задач на правку источник отдал
  исполнителю, которому теперь вправе отдать только ревью, и сколько из них упало;
- удержание интеграции (`Dispatcher._finish_worktree(hold=...)`): сколько раз
  недоделка ушла в рабочую копию перед эскалацией и чем кончилась интеграция
  следующего исполнителя;
- отчёт исполнителя (`result_parser._coerce`): сколько `partial` дал только
  формат отчёта (уверенность словами, `null` в необязательных полях).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from agent_dispatch.executors.health import classify_failure, cooldown_seconds, failure_scope
from agent_dispatch.models import ExecutionResult

#: Ошибки валидации, которые `_coerce` теперь исправляет сам.
_FORMAT_ERRORS = ("is not of type 'number'", "None is not of type")

#: Отказ guard'а до запуска исполнителя (не сбой CLI): соседей слишком много,
#: исполнитель недоступен или его выводил из ротации лимит. У таких задач
#: исполнитель пустой или стоит `meta.guard`, а ошибка повторяет текст guard'а.
_GUARD_ERROR_MARKERS = ("sibling count", "executor is unavailable", "after quota")


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
    source_agent: str = "unknown"
    kind: str = "task"
    started_at: datetime | None = None


def guard_refusal(task: Task) -> bool:
    """Задача, которую демон не запускал: guard отклонил её до исполнителя."""
    if not task.executor:
        return True
    meta = task.result.meta if task.result else {}
    if meta.get("guard"):
        return True
    error = (task.result.error or "") if task.result else ""
    return any(marker in error for marker in _GUARD_ERROR_MARKERS)


def load(db_path: Path) -> list[Task]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select task_id, coalesce(executor, ''), status, created_at, started_at, finished_at,"
            " coalesce(duration_ms, 0), escalated_from, result_json, source_agent,"
            " coalesce(json_extract(request_json, '$.kind'), 'task')"
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
            started_at=datetime.fromisoformat(row[4]) if row[4] else None,
            finished_at=datetime.fromisoformat(row[5]) if row[5] else None,
            duration_ms=row[6],
            escalated_from=row[7],
            result=ExecutionResult.model_validate_json(row[8]) if row[8] else None,
            source_agent=row[9],
            kind=row[10],
        )
        for row in rows
    ]


def default_limit_group(executor: str) -> str:
    """Группа лимита без конфига: у claude/* и codex/* один аккаунт на все модели."""
    adapter = executor.split("/", 1)[0]
    return adapter if adapter in {"claude", "codex"} else executor


def _window(times: list[datetime]) -> dict | None:
    """Первый и последний случай секции: старые проблемы не читаются как живые."""
    if not times:
        return None
    return {
        "first": min(times).isoformat(timespec="seconds"),
        "last": max(times).isoformat(timespec="seconds"),
    }


def replay(
    tasks: list[Task],
    cooldown: int = 900,
    *,
    quota_cooldown: int = 3600,
    limit_group: Callable[[str], str] = default_limit_group,
    review_only: dict[str, list[str]] | None = None,
    local_tz: tzinfo | None = None,
    recheck: Callable[[str], bool] = lambda _executor: False,
    since: datetime | None = None,
) -> dict:
    if since is not None:
        tasks = [task for task in tasks if task.created_at >= since]
    by_id = {task.task_id: task for task in tasks}
    review_only = {"codex": ["claude"]} if review_only is None else review_only
    failures: list[dict] = []
    failure_times: list[datetime] = []
    # Остывания сбоев: (ключ, когда сбой кончился, до какого момента остывание).
    # Момент сравнения берётся от запуска задачи, а не от её создания.
    cooldowns: list[tuple[str, datetime, datetime]] = []
    for task in tasks:
        if guard_refusal(task):
            continue
        kind = classify_failure(task.result) if task.result else None
        if kind is not None and task.finished_at is not None:
            error = task.result.error or ""
            seconds = cooldown_seconds(
                kind,
                error,
                task.finished_at,
                failure_seconds=cooldown,
                quota_seconds=quota_cooldown,
                local_tz=local_tz,
                recheck=recheck(task.executor),
            )
            failures.append(
                {
                    "task_id": task.task_id[:8],
                    "executor": task.executor,
                    "kind": kind,
                    "hours": round(seconds / 3600, 2),
                }
            )
            failure_times.append(task.finished_at)
            key = (
                limit_group(task.executor)
                if failure_scope(kind, error) == "group"
                else f"executor:{task.executor}"
            )
            cooldowns.append((key, task.finished_at, task.finished_at + timedelta(seconds=seconds)))

    avoidable: list[dict] = []
    avoidable_times: list[datetime] = []
    for task in tasks:
        # Никогда не запущенную задачу и отказ guard'а сопоставлять с остыванием
        # нечем: исполнитель не выбирался и не тратил минуты.
        if guard_refusal(task) or task.started_at is None:
            continue
        keys = (limit_group(task.executor), f"executor:{task.executor}")
        until = max(
            (
                moment
                for key, finished, moment in cooldowns
                if key in keys and finished <= task.started_at
            ),
            default=None,
        )
        if until is not None and task.started_at < until:
            avoidable.append(
                {
                    "task_id": task.task_id[:8],
                    "executor": task.executor,
                    "status": task.status,
                    "minutes": round(task.duration_ms / 60000, 1),
                }
            )
            avoidable_times.append(task.started_at)

    restricted = [
        task
        for task in tasks
        if task.kind == "task"
        and task.executor.split("/", 1)[0] in review_only.get(task.source_agent, [])
    ]

    held: list[dict] = []
    held_times: list[datetime] = []
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
            if parent.finished_at is not None:
                held_times.append(parent.finished_at)

    partial = [task for task in tasks if task.status == "partial" and task.result]
    format_only_tasks = [
        task
        for task in partial
        if any(error in task.result.meta.get("parse_error", "") for error in _FORMAT_ERRORS)
    ]
    format_only = [task.task_id[:8] for task in format_only_tasks]
    return {
        "tasks": len(tasks),
        "cooldown_seconds": cooldown,
        "quota_cooldown_seconds": quota_cooldown,
        "executor_failures": failures,
        "avoidable_runs": avoidable,
        "avoidable_minutes": round(sum(run["minutes"] for run in avoidable), 1),
        "integrations_to_hold": held,
        "integration_conflicts_prevented": sum(
            1 for row in held if row["child_integrated"] is False
        ),
        "partial": len(partial),
        "partial_from_report_format": format_only,
        "review_only_runs": len(restricted),
        "review_only_failed": sum(1 for task in restricted if task.status == "failed"),
        "review_only_minutes": round(sum(task.duration_ms for task in restricted) / 60000, 1),
        "section_windows": {
            "executor_failures": _window(failure_times),
            "avoidable_runs": _window(avoidable_times),
            "integrations_to_hold": _window(held_times),
            "partial_from_report_format": _window([task.created_at for task in format_only_tasks]),
            "review_only_runs": _window([task.created_at for task in restricted]),
        },
    }


def render(report: dict) -> str:
    windows = report.get("section_windows", {})

    def window(key: str) -> str:
        info = windows.get(key)
        if not info:
            return ""
        return f" (cases {info['first']} … {info['last']})"

    lines = [
        f"tasks: {report['tasks']}",
        f"executor failures (cooldown {report['cooldown_seconds']}s, quota until reset or "
        f"{report['quota_cooldown_seconds']}s): {len(report['executor_failures'])}"
        + window("executor_failures"),
    ]
    lines += [
        f"  {row['task_id']} {row['executor']}: {row['kind']} for {row['hours']} h"
        for row in report["executor_failures"]
    ]
    lines.append(
        f"runs sent to an executor that was already down: {len(report['avoidable_runs'])}, "
        f"{report['avoidable_minutes']} min" + window("avoidable_runs")
    )
    lines += [
        f"  {row['task_id']} {row['executor']} {row['status']} {row['minutes']} min"
        for row in report["avoidable_runs"]
    ]
    lines.append(
        f"half-done work integrated before an escalation: {len(report['integrations_to_hold'])}, "
        f"next executor's integration failed: {report['integration_conflicts_prevented']}"
        + window("integrations_to_hold")
    )
    lines.append(
        f"partial results: {report['partial']}, "
        f"from report format only: {len(report['partial_from_report_format'])}"
        + window("partial_from_report_format")
    )
    lines.append(
        f"work given to a review-only executor: {report['review_only_runs']} runs, "
        f"{report['review_only_failed']} failed, {report['review_only_minutes']} min"
        + window("review_only_runs")
    )
    return "\n".join(lines)
