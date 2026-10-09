"""Прогон правил диспатчера по живой телеметрии: что изменилось бы на прошлых задачах.

Бесплатно и детерминированно: только чтение `dispatch.db`, без CLI и сети
(`acceptance` по умолчанию читает ещё `git log` рабочих копий вызывающего).
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

И три вещи о пользе самой утилиты:

- `delegations`: сколько исходных задач (с цепочкой эскалаций) не дали ничего,
  ни `completed`, ни изменённых файлов, сколько минут на них ушло и почему.
  Вызывающий потратил контекст на отправку и ожидание и сделал работу сам;
- `acceptance`: по скольким дифам вызывающий потом закоммитил хотя бы один
  из `changed_files` и какая доля значимых строк патча дожила до этого коммита.
  Сам факт коммита это верхняя оценка: коммит мог переписать работу;
- `router_vs_static`: как часто выбор Jev совпал бы со статическим правилом
  (один тир для всех, без вопроса о суждении, тот же периметр) на текущем
  конфиге и чем кончились задачи там, где они расходятся.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.health import classify_failure, cooldown_seconds, failure_scope
from agent_dispatch.models import ExecutionResult
from agent_dispatch.routing.capability import has_corporate, select

#: Ошибки валидации, которые `_coerce` теперь исправляет сам.
_FORMAT_ERRORS = ("is not of type 'number'", "None is not of type")

#: Отказ guard'а до запуска исполнителя (не сбой CLI): соседей слишком много,
#: исполнитель недоступен или его выводил из ротации лимит. У таких задач
#: исполнитель пустой или стоит `meta.guard`, а ошибка повторяет текст guard'а.
_GUARD_ERROR_MARKERS = ("sibling count", "executor is unavailable", "after quota")

#: Тема служебного коммита демона на ветке задачи (`Dispatcher`, worktree-режим):
#: это не решение вызывающего принять диф.
_DAEMON_COMMIT = "^agent-dispatch: task "

#: Статусы задачи, которая ещё не кончилась: её цепочку судить рано.
_UNFINISHED = ("queued", "running")


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
    cwd: str = ""
    parent_task_id: str | None = None


@dataclass
class Decision:
    task_id: str
    router: str
    candidates: list[str]
    choice: str
    guard_reason: str | None
    judgments: dict


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
            " coalesce(json_extract(request_json, '$.kind'), 'task'), cwd, parent_task_id"
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
            cwd=row[11],
            parent_task_id=row[12],
        )
        for row in rows
    ]


def load_decisions(db_path: Path) -> list[Decision]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select coalesce(task_id, ''), router, candidates_json, choice, guard_reason,"
            " judgments_json from routing_decisions order by id"
        ).fetchall()
    finally:
        conn.close()
    return [
        Decision(
            task_id=row[0],
            router=row[1],
            candidates=json.loads(row[2]),
            choice=row[3],
            guard_reason=row[4],
            judgments=json.loads(row[5]),
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


def _chains(tasks: list[Task]) -> list[list[Task]]:
    """Исходные задачи с их цепочками эскалаций, в порядке запуска звеньев.

    Корень это задача без `escalated_from` или с родителем вне выборки (`since`).
    """
    ids = {task.task_id for task in tasks}
    children: dict[str, list[Task]] = {}
    for task in tasks:
        if task.escalated_from:
            children.setdefault(task.escalated_from, []).append(task)
    chains = []
    for root in tasks:
        if root.escalated_from and root.escalated_from in ids:
            continue
        chain, queue = [], [root]
        while queue:
            task = queue.pop(0)
            chain.append(task)
            queue.extend(children.get(task.task_id, []))
        chains.append(chain)
    return chains


def _has_work(task: Task) -> bool:
    return task.status in ("completed", "partial") or bool(
        task.result and task.result.changed_files
    )


def _waste_cause(task: Task) -> str:
    """Почему последнее звено цепочки ничего не дало."""
    if task.status in ("cancelled", "needs_context", "needs_escalation"):
        return task.status
    if guard_refusal(task):
        return "guard"
    if task.result is None:
        return "no_result"
    kind = classify_failure(task.result)
    if kind is not None:
        return kind
    error = task.result.error or ""
    if error == "timeout" or error.startswith("stalled:"):
        return "timeout"
    if task.result.meta.get("parse_error"):
        return "no_report"
    return "task_failed"


def children_refusals(tasks: list[Task], max_children: int = 2) -> dict:
    """Отказы `max_children` в прошлом, пересчитанные по сегодняшнему правилу.

    Сегодня (`Storage.count_children`) квоту занимают только дети родителя с
    назначенным исполнителем, не отменённые и не ретраи эскалации; сама задача
    и отказанные попытки не в счёт. До 2026-10-09 счётчик включал их, и лимит
    2 пускал одного ребёнка. Ретрай отказа после допуска первой попытки не
    случился бы, поэтому из отказов одного родителя допускается не больше, чем
    осталось свободных мест.
    """
    by_parent: dict[str, list[Task]] = {}
    for task in tasks:
        if task.parent_task_id:
            by_parent.setdefault(task.parent_task_id, []).append(task)
    refused = admitted = 0
    parents: list[dict] = []
    for parent, children in by_parent.items():
        refusals = [
            task
            for task in children
            if (task.result and task.result.error or "").startswith("sibling count")
            or (task.result and task.result.meta.get("guard") == "max_children")
        ]
        if not refusals:
            continue
        occupied = sum(
            1
            for task in children
            if task.executor
            and task.status != "cancelled"
            and task.escalated_from is None
            and task not in refusals
        )
        free = max(0, max_children - occupied)
        refused += len(refusals)
        admitted += min(free, len(refusals))
        parents.append(
            {
                "parent": parent[:8],
                "children": occupied,
                "refused": len(refusals),
                "admitted_now": min(free, len(refusals)),
            }
        )
    return {
        "max_children": max_children,
        "refused": refused,
        "admitted_now": admitted,
        "retries_avoided": refused - admitted,
        "parents": parents,
    }


def delegations(tasks: list[Task]) -> dict:
    """Сколько исходных задач дошли до результата, а сколько пропали впустую."""
    outcome: Counter[str] = Counter()
    causes: Counter[str] = Counter()
    wasted_ms = 0
    for chain in _chains(tasks):
        if any(task.status in _UNFINISHED for task in chain):
            outcome["in_flight"] += 1
        elif any(task.status == "completed" for task in chain):
            outcome["completed"] += 1
        elif any(_has_work(task) for task in chain):
            outcome["work"] += 1
        else:
            outcome["wasted"] += 1
            causes[_waste_cause(chain[-1])] += 1
            wasted_ms += sum(task.duration_ms for task in chain)
    finished = outcome["completed"] + outcome["work"] + outcome["wasted"]
    return {
        "roots": finished,
        "in_flight": outcome["in_flight"],
        "completed": outcome["completed"],
        "work_only": outcome["work"],
        "wasted": outcome["wasted"],
        "wasted_share": round(outcome["wasted"] / finished, 3) if finished else 0.0,
        "wasted_minutes": round(wasted_ms / 60000, 1),
        "wasted_by_cause": dict(causes.most_common()),
    }


@dataclass
class Acceptance:
    """Что стало с дифом исполнителя в рабочей копии вызывающего.

    `added` это значимые добавленные строки патча, `survived` из них те, что
    есть в файлах на первом коммите вызывающего. Без патча оба равны нулю.
    """

    committed: bool
    added: int = 0
    survived: int = 0


#: Строка короче этого не доказывает авторство: `}`, `)`, `else:`, `pass`.
_MIN_LINE = 8


def added_lines(patch: str) -> dict[str, list[str]]:
    """Значимые добавленные строки unified diff по файлам (без пробелов по краям)."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else files.setdefault(path[2:], [])
        elif line.startswith("diff --git"):
            current = None
        elif current is not None and line.startswith("+"):
            text = line[1:].strip()
            if len(text) >= _MIN_LINE:
                current.append(text)
    return files


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Без GIT_*: внутри git-хука GIT_DIR и GIT_INDEX_FILE перебили бы `-C cwd`.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, env=env
    )


