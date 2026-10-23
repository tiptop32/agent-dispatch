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
- очередь корневых задач: сколько каждая группа лимита ждала слоты. Метрика
  берётся только из новых событий `slot`, старые задачи в неё не подмешиваются.

И три вещи о пользе самой утилиты:

- `delegations`: сколько исходных задач (с цепочкой эскалаций) не дали ничего,
  ни `completed`, ни изменённых файлов, сколько минут на них ушло и почему.
  Вызывающий потратил контекст на отправку и ожидание и сделал работу сам;
- `acceptance`: по скольким дифам вызывающий потом закоммитил хотя бы один
  из `changed_files` и какая доля значимых строк патча дожила до этого коммита.
  Сам факт коммита это верхняя оценка: коммит мог переписать работу. Удалённый
  после слияния worktree вызывающего судится по общему git dir (событие `git`
  задачи или поиск по соседям), остальные `unknown` разбиты по причинам;
- `timeouts`: жёсткие таймауты по исполнителям и был ли вывод за последние
  120 с до убийства (`meta.last_output_age_s`): работавшему до конца агенту
  мал таймаут или велика задача, молчавший висел;
- `router_vs_static`: как часто выбор Jev совпал бы со статическим правилом
- `exploration`: исход и acceptance рандомизированных arm `explore` и `jev`
  (один тир для всех, без вопроса о суждении, тот же периметр) на текущем
  конфиге и чем кончились задачи там, где они расходятся.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors import worktree
from agent_dispatch.executors.health import classify_failure, cooldown_seconds, failure_scope
from agent_dispatch.executors.workspace import git_env
from agent_dispatch.models import FINAL_STATUSES, ExecutionResult
from agent_dispatch.routing.capability import narrows_to_perimeter, select
from agent_dispatch.routing.exploration import (
    ARM_JUDGMENT,
    CROSSOVER_JUDGMENT,
    JEV_CHOICE_JUDGMENT,
)

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
    #: События, которые читает replay (`slot`, `git`), в порядке записи.
    events: list[dict] = field(default_factory=list)
    #: Общий git dir cwd из события `git` при `submit`; у задач до него None.
    git_common_dir: str | None = None
    #: `decision.meta.perimeter` задачи (`answered`/`unknown`) или None.
    perimeter: str | None = None
    followup_of: str | None = None


@dataclass
class Decision:
    task_id: str
    router: str
    candidates: list[str]
    choice: str
    guard_reason: str | None
    judgments: dict
    capability: str | None = None


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
            " coalesce(json_extract(request_json, '$.kind'), 'task'), cwd, parent_task_id,"
            " json_extract(decision_json, '$.meta.perimeter'),"
            " json_extract(request_json, '$.followup_of')"
            " from tasks order by created_at"
        ).fetchall()
        # Только нужные события: таблица растёт с каждой задачей.
        event_rows = conn.execute(
            "select task_id, ts, kind, payload_json from events"
            " where kind in ('slot', 'git') order by id"
        ).fetchall()
    finally:
        conn.close()
    events: dict[str, list[dict]] = {}
    git: dict[str, str] = {}
    for task_id, ts, kind, payload in event_rows:
        data = json.loads(payload)
        events.setdefault(task_id, []).append({"ts": ts, "kind": kind, "payload": data})
        if kind == "git":
            git[task_id] = data["common_dir"]
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
            events=events.get(row[0], []),
            git_common_dir=git.get(row[0]),
            perimeter=row[13],
            followup_of=row[14],
        )
        for row in rows
    ]


def perimeter_violations(tasks: list[Task], corporate: Callable[[str], bool]) -> dict:
    """Запуски вне корпоративного периметра, который требовала задача или её предок.

    Требование читается так же, как в `Dispatcher._lineage_perimeter`: своё
    `decision.meta.perimeter` или любого предка по `escalated_from`,
    `followup_of` и `parent_task_id`. Отказы guard'а не запуски. Порог ноль:
    любой такой запуск это утечка корпоративного кода наружу. `corporate`
    судит по текущему конфигу: исполнитель, позже снятый с периметра, даст
    ложное срабатывание, а не пропуск.
    """
    by_id = {task.task_id: task for task in tasks}

    def required(task: Task) -> str | None:
        pending, seen = [task], set()
        while pending:
            current = pending.pop()
            if current.task_id in seen:
                continue
            seen.add(current.task_id)
            if current.perimeter in ("answered", "unknown"):
                return current.perimeter
            for parent_id in (current.escalated_from, current.followup_of, current.parent_task_id):
                if parent_id in by_id:
                    pending.append(by_id[parent_id])
        return None

    rows = [
        {"task_id": task.task_id[:8], "executor": task.executor, "perimeter": status}
        for task in tasks
        if not guard_refusal(task)
        and not corporate(task.executor)
        and (status := required(task)) is not None
    ]
    return {"count": len(rows), "runs": rows}