def git_acceptance(
    cwd: str, files: list[str], patch: str | None, since: datetime, until: datetime
) -> Acceptance | None:
    """Коммит вызывающего, тронувший `files` в окне, и доля строк патча в нём.

    None, если судить нечем: каталога уже нет (worktree удалён) или это не git.
    Служебные коммиты демона (`agent-dispatch: task …`) не в счёт.
    """
    if not Path(cwd).is_dir():
        return None
    window = [
        f"--since={since.isoformat()}",
        f"--until={until.isoformat()}",
        f"--grep={_DAEMON_COMMIT}",
        "--invert-grep",
    ]
    # Пути патча и changed_files идут от корня репы, а cwd бывает подкаталогом.
    proc = _git(cwd, "log", "--format=%H", *window, "--", *(f":(top){f}" for f in files))
    if proc.returncode != 0:
        return None
    if not proc.stdout.strip():
        return Acceptance(committed=False)
    if not patch:
        return Acceptance(committed=True)
    added = survived = 0
    for path, lines in added_lines(patch).items():
        if not lines:
            continue
        added += len(lines)
        first = _git(cwd, "log", "--reverse", "--format=%H", *window, "--", f":(top){path}")
        commit = (first.stdout.split() or [None])[0]
        if commit is None:
            continue
        shown = _git(cwd, "show", f"{commit}:{path}")
        kept = {line.strip() for line in shown.stdout.splitlines()}
        survived += sum(1 for line in lines if line in kept)
    return Acceptance(committed=True, added=added, survived=survived)


def _patch_text(result: ExecutionResult) -> str | None:
    path = result.meta.get("patch")
    if not path or not Path(path).is_file():
        return None
    return Path(path).read_text(errors="replace")


def acceptance(
    tasks: list[Task],
    check: Callable[
        [str, list[str], str | None, datetime, datetime], Acceptance | None
    ] = git_acceptance,
    *,
    window_hours: float = 48,
) -> dict:
    """Что вызывающий сделал с дифами исполнителей: закоммитил ли и сколько оставил.

    Судятся задачи с изменёнными файлами, которые дошли до рабочей копии
    вызывающего: `integrated: false` (конфликт, удержание, ветка) не в счёт.
    `committed` это верхняя оценка (коммит мог всё переписать), поэтому рядом
    стоит доля строк патча, доживших до коммита.
    """
    verdicts: Counter[str] = Counter()
    by_executor: dict[str, Counter[str]] = {}
    lines: dict[str, list[int]] = {}
    for task in tasks:
        result = task.result
        if result is None or not result.changed_files or task.finished_at is None:
            continue
        if result.meta.get("integrated") is False:
            continue
        found = check(
            task.cwd,
            list(result.changed_files),
            _patch_text(result),
            task.finished_at,
            task.finished_at + timedelta(hours=window_hours),
        )
        verdict = (
            "unknown" if found is None else "committed" if found.committed else "not_committed"
        )
        verdicts[verdict] += 1
        by_executor.setdefault(task.executor, Counter())[verdict] += 1
        if found is not None and found.added:
            share = found.survived / found.added
            verdicts["kept" if share >= 0.8 else "reworked" if share >= 0.2 else "rewritten"] += 1
            counts = lines.setdefault(task.executor, [0, 0])
            counts[0] += found.survived
            counts[1] += found.added
    judged = verdicts["committed"] + verdicts["not_committed"]
    survived = sum(kept for kept, _ in lines.values())
    added = sum(total for _, total in lines.values())
    return {
        "window_hours": window_hours,
        "diffs": verdicts["committed"] + verdicts["not_committed"] + verdicts["unknown"],
        "committed": verdicts["committed"],
        "not_committed": verdicts["not_committed"],
        "unknown": verdicts["unknown"],
        "committed_share": round(verdicts["committed"] / judged, 3) if judged else None,
        "with_patch": verdicts["kept"] + verdicts["reworked"] + verdicts["rewritten"],
        "kept": verdicts["kept"],
        "reworked": verdicts["reworked"],
        "rewritten": verdicts["rewritten"],
        "lines_survived_share": round(survived / added, 3) if added else None,
        "by_executor": {name: dict(counts) for name, counts in sorted(by_executor.items())},
        "lines_survived_by_executor": {
            name: round(kept / total, 3) for name, (kept, total) in sorted(lines.items())
        },
    }


def _completion(statuses: list[str]) -> float | None:
    if not statuses:
        return None
    return round(sum(1 for status in statuses if status == "completed") / len(statuses), 3)