def load_decisions(db_path: Path) -> list[Decision]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select coalesce(d.task_id, ''), d.router, d.candidates_json, d.choice,"
            " d.guard_reason, d.judgments_json, json_extract(t.decision_json, '$.capability')"
            " from routing_decisions d left join tasks t on t.task_id = d.task_id order by d.id"
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
            capability=row[6],
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


def _queue_wait_by_limit_group(tasks: list[Task]) -> dict[str, dict]:
    """Сводка ожидания по новым slot-событиям; старые задачи исключены."""
    waits: dict[str, list[float]] = {}
    for task in tasks:
        for event in task.events:
            if event["kind"] == "slot":
                payload = event["payload"]
                waits.setdefault(payload["limit_group"], []).append(payload["wait_seconds"])
    return {
        group: {
            "samples": len(values),
            "waited_over_60_seconds": sum(value > 60 for value in values),
            "avg_seconds": round(sum(values) / len(values), 1),
            "max_seconds": round(max(values), 1),
        }
        for group, values in sorted(waits.items())
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
    recovery_tasks = [
        task for task in tasks if task.result and task.result.meta.get("report_recovery")
    ]
    recovery: dict[str, int] = {"attempted": len(recovery_tasks), "recovered": 0, "failed": 0}
    for task in recovery_tasks:
        outcome = str(task.result.meta["report_recovery"]).split(":", 1)[0]
        recovery[outcome if outcome in recovery else "failed"] += 1
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
        "report_recovery": recovery,
        "review_only_runs": len(restricted),
        "review_only_failed": sum(1 for task in restricted if task.status == "failed"),
        "review_only_minutes": round(sum(task.duration_ms for task in restricted) / 60000, 1),
        "queue_wait_by_limit_group": _queue_wait_by_limit_group(tasks),
        "section_windows": {
            "executor_failures": _window(failure_times),
            "avoidable_runs": _window(avoidable_times),
            "integrations_to_hold": _window(held_times),
            "partial_from_report_format": _window([task.created_at for task in format_only_tasks]),
            "report_recovery": _window([task.created_at for task in recovery_tasks]),
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
    `via` говорит, где искали коммит (`GitRepo.via`).
    """

    committed: bool
    added: int = 0
    survived: int = 0
    via: str = "cwd"


@dataclass(frozen=True)
class Unknown:
    """Судить нечем. Причина идёт в разбивку отчёта:

    - `cwd_gone`: каталога нет, общий git dir не записан и не нашёлся;
    - `ambiguous`: каталога нет, а git-checkouts рядом с ним из разных реп;
    - `not_git`: каталог есть, но это не git, и репа не нашлась так же, как
      для удалённого;
    - `git_error`: git отказал (битая репа, нет коммита);
    - `unverified_repo`: догадка по предку или соседям нашла репу, в истории
      которой нет ни одного файла задачи, то есть, скорее всего, чужую.
    """

    reason: str


@dataclass(frozen=True)
class GitRepo:
    """Где искать коммит вызывающего.

    `via: cwd` это рабочая копия задачи (`git -C`, её HEAD). Остальное это общий
    git dir удалённой рабочей копии (`git --git-dir`, все ветки без stash):
    ветка вызывающего могла слиться в основную, а могла остаться отдельной.
    `recorded` взят из события `git` задачи, `ancestor` из ближайшего живого
    предка cwd, `siblings` из соседних checkouts с одним общим git dir.
    """

    path: str
    via: str = "cwd"

    @property
    def args(self) -> list[str]:
        return ["-C", self.path] if self.via == "cwd" else ["--git-dir", self.path]

    @property
    def refs(self) -> list[str]:
        return [] if self.via == "cwd" else ["--exclude=refs/stash", "--all"]


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


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # Без GIT_*: внутри git-хука GIT_DIR и GIT_INDEX_FILE перебили бы `-C cwd`.
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, env=git_env()
    )


def common_dir(path: str | Path) -> str | None:
    """Абсолютный общий git dir каталога (без симлинков) или None."""
    found = worktree.common_dir(path)
    return str(Path(found).resolve()) if found else None


class GitLocator:
    """Репозиторий задачи; кэш на один прогон: у сотен задач общие предки."""

    def __init__(self) -> None:
        self._common: dict[str, str | None] = {}
        # Один и тот же диф судят и `acceptance`, и сравнение arm эксперимента.
        self._verdicts: dict[tuple, Acceptance | Unknown] = {}

    def _common_dir(self, path: Path) -> str | None:
        key = str(path)
        if key not in self._common:
            self._common[key] = common_dir(path)
        return self._common[key]

    def locate(self, task: Task) -> GitRepo | Unknown:
        cwd = Path(task.cwd)
        if cwd.is_dir() and self._common_dir(cwd):
            return GitRepo(task.cwd)
        # Каталог есть, но не git: worktree убран, а неотслеживаемое (`.idea`)
        # осталось. Судится как удалённый, причина без репы своя.
        missing = Unknown("not_git" if cwd.is_dir() else "cwd_gone")
        # Записанный при `submit` git dir сильнее догадки по соседям.
        if task.git_common_dir:
            if Path(task.git_common_dir).is_dir():
                return GitRepo(task.git_common_dir, via="recorded")
            return missing
        ancestor = next((parent for parent in cwd.parents if parent.is_dir()), None)
        if ancestor is None:
            return missing
        # Worktree внутри репы (`.claude/worktrees/…`): предок сам в git.
        own = self._common_dir(ancestor)
        if own:
            return GitRepo(own, via="ancestor")
        # Каталог worktrees одной репы (`wt/qa-tools/<ветка>`): соседи скажут,
        # чья она. Соседи из разных реп не скажут ничего.
        try:
            children = sorted(
                child for child in ancestor.iterdir() if child != cwd and (child / ".git").exists()
            )
        except OSError:
            children = []
        found = {self._common_dir(child) for child in children} - {None}
        if len(found) == 1:
            return GitRepo(found.pop(), via="siblings")
        return Unknown("ambiguous") if found else missing

    def __call__(
        self, task: Task, files: list[str], patch: str | None, since: datetime, until: datetime
    ) -> Acceptance | Unknown:
        key = (task.task_id, tuple(files), patch, since, until)
        if key not in self._verdicts:
            self._verdicts[key] = self._judge(task, files, patch, since, until)
        return self._verdicts[key]

    def _judge(
        self, task: Task, files: list[str], patch: str | None, since: datetime, until: datetime
    ) -> Acceptance | Unknown:
        repo = self.locate(task)
        if isinstance(repo, Unknown):
            return repo
        verdict = git_acceptance(repo, files, patch, since, until)
        # Догадка (предок или соседи) могла найти чужую репу: удалённый
        # checkout репы B под dotfiles-репой A. Коммит в окне уже доказывает,
        # что репа знает файл; без него нужна проверка по всей истории, иначе
        # чужая репа дала бы ложный `not_committed`.
        if repo.via in {"ancestor", "siblings"} and isinstance(verdict, Acceptance):
            if not verdict.committed:
                known = _knows_any(repo, files)
                if known is None:
                    return Unknown("git_error")
                if not known:
                    return Unknown("unverified_repo")
        return verdict


def _knows_any(repo: GitRepo, files: list[str]) -> bool | None:
    """Есть ли в истории репы (все ветки) хотя бы один из путей.

    None, если git отказал: битая репа это `git_error`, а не «чужая репа».
    """
    proc = _git(
        *repo.args,
        "log",
        *repo.refs,
        "-1",
        "--format=%H",
        "--",
        *(f":(top){f}" for f in files),
    )
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def git_acceptance(
    repo: GitRepo, files: list[str], patch: str | None, since: datetime, until: datetime
) -> Acceptance | Unknown:
    """Коммит вызывающего, тронувший `files` в окне, и доля строк патча в нём.

    Служебные коммиты демона (`agent-dispatch: task …`) не в счёт.
    """
    window = [
        *repo.refs,
        f"--since={since.isoformat()}",
        f"--until={until.isoformat()}",
        f"--grep={_DAEMON_COMMIT}",
        "--invert-grep",
    ]
    # Пути патча и changed_files идут от корня репы, а cwd бывает подкаталогом.
    # С `--git-dir` рабочей копии нет, и `:(top)` тоже значит корень.
    proc = _git(*repo.args, "log", "--format=%H", *window, "--", *(f":(top){f}" for f in files))
    if proc.returncode != 0:
        return Unknown("git_error")
    if not proc.stdout.strip():
        return Acceptance(committed=False, via=repo.via)
    if not patch:
        return Acceptance(committed=True, via=repo.via)
    added = survived = 0
    for path, lines in added_lines(patch).items():
        if not lines:
            continue
        added += len(lines)
        first = _git(*repo.args, "log", "--reverse", "--format=%H", *window, "--", f":(top){path}")
        commit = (first.stdout.split() or [None])[0]
        if commit is None:
            continue
        shown = _git(*repo.args, "show", f"{commit}:{path}")
        kept = {line.strip() for line in shown.stdout.splitlines()}
        survived += sum(1 for line in lines if line in kept)
    return Acceptance(committed=True, added=added, survived=survived, via=repo.via)


def _patch_text(result: ExecutionResult) -> str | None:
    path = result.meta.get("patch")
    if not path or not Path(path).is_file():
        return None
    return Path(path).read_text(errors="replace")


def acceptance(
    tasks: list[Task],
    check: Callable[[Task, list[str], str | None, datetime, datetime], Acceptance | Unknown]
    | None = None,
    *,
    window_hours: float = 48,
) -> dict:
    """Что вызывающий сделал с дифами исполнителей: закоммитил ли и сколько оставил.

    Судятся задачи с изменёнными файлами, которые дошли до рабочей копии
    вызывающего: `integrated: false` (конфликт, удержание, ветка) не в счёт.
    `committed` это верхняя оценка (коммит мог всё переписать), поэтому рядом
    стоит доля строк патча, доживших до коммита.
    """
    check = check or GitLocator()
    verdicts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    via: Counter[str] = Counter()
    by_executor: dict[str, Counter[str]] = {}
    lines: dict[str, list[int]] = {}
    for task in tasks:
        result = task.result
        if result is None or not result.changed_files or task.finished_at is None:
            continue
        if result.meta.get("integrated") is False:
            continue
        found = check(
            task,
            list(result.changed_files),
            _patch_text(result),
            task.finished_at,
            task.finished_at + timedelta(hours=window_hours),
        )
        if isinstance(found, Unknown):
            verdict = "unknown"
            reasons[found.reason] += 1
        else:
            verdict = "committed" if found.committed else "not_committed"
            via[found.via] += 1
        verdicts[verdict] += 1
        by_executor.setdefault(task.executor, Counter())[verdict] += 1
        if isinstance(found, Acceptance) and found.added:
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
        "unknown_by_reason": dict(reasons.most_common()),
        "judged_via": dict(via.most_common()),
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


#: Вывод не старше этого к моменту убийства: агент работал до конца.
ACTIVE_BEFORE_KILL_S = 120


#: Поля секции `timeouts`: всего и по вердиктам.
_TIMEOUT_KEYS = ("count", "active", "quiet", "no_stream", "unknown")


def timeouts(tasks: list[Task]) -> dict:
    """Жёсткие таймауты по исполнителям: работал агент до конца или молчал.

    `active`: вывод был за `ACTIVE_BEFORE_KILL_S` до убийства, исполнителю мал
    таймаут (`executors.<имя>.timeout_seconds`) или задачу надо дробить.
    `quiet`: молчал дольше, агент висел или ждал чего-то. `no_stream`: CLI
    печатает всё в конце (claude), по молчанию не судят. `unknown`: задачи до
    `meta.last_output_age_s`.
    """
    by_executor: dict[str, Counter[str]] = {}
    minutes: dict[str, int] = {}
    for task in tasks:
        result = task.result
        if result is None or result.error != "timeout":
            continue
        if "last_output_age_s" not in result.meta:
            verdict = "unknown"
        elif result.meta["last_output_age_s"] is None:
            verdict = "no_stream"
        elif result.meta["last_output_age_s"] <= ACTIVE_BEFORE_KILL_S:
            verdict = "active"
        else:
            verdict = "quiet"
        counts = by_executor.setdefault(task.executor, Counter())
        counts["count"] += 1
        counts[verdict] += 1
        minutes[task.executor] = minutes.get(task.executor, 0) + task.duration_ms
    total: Counter[str] = Counter()
    for counts in by_executor.values():
        total.update(counts)
    return {
        "active_window_s": ACTIVE_BEFORE_KILL_S,
        **{key: total[key] for key in _TIMEOUT_KEYS},
        "by_executor": {
            name: {
                **{key: counts[key] for key in _TIMEOUT_KEYS},
                "avg_minutes": round(minutes[name] / counts["count"] / 60000, 1),
            }
            for name, counts in sorted(
                by_executor.items(), key=lambda item: (-item[1]["count"], item[0])
            )
        },
    }


def _completion(statuses: list[str]) -> float | None:
    if not statuses:
        return None
    return round(sum(1 for status in statuses if status == "completed") / len(statuses), 3)


def exploration_comparison(
    decisions: list[Decision],
    tasks: list[Task],
    check: Callable[[Task, list[str], str | None, datetime, datetime], Acceptance | Unknown]
    | None = None,
    *,
    window_hours: float = 48,
    with_acceptance: bool = True,
) -> dict:
    """Сравнить исходы рандомизированных arm `explore` и `jev`.

    Берутся только новые решения с явной меткой `exploration_arm`: старые
    записи Jev не попадают в контрольную группу задним числом. Acceptance
    считается той же функцией, что и самостоятельная секция replay.
    Незавершённые задачи исхода ещё не имеют и в доли не входят: иначе arm с
    большим числом задач в работе выглядел бы хуже. Crossover (arm `explore`,
    но статический исполнитель был недоступен и запустился выбор Jev) остаётся
    в своём arm по назначению, а не по факту, и считается отдельно.
    """
    arm_by_task: dict[str, str] = {}
    crossover: set[str] = set()
    for decision in decisions:
        marker = decision.judgments.get(ARM_JUDGMENT) or {}
        arm = marker.get("value")
        if arm in {"explore", "jev"} and decision.task_id:
            arm_by_task[decision.task_id] = arm
            if decision.judgments.get(CROSSOVER_JUDGMENT):
                crossover.add(decision.task_id)

    grouped: dict[str, list[Task]] = {"explore": [], "jev": []}
    in_flight: Counter[str] = Counter()
    for task in tasks:
        arm = arm_by_task.get(task.task_id)
        if arm is None:
            continue
        if task.status not in FINAL_STATUSES:
            in_flight[arm] += 1
            continue
        grouped[arm].append(task)

    arms: dict[str, dict] = {}
    for arm in ("explore", "jev"):
        members = grouped[arm]
        accepted = (
            acceptance(members, check, window_hours=window_hours) if with_acceptance else None
        )
        arms[arm] = {
            "n": len(members),
            "in_flight": in_flight[arm],
            "crossover": sum(1 for task in members if task.task_id in crossover),
            "completed_share": _completion([task.status for task in members]),
            "committed_share": accepted["committed_share"] if accepted else None,
            "lines_kept_share": accepted["lines_survived_share"] if accepted else None,
        }

    small = [f"{arm} ({arms[arm]['n']})" for arm in ("explore", "jev") if arms[arm]["n"] < 30]
    warning = f"warning: fewer than 30 tasks in arm(s): {', '.join(small)}" if small else None
    return {"arms": arms, "warning": warning}


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
        if decision.router not in {"jev", "explore"} or decision.guard_reason:
            continue
        capability = decision.capability or (decision.judgments.get("capability") or {}).get(
            "value"
        )
        if capability is None:
            skipped["no_capability"] += 1
            continue
        if not decision.candidates or any(n not in executors for n in decision.candidates):
            skipped["unknown_candidate"] += 1
            continue
        candidates = {name: executors[name] for name in decision.candidates}
        corporate = decision.judgments.get("corporate_data") or {}
        narrow = narrows_to_perimeter(
            candidates,
            answered_yes=bool(corporate.get("value")),
            confidence=float(corporate.get("confidence", 0.0)),
            perimeter=corporate_perimeter,
            min_confidence=corporate_min_confidence,
        )
        judgment = bool((decision.judgments.get("judgment") or {}).get("value"))
        jev = select(capability, candidates, judgment=judgment, corporate=narrow).executor
        static = select(baseline_tier, candidates, corporate=narrow).executor
        capabilities[capability] += 1
        logged_jev = (decision.judgments.get(JEV_CHOICE_JUDGMENT) or {}).get(
            "value", decision.choice
        )
        drift += jev != logged_jev
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


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


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
    recovery = report["report_recovery"]
    lines.append(
        f"missing reports asked again in the same session: {recovery['attempted']}, "
        f"recovered {recovery['recovered']}, failed {recovery['failed']}"
        + window("report_recovery")
    )
    lines.append(
        f"work given to a review-only executor: {report['review_only_runs']} runs, "
        f"{report['review_only_failed']} failed, {report['review_only_minutes']} min"
        + window("review_only_runs")
    )
    waits = report["queue_wait_by_limit_group"]
    # Пустая секция без пояснения читается как «ожиданий не было», а это
    # значит лишь, что демон ещё не писал события `slot`.
    lines.append(
        "queue wait by limit group (slot events only):" + ("" if waits else " no slot events yet")
    )
    lines += [
        f"  {group}: {info['waited_over_60_seconds']} over 60 s of {info['samples']}, "
        f"avg {info['avg_seconds']:.1f} s, max {info['max_seconds']:.1f} s"
        for group, info in waits.items()
    ]
    if "perimeter_violations" in report:
        info = report["perimeter_violations"]
        lines.append(f"runs outside a required corporate perimeter (must be 0): {info['count']}")
        lines += [
            f"  {row['task_id']} {row['executor']} (perimeter {row['perimeter']})"
            for row in info["runs"]
        ]
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
        share = _pct(info["committed_share"])
        reasons = ", ".join(f"{reason} {n}" for reason, n in info["unknown_by_reason"].items())
        lines.append(
            f"diffs committed by the caller within {info['window_hours']:g} h: "
            f"{info['committed']} of {info['committed'] + info['not_committed']} ({share}), "
            f"unknown {info['unknown']}" + (f" ({reasons})" if reasons else "")
        )
        removed = {via: n for via, n in info["judged_via"].items() if via != "cwd"}
        if removed:
            lines.append(
                "  judged by the git dir of a removed working copy: "
                + ", ".join(f"{via} {n}" for via, n in removed.items())
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
    if "timeouts" in report:
        info = report["timeouts"]
        lines.append(
            f"timeouts: {info['count']}, output within {info['active_window_s']} s before "
            f"the kill {info['active']} (raise that executor's timeout or split the task), "
            f"quiet {info['quiet']}, no stream {info['no_stream']}, unknown {info['unknown']}"
        )
        lines += [
            f"  {name}: {row['count']}, active {row['active']}, quiet {row['quiet']}, "
            f"no stream {row['no_stream']}, unknown {row['unknown']}, "
            f"avg {row['avg_minutes']} min"
            for name, row in info["by_executor"].items()
        ]
    if "router_vs_static" in report:
        info = report["router_vs_static"]
        share = _pct(info["agreement"])

        lines.append(
            f"jev vs static tier '{info['baseline_tier']}': {info['agree']} of "
            f"{info['compared']} agree ({share}); completed when agree "
            f"{_pct(info['completed_when_agree'])}, when disagree "
            f"{_pct(info['completed_when_disagree'])}; logged choice differs from today's "
            f"config in {info['choice_drift']}"
        )
        lines += [f"  {pair}: {n}" for pair, n in info["disagreements"].items()]
    if "exploration" in report:
        info = report["exploration"]

        for arm in ("explore", "jev"):
            metrics = info["arms"][arm]
            lines.append(
                f"routing experiment {arm}: n {metrics['n']}, "
                f"completed {_pct(metrics['completed_share'])}, "
                f"committed {_pct(metrics['committed_share'])}, "
                f"lines kept {_pct(metrics['lines_kept_share'])}, "
                f"in flight {metrics['in_flight']}, crossover {metrics['crossover']}"
            )
        if info["warning"]:
            lines.append(info["warning"])
    return "\n".join(lines)