def router_vs_static(
    decisions: list[Decision],
    tasks: list[Task],
    executors: dict[str, ExecutorSettings],
    *,
    baseline_tier: str = "balanced",
    corporate_min_confidence: float = 0.0,
    corporate_perimeter: bool = True,
) -> dict:
    """Совпал бы выбор Jev со статическим правилом на текущем конфиге.

    Обе стороны пересчитываются `capability.select` по одним кандидатам и одному
    ответу о периметре: Jev со своей capability и ответом о суждении, правило
    с `baseline_tier` и без суждения. Решения без capability (старый формат),
    с guard'ом или с кандидатом, которого нет в конфиге, не сравниваются.
    """
    status = {task.task_id: task.status for task in tasks}
    agree: list[str] = []
    disagree: list[str] = []
    capabilities: Counter[str] = Counter()
    pairs: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    drift = 0
    for decision in decisions:
        if decision.router != "jev" or decision.guard_reason:
            continue
        capability = (decision.judgments.get("capability") or {}).get("value")
        if capability is None:
            skipped["no_capability"] += 1
            continue
        if not decision.candidates or any(n not in executors for n in decision.candidates):
            skipped["unknown_candidate"] += 1
            continue
        candidates = {name: executors[name] for name in decision.candidates}
        corporate = decision.judgments.get("corporate_data") or {}
        narrow = (
            corporate_perimeter
            and has_corporate(candidates)
            and bool(corporate.get("value"))
            and float(corporate.get("confidence", 0.0)) >= corporate_min_confidence
        )
        judgment = bool((decision.judgments.get("judgment") or {}).get("value"))
        jev = select(capability, candidates, judgment=judgment, corporate=narrow).executor
        static = select(baseline_tier, candidates, corporate=narrow).executor
        capabilities[capability] += 1
        drift += jev != decision.choice
        outcome = status.get(decision.task_id, "unknown")
        if jev == static:
            agree.append(outcome)
        else:
            disagree.append(outcome)
            pairs[f"{capability}: jev {jev} / static {static}"] += 1
    compared = len(agree) + len(disagree)
    return {
        "baseline_tier": baseline_tier,
        "compared": compared,
        "agree": len(agree),
        "agreement": round(len(agree) / compared, 3) if compared else None,
        "capabilities": dict(capabilities.most_common()),
        "disagreements": dict(pairs.most_common()),
        "completed_when_agree": _completion(agree),
        "completed_when_disagree": _completion(disagree),
        "choice_drift": drift,
        "skipped": dict(skipped),
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
    if "children_refusals" in report:
        info = report["children_refusals"]
        lines.append(
            f"max_children refusals: {info['refused']}, today's rule "
            f"(limit {info['max_children']}) admits {info['admitted_now']}, "
            f"the other {info['retries_avoided']} were retries of a refusal"
        )
        lines += [
            f"  {row['parent']}: {row['children']} assigned child(ren), "
            f"{row['refused']} refused, {row['admitted_now']} admitted now"
            for row in info["parents"]
        ]
    if "delegations" in report:
        info = report["delegations"]
        causes = ", ".join(f"{cause} {n}" for cause, n in info["wasted_by_cause"].items())
        lines.append(
            f"delegations: {info['roots']} finished, {info['completed']} completed, "
            f"{info['work_only']} work only, {info['wasted']} wasted "
            f"({info['wasted_share']:.0%}, {info['wasted_minutes']} min)"
            + (f": {causes}" if causes else "")
        )
    if "acceptance" in report:
        info = report["acceptance"]
        share = "n/a" if info["committed_share"] is None else f"{info['committed_share']:.0%}"
        lines.append(
            f"diffs committed by the caller within {info['window_hours']:g} h: "
            f"{info['committed']} of {info['committed'] + info['not_committed']} ({share}), "
            f"unknown {info['unknown']}"
        )
        if info["with_patch"]:
            lines.append(
                f"  executor lines kept in the caller's commit: "
                f"{info['lines_survived_share']:.0%} over {info['with_patch']} diffs "
                f"(kept {info['kept']}, reworked {info['reworked']}, "
                f"rewritten {info['rewritten']})"
            )
        survival = info["lines_survived_by_executor"]
        lines += [
            f"  {name}: "
            + ", ".join(f"{verdict} {n}" for verdict, n in counts.items())
            + (f", lines kept {survival[name]:.0%}" if name in survival else "")
            for name, counts in info["by_executor"].items()
        ]
    if "router_vs_static" in report:
        info = report["router_vs_static"]
        share = "n/a" if info["agreement"] is None else f"{info['agreement']:.0%}"

        def rate(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.0%}"

        lines.append(
            f"jev vs static tier '{info['baseline_tier']}': {info['agree']} of "
            f"{info['compared']} agree ({share}); completed when agree "
            f"{rate(info['completed_when_agree'])}, when disagree "
            f"{rate(info['completed_when_disagree'])}; logged choice differs from today's "
            f"config in {info['choice_drift']}"
        )
        lines += [f"  {pair}: {n}" for pair, n in info["disagreements"].items()]
    return "\n".join(lines)
