"""Replay правил диспатчера по телеметрии: синтетическая база с известным ответом."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.models import ExecutionResult
from evals.replay.harness import (
    Acceptance,
    Decision,
    GitLocator,
    GitRepo,
    Task,
    Unknown,
    acceptance,
    added_lines,
    agent_lb_costs,
    children_refusals,
    cost_drift,
    delegations,
    exploration_comparison,
    feedback_summary,
    git_acceptance,
    load,
    load_decisions,
    perimeter_violations,
    post_review,
    recommendations,
    render,
    replay,
    router_vs_static,
    timeouts,
)

SCHEMA = Path(__file__).parent.parent / "agent_dispatch" / "telemetry" / "schema.sql"
# Исполнитель без сети: узнаваемая строка CLI в `error` (события переподключения
# больше не классифицируются — таймаут сам по себе ничего не говорит).
OFFLINE = (
    "couldn't connect to 127.0.0.1:9 (connection refused). "
    "Check that the server is running and reachable."
)


def _result(status, *, error=None, summary="", changed=(), meta=None):
    return {
        "status": status,
        "executor": "x",
        "model": None,
        "summary": summary,
        "changed_files": list(changed),
        "error": error,
        "meta": meta or {},
    }


def _db(tmp_path: Path, rows: list[tuple]) -> Path:
    path = tmp_path / "dispatch.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    for row in rows:
        task_id, executor, created, finished, minutes, parent, result = row[:7]
        source = row[7] if len(row) > 7 else "claude"
        started = row[8] if len(row) > 8 else created
        conn.execute(
            "insert into tasks (task_id, escalated_from, root_agent, source_agent, hop, cwd,"
            " status, executor, request_json, result_json, log_path, created_at, started_at,"
            " finished_at, duration_ms)"
            " values (?, ?, 'claude', ?, 0, '/r', ?, ?, '{}', ?, '', ?, ?, ?, ?)",
            (
                task_id,
                parent,
                source,
                result["status"],
                executor,
                json.dumps(result),
                f"2026-09-23T{created}:00+00:00",
                f"2026-09-23T{started}:00+00:00" if started is not None else None,
                f"2026-09-23T{finished}:00+00:00",
                minutes * 60000,
            ),
        )
    conn.commit()
    conn.close()
    return path


def _slot_event(path: Path, task_id: str, group: str, wait_seconds: float, **policy) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into events (task_id, ts, kind, payload_json) values (?, ?, 'slot', ?)",
        (
            task_id,
            "2026-09-23T12:00:00+00:00",
            json.dumps({"limit_group": group, "wait_seconds": wait_seconds, **policy}),
        ),
    )
    conn.commit()
    conn.close()


def _feedback_event(
    path: Path,
    task_id: str,
    outcome: str,
    ts: str = "2026-09-23T12:01:00+00:00",
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into events (task_id, ts, kind, payload_json) values (?, ?, 'feedback', ?)",
        (task_id, ts, json.dumps({"outcome": outcome, "note": "checked"})),
    )
    conn.commit()
    conn.close()


def test_load_keeps_feedback_and_summary_uses_latest_outcome(tmp_path):
    db = _db(
        tmp_path,
        [
            ("a" * 32, "codex/sol", "10:00", "10:01", 1, None, _result("completed")),
            ("b" * 32, "codex/sol", "10:02", "10:03", 1, None, _result("failed")),
        ],
    )
    _feedback_event(db, "a" * 32, "failure", "2026-09-23T12:01:00+00:00")
    _feedback_event(db, "a" * 32, "user_accepted", "2026-09-23T12:02:00+00:00")
    tasks = load(db)

    assert tasks[0].feedback[-1]["outcome"] == "user_accepted"
    assert feedback_summary(tasks) == {
        "eligible": 2,
        "labeled": 1,
        "unlabeled": 1,
        "coverage": 0.5,
        "outcomes": {"user_accepted": 1},
    }


def test_recommendations_prioritize_observed_waste_and_missing_instrumentation():
    report = {
        "delegations": {"wasted": 2, "roots": 10, "wasted_share": 0.2},
        "review_only_failed": 3,
        "timeouts": {"count": 4, "unknown": 2},
        "feedback": {"eligible": 10, "labeled": 0},
        "exploration": {"arms": {"explore": {"n": 10}, "jev": {"n": 40}}},
    }

    items = recommendations(report)

    assert [item["metric"] for item in items] == [
        "delegations.wasted_share",
        "review_only_failed",
        "timeouts.unknown",
        "feedback.coverage",
        "exploration.sample_size",
    ]
    assert "2/10" in items[0]["evidence"]


def test_replay_counts_runs_sent_to_an_executor_that_was_down(tmp_path):
    db = _db(
        tmp_path,
        [
            # codex без сети, 30 минут до таймаута.
            (
                "a" * 32,
                "codex/sol",
                "10:00",
                "10:30",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
            # Через 5 минут его же снова берут: этот запуск остывание сняло бы.
            (
                "b" * 32,
                "codex/sol",
                "10:35",
                "11:05",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
            # Через час после последнего сбоя остывание уже кончилось.
            ("c" * 32, "codex/sol", "12:10", "12:20", 10, None, _result("completed")),
            # Таймаут работавшего агента исполнителя не выключает.
            (
                "d" * 32,
                "claude/opus",
                "10:00",
                "10:30",
                30,
                None,
                _result("failed", error="timeout", changed=["a.py"]),
            ),
            ("e" * 32, "claude/opus", "10:31", "10:40", 9, None, _result("completed")),
        ],
    )
    report = replay(load(db), cooldown=900)

    assert [row["kind"] for row in report["executor_failures"]] == ["network", "network"]
    assert [row["task_id"] for row in report["avoidable_runs"]] == ["b" * 8]
    assert report["avoidable_minutes"] == 30.0
    assert "runs sent to an executor that was already down: 1, 30.0 min" in render(report)


def test_replay_finds_half_done_work_integrated_before_an_escalation(tmp_path):
    parent = _result("failed", error="timeout", changed=["a.py"], meta={"integrated": True})
    child = _result("completed", changed=["a.py"], meta={"integrated": False})
    clean_parent = _result("failed", error="timeout", meta={"integrated": True})
    db = _db(
        tmp_path,
        [
            ("p" * 32, "codex/sol", "10:00", "10:30", 30, None, parent),
            ("q" * 32, "claude/opus", "10:30", "10:40", 10, "p" * 32, child),
            ("r" * 32, "codex/sol", "11:00", "11:30", 30, None, clean_parent),
            ("s" * 32, "claude/opus", "11:30", "11:40", 10, "r" * 32, _result("completed")),
        ],
    )
    report = replay(load(db))

    assert report["integrations_to_hold"] == [
        {"parent": "p" * 8, "child": "q" * 8, "child_integrated": False}
    ]
    assert report["integration_conflicts_prevented"] == 1


def test_replay_separates_report_format_partials(tmp_path):
    fmt = _result("partial", meta={"parse_error": "'high' is not of type 'number'"})
    real = _result("partial", meta={"parse_error": "no result block"})
    db = _db(
        tmp_path,
        [
            ("f" * 32, "opencode/x5", "10:00", "10:05", 5, None, fmt),
            ("g" * 32, "opencode/x5", "10:10", "10:15", 5, None, real),
        ],
    )
    report = replay(load(db))
    assert report["partial"] == 2 and report["partial_from_report_format"] == ["f" * 8]


def test_replay_counts_report_recoveries(tmp_path):
    db = _db(
        tmp_path,
        [
            (
                "a" * 32,
                "opencode/x5",
                "10:00",
                "10:05",
                5,
                None,
                _result("completed", meta={"report_recovery": "recovered"}),
            ),
            (
                "b" * 32,
                "opencode/x5",
                "10:10",
                "10:15",
                5,
                None,
                _result(
                    "partial",
                    meta={
                        "report_recovery": "failed: no result block",
                        "parse_error": "no result block",
                    },
                ),
            ),
            ("c" * 32, "opencode/x5", "10:20", "10:25", 5, None, _result("completed")),
        ],
    )
    report = replay(load(db))

    assert report["report_recovery"] == {"attempted": 2, "recovered": 1, "failed": 1}
    assert report["section_windows"]["report_recovery"]["first"].startswith("2026-09-23T10:00")
    assert "missing reports asked again in the same session: 2, recovered 1, failed 1" in (
        render(report)
    )


WEEKLY = "You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)"


def test_replay_holds_the_whole_limit_group_until_the_reset(tmp_path):
    # 2026-09-23: claude/opus на недельном лимите в 10:00 UTC, сброс 00:00 UTC.
    db = _db(
        tmp_path,
        [
            ("a" * 32, "claude/opus", "10:00", "10:01", 1, None, _result("failed", error=WEEKLY)),
            # Через 3 часа: старое 15-минутное остывание уже кончилось бы.
            ("b" * 32, "claude/opus", "13:00", "13:01", 1, None, _result("failed", error=WEEKLY)),
            # Соседняя модель того же аккаунта.
            ("c" * 32, "claude/haiku", "14:00", "14:01", 1, None, _result("failed", error=WEEKLY)),
            ("d" * 32, "codex/sol", "14:00", "14:10", 10, None, _result("completed")),
        ],
    )
    report = replay(load(db), cooldown=900)
    assert [row["task_id"] for row in report["avoidable_runs"]] == ["b" * 8, "c" * 8]
    assert report["executor_failures"][0]["hours"] == 14.0


def test_replay_counts_work_given_to_a_review_only_executor(tmp_path):
    db = _db(
        tmp_path,
        [
            ("a" * 32, "claude/opus", "10:00", "10:20", 20, None, _result("completed"), "codex"),
            ("b" * 32, "claude/opus", "11:00", "11:01", 1, None, _result("failed"), "codex"),
            ("c" * 32, "claude/opus", "12:00", "12:05", 5, None, _result("completed"), "claude"),
            ("d" * 32, "codex/sol", "12:00", "12:05", 5, None, _result("completed"), "codex"),
        ],
    )
    report = replay(load(db))
    assert (report["review_only_runs"], report["review_only_failed"]) == (2, 1)
    assert report["review_only_minutes"] == 21.0
    assert "work given to a review-only executor: 2 runs, 1 failed, 21.0 min" in render(report)


def test_replay_avoidable_uses_spawn_time_and_only_finished_cooldowns(tmp_path):
    # Сбой кончился в 10:30, остывание до 10:45. Задача создана до сбоя (старый
    # код по created_at счёл бы её остывание снявшим), но запущена после конца
    # остывания. Вторая запущена, пока сбой ещё шёл: остывание ещё не началось.
    db = _db(
        tmp_path,
        [
            (
                "a" * 32,
                "codex/sol",
                "09:00",
                "10:30",
                90,
                None,
                _result("failed", error=OFFLINE),
            ),
            (
                "b" * 32,
                "codex/sol",
                "10:00",
                "11:05",
                25,
                None,
                _result("completed"),
                "claude",
                "11:00",
            ),
            (
                "c" * 32,
                "codex/sol",
                "10:20",
                "10:40",
                20,
                None,
                _result("completed"),
                "claude",
                "10:25",
            ),
        ],
    )
    report = replay(load(db), cooldown=900)

    assert report["avoidable_runs"] == []
    assert report["avoidable_minutes"] == 0.0


def test_replay_reports_queue_wait_by_limit_group_from_slot_events_only(tmp_path):
    db = _db(
        tmp_path,
        [
            ("a" * 32, "codex/sol", "10:00", "10:01", 1, None, _result("completed")),
            ("b" * 32, "codex/sol", "10:02", "10:03", 1, None, _result("completed")),
            (
                "c" * 32,
                "opencode/x5",
                "10:04",
                "10:05",
                1,
                None,
                _result("completed"),
            ),
            # Старая задача без slot-события в новую метрику не попадает.
            ("d" * 32, "codex/sol", "10:06", "10:07", 1, None, _result("completed")),
        ],
    )
    _slot_event(db, "a" * 32, "codex", 30.0)
    _slot_event(db, "b" * 32, "codex", 90.0)
    _slot_event(db, "c" * 32, "opencode:x5", 120.0)

    report = replay(load(db))

    assert report["queue_wait_by_limit_group"] == {
        "codex": {
            "samples": 2,
            "waited_over_60_seconds": 1,
            "avg_seconds": 60.0,
            "max_seconds": 90.0,
        },
        "opencode:x5": {
            "samples": 1,
            "waited_over_60_seconds": 1,
            "avg_seconds": 120.0,
            "max_seconds": 120.0,
        },
    }
    text = render(report)
    assert "queue wait by limit group (slot events only):" in text
    assert "codex: 1 over 60 s of 2, avg 60.0 s, max 90.0 s" in text
    assert "no slot events yet" not in text


def test_replay_says_when_there_are_no_slot_events(tmp_path):
    db = _db(
        tmp_path,
        [("a" * 32, "codex/sol", "10:00", "10:01", 1, None, _result("completed"))],
    )

    text = render(replay(load(db)))

    assert "queue wait by limit group (slot events only): no slot events yet" in text


def test_replay_never_started_tasks_are_not_avoidable(tmp_path):
    # Задача в очереди: её не отдавали исполнителю, минуты она не тратила.
    db = _db(
        tmp_path,
        [
            (
                "a" * 32,
                "codex/sol",
                "10:00",
                "10:30",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
            (
                "b" * 32,
                "codex/sol",
                "10:35",
                "10:40",
                5,
                None,
                _result("completed"),
                "claude",
                None,
            ),
        ],
    )
    report = replay(load(db), cooldown=900)
    assert report["avoidable_runs"] == []


GUARD_QUOTA = "executor is unavailable: codex/sol after quota: rate limit exceeded, try again later"


def test_replay_skips_guard_refusals(tmp_path):
    # Guard-отказ с пустым исполнителем и ошибкой про лимит старый код считал
    # квотным сбоем с ключом '' и делал остальными guard-отказами «избегаемыми».
    db = _db(
        tmp_path,
        [
            ("a" * 32, "", "10:00", "10:30", 30, None, _result("failed", error=GUARD_QUOTA)),
            (
                "b" * 32,
                "",
                "11:00",
                "11:01",
                1,
                None,
                _result("failed", error="sibling count 2 >= 2"),
            ),
            (
                "c" * 32,
                "codex/sol",
                "11:05",
                "11:06",
                1,
                None,
                _result("failed", meta={"guard": "max_hops"}),
            ),
            (
                "d" * 32,
                "codex/sol",
                "12:00",
                "12:30",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
        ],
    )
    report = replay(load(db), cooldown=900)

    assert report["executor_failures"] == [
        {"task_id": "d" * 8, "executor": "codex/sol", "kind": "network", "hours": 0.25}
    ]
    assert report["avoidable_runs"] == []
    assert report["avoidable_minutes"] == 0.0


def test_replay_reports_first_and_last_case_time_per_section(tmp_path):
    db = _db(
        tmp_path,
        [
            (
                "a" * 32,
                "codex/sol",
                "10:00",
                "10:30",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
            (
                "b" * 32,
                "codex/sol",
                "10:35",
                "11:05",
                30,
                None,
                _result("failed"),
                "claude",
                "10:40",
            ),
            (
                "c" * 32,
                "codex/sol",
                "12:00",
                "12:05",
                5,
                None,
                _result("partial", meta={"parse_error": "'high' is not of type 'number'"}),
            ),
        ],
    )
    report = replay(load(db), cooldown=900)

    assert report["section_windows"]["avoidable_runs"] == {
        "first": "2026-09-23T10:40:00+00:00",
        "last": "2026-09-23T10:40:00+00:00",
    }
    assert report["section_windows"]["executor_failures"] == {
        "first": "2026-09-23T10:30:00+00:00",
        "last": "2026-09-23T10:30:00+00:00",
    }
    assert report["section_windows"]["partial_from_report_format"] == {
        "first": "2026-09-23T12:00:00+00:00",
        "last": "2026-09-23T12:00:00+00:00",
    }
    assert report["section_windows"]["integrations_to_hold"] is None
    text = render(report)
    assert "(cases 2026-09-23T10:40:00+00:00 … 2026-09-23T10:40:00+00:00)" in text
    assert "runs sent to an executor that was already down: 1, 30.0 min" in text


def test_replay_since_keeps_only_later_tasks(tmp_path):
    db = _db(
        tmp_path,
        [
            (
                "a" * 32,
                "codex/sol",
                "10:00",
                "10:30",
                30,
                None,
                _result("failed", error=OFFLINE),
            ),
            ("b" * 32, "codex/sol", "13:00", "13:01", 1, None, _result("completed"), "claude"),
            (
                "c" * 32,
                "codex/sol",
                "13:30",
                "13:31",
                1,
                None,
                _result("failed", error=OFFLINE),
            ),
        ],
    )
    report = replay(load(db), cooldown=900, since=datetime(2026, 9, 23, 12, 0, tzinfo=UTC))

    assert report["tasks"] == 2
    assert [row["task_id"] for row in report["executor_failures"]] == ["c" * 8]
    assert report["avoidable_runs"] == []
    assert report["section_windows"]["executor_failures"] == {
        "first": "2026-09-23T13:31:00+00:00",
        "last": "2026-09-23T13:31:00+00:00",
    }


def _task(
    task_id,
    status,
    *,
    executor="codex/sol",
    parent=None,
    minutes=1,
    result=None,
    cwd="/r",
    parent_task_id=None,
    git_common_dir=None,
):
    created = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    return Task(
        task_id=task_id,
        executor=executor,
        status=status,
        created_at=created,
        started_at=created,
        finished_at=created,
        duration_ms=minutes * 60000,
        escalated_from=parent,
        # У отменённой и ещё идущей задачи результата нет.
        result=None
        if status in ("cancelled", "running")
        else ExecutionResult.model_validate(result or _result(status)),
        cwd=cwd,
        parent_task_id=parent_task_id,
        git_common_dir=git_common_dir,
    )


def test_delegations_follow_the_escalation_chain_to_its_outcome():
    tasks = [
        # Упал, но эскалация довела до completed: делегирование удалось.
        _task("a" * 32, "failed", minutes=10, result=_result("failed", error="timeout")),
        _task("b" * 32, "completed", parent="a" * 32, minutes=5),
        # Таймаут с изменёнными файлами: работа есть, хотя не completed.
        _task("c" * 32, "partial", result=_result("partial", changed=["x.py"])),
        # Квота, затем отказ guard'а следующему звену: ничего, 7 минут впустую.
        _task("d" * 32, "failed", minutes=6, result=_result("failed", error=WEEKLY)),
        _task(
            "e" * 32,
            "failed",
            executor="",
            parent="d" * 32,
            result=_result("failed", error="sibling count 2 >= 2"),
        ),
        _task("f" * 32, "cancelled", minutes=0),
        _task("g" * 32, "running"),
    ]
    report = delegations(tasks)

    assert report == {
        "roots": 4,
        "in_flight": 1,
        "completed": 1,
        "work_only": 1,
        "wasted": 2,
        "wasted_share": 0.5,
        "wasted_minutes": 7.0,
        "wasted_by_cause": {"guard": 1, "cancelled": 1},
    }


def test_delegations_treat_an_escalation_outside_since_as_a_root():
    child = _task("b" * 32, "completed", parent="a" * 32)
    assert delegations([child])["completed"] == 1


def test_added_lines_keep_meaningful_lines_per_file():
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,3 @@\n"
        " x = 1\n+    }\n+value = compute(x)\n"
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n"
        "-gone = True\n"
    )
    assert added_lines(patch) == {"a.py": ["value = compute(x)"]}


def _accepted(verdicts):
    """Поддельная проверка git: вердикт по cwd задачи."""

    def check(task, files, patch, since, until):
        assert until - since == timedelta(hours=48)
        return verdicts[task.cwd]

    return check


def test_acceptance_counts_commits_and_surviving_lines():
    def done(task_id, cwd, **meta):
        return _task(
            task_id, "completed", result=_result("completed", changed=["a.py"], meta=meta), cwd=cwd
        )

    tasks = [
        done("a" * 32, "/kept"),
        done("b" * 32, "/rewritten"),
        done("c" * 32, "/dropped"),
        done("d" * 32, "/gone"),
        done("g" * 32, "/plain"),
        done("h" * 32, "/merged"),
        # Не дошло до рабочей копии: судить вызывающего не за что.
        done("e" * 32, "/held", integrated=False),
        # Ревью без изменений файлов.
        _task("f" * 32, "completed", cwd="/review"),
    ]
    verdicts = {
        "/kept": Acceptance(committed=True, added=10, survived=9),
        "/rewritten": Acceptance(committed=True, added=10, survived=1),
        "/dropped": Acceptance(committed=False),
        "/gone": Unknown("cwd_gone"),
        "/plain": Unknown("not_git"),
        "/merged": Acceptance(committed=True, via="siblings"),
    }
    report = acceptance(tasks, _accepted(verdicts))

    assert (report["diffs"], report["committed"], report["not_committed"], report["unknown"]) == (
        6,
        3,
        1,
        2,
    )
    assert report["unknown_by_reason"] == {"cwd_gone": 1, "not_git": 1}
    assert report["judged_via"] == {"cwd": 3, "siblings": 1}
    assert report["committed_share"] == 0.75
    assert (report["kept"], report["reworked"], report["rewritten"]) == (1, 0, 1)
    assert report["lines_survived_share"] == 0.5
    text = render({**replay([]), "acceptance": report})
    assert (
        "diffs committed by the caller within 48 h: 3 of 4 (75%), unknown 2 (cwd_gone 1, not_git 1)"
        in text
    )
    assert "judged by the git dir of a removed working copy: siblings 1" in text
    assert "executor lines kept in the caller's commit: 50% over 2 diffs" in text


def _git_env(home: Path) -> dict:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "HOME": str(home),
    }


def test_git_acceptance_reads_the_callers_commit_and_skips_daemon_commits(git_repo, tmp_path):
    # Коммиты на час вперёд: init-коммит фикстуры не должен попасть в окно.
    later = datetime.now(UTC) + timedelta(hours=1)
    env = {
        **_git_env(tmp_path),
        "GIT_AUTHOR_DATE": later.isoformat(),
        "GIT_COMMITTER_DATE": later.isoformat(),
    }

    def commit(message, text):
        (git_repo / "a.py").write_text(text)
        subprocess.run(["git", "add", "a.py"], cwd=git_repo, env=env, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=git_repo, env=env, check=True)

    since = later - timedelta(minutes=30)
    until = later + timedelta(minutes=30)
    patch = "+++ b/a.py\n+value = compute(x)\n+other = compute(y)\n"
    # Коммит демона на ветке задачи не решение вызывающего.
    commit("agent-dispatch: task deadbeef", "x = 1\nvalue = compute(x)\nother = compute(y)\n")
    assert git_acceptance(GitRepo(str(git_repo)), ["a.py"], patch, since, until) == Acceptance(
        committed=False
    )

    commit("caller keeps half", "x = 1\nvalue = compute(x)\n")
    assert git_acceptance(GitRepo(str(git_repo)), ["a.py"], patch, since, until) == Acceptance(
        committed=True, added=2, survived=1
    )
    sub = git_repo / "sub"
    sub.mkdir()
    assert git_acceptance(GitRepo(str(sub)), ["a.py"], patch, since, until) == Acceptance(
        committed=True, added=2, survived=1
    )
    # Вдали от репы фикстуры: соседей-checkouts у каталога нет.
    lonely = tmp_path / "far" / "lonely"
    lonely.mkdir(parents=True)
    locator = GitLocator()
    assert locator.locate(_task("a" * 32, "completed", cwd=str(lonely / "gone"))) == Unknown(
        "cwd_gone"
    )
    assert locator.locate(_task("a" * 32, "completed", cwd=str(lonely))) == Unknown("not_git")
    broken = tmp_path / "broken.git"
    broken.mkdir()
    assert git_acceptance(GitRepo(str(broken), via="recorded"), ["a.py"], patch, since, until) == (
        Unknown("git_error")
    )


def _removed_worktree(git_repo: Path, tmp_path: Path) -> tuple[Path, datetime, datetime]:
    """Worktree вызывающего в `wt/`, его ветка слита в main, а сам он удалён.

    Рядом остаётся живой worktree той же репы. Ещё есть ветка демона с его
    служебным коммитом в `b.py` и stash с правкой `c.py`: ни то, ни другое не
    решение вызывающего.
    """
    later = datetime.now(UTC) + timedelta(hours=1)
    env = {
        **_git_env(tmp_path),
        "GIT_AUTHOR_DATE": later.isoformat(),
        "GIT_COMMITTER_DATE": later.isoformat(),
    }

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    wt = tmp_path / "wt"
    feature = wt / "feature"
    git(git_repo, "worktree", "add", "-q", "-b", "feature", str(feature))
    git(git_repo, "worktree", "add", "-q", "-b", "other", str(wt / "other"))
    (feature / "a.py").write_text("x = 1\nvalue = compute(x)\n")
    git(feature, "commit", "-qam", "caller keeps half")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    git(git_repo, "worktree", "remove", str(feature))
    git(git_repo, "branch", "-q", "-d", "feature")

    git(git_repo, "checkout", "-q", "-b", "agent-dispatch/t")
    (git_repo / "b.py").write_text("daemon = True\n")
    git(git_repo, "add", "b.py")
    git(git_repo, "commit", "-qm", "agent-dispatch: task deadbeef")
    git(git_repo, "checkout", "-q", "main")
    (git_repo / "c.py").write_text("stashed = True\n")
    git(git_repo, "add", "c.py")
    git(git_repo, "stash", "-q")
    return feature, later - timedelta(minutes=30), later + timedelta(minutes=30)


def test_removed_worktree_whose_branch_was_merged_counts_as_committed(git_repo, tmp_path):
    feature, since, until = _removed_worktree(git_repo, tmp_path)
    patch = "+++ b/a.py\n+value = compute(x)\n+other = compute(y)\n"
    locator = GitLocator()
    common = str((git_repo / ".git").resolve())

    # Без записанного git dir: живой сосед в `wt/` говорит, чья это репа.
    task = _task("a" * 32, "completed", cwd=str(feature / "src"))
    assert locator.locate(task) == GitRepo(common, via="siblings")
    assert locator(task, ["a.py"], patch, since, until) == Acceptance(
        committed=True, added=2, survived=1, via="siblings"
    )
    # Служебный коммит демона и stash в окне есть, но решением вызывающего не считаются.
    assert locator(task, ["b.py"], None, since, until) == Acceptance(
        committed=False, via="siblings"
    )
    # `c.py` есть только в stash: история репы его не знает, и догадка по
    # соседям не подтверждена этим файлом.
    assert locator(task, ["c.py"], None, since, until) == Unknown("unverified_repo")
    # Каталог worktree остался с неотслеживаемым `.idea`: судится как удалённый.
    (feature / ".idea").mkdir(parents=True)
    leftover = _task("a" * 32, "completed", cwd=str(feature))
    assert locator.locate(leftover) == GitRepo(common, via="siblings")
    # Worktree внутри самой репы: предок cwd сам в git.
    inside = _task("a" * 32, "completed", cwd=str(git_repo / ".claude" / "worktrees" / "gone"))
    assert locator.locate(inside) == GitRepo(common, via="ancestor")


def test_parent_with_checkouts_of_different_repos_stays_unknown(git_repo, tmp_path):
    feature, since, until = _removed_worktree(git_repo, tmp_path)
    stranger = feature.parent / "stranger"
    stranger.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=stranger, env=_git_env(tmp_path), check=True, capture_output=True
    )
    task = _task("a" * 32, "completed", cwd=str(feature))

    assert GitLocator().locate(task) == Unknown("ambiguous")
    report = acceptance(
        [
            _task(
                "a" * 32,
                "completed",
                cwd=str(feature),
                result=_result("completed", changed=["a.py"]),
            )
        ]
    )
    assert (report["unknown"], report["unknown_by_reason"]) == (1, {"ambiguous": 1})


def test_ancestor_repo_that_never_had_the_files_is_not_the_task_repo(tmp_path):
    # Удалённый checkout репы B лежал под dotfiles-репой A: ближайший живой
    # предок в git, но это не та репа, и вердикт по ней был бы ложным.
    dotfiles = tmp_path / "home"
    dotfiles.mkdir()
    env = _git_env(tmp_path)
    for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "dotfiles")):
        subprocess.run(["git", *args], cwd=dotfiles, env=env, check=True, capture_output=True)
    task = _task("a" * 32, "completed", cwd=str(dotfiles / "wt" / "repo-b-feature"))
    now = datetime.now(UTC)

    assert GitLocator().locate(task).via == "ancestor"
    assert GitLocator()(task, ["src/app.py"], None, now, now) == Unknown("unverified_repo")


def test_broken_inferred_repo_is_a_git_error_not_a_stranger(tmp_path):
    # Ссылка на несуществующий объект: `git log --all` падает. Это поломка
    # репы (`git_error`), а не вывод «репа чужая».
    home = tmp_path / "home"
    home.mkdir()
    env = _git_env(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=home, env=env, check=True, capture_output=True)
    (home / ".git" / "refs" / "heads" / "broken").write_text("1" * 40 + "\n")
    task = _task("a" * 32, "completed", cwd=str(home / "wt" / "gone"))
    now = datetime.now(UTC)

    assert GitLocator()(task, ["src/app.py"], None, now, now) == Unknown("git_error")


def test_recorded_common_dir_wins_over_the_heuristic(git_repo, tmp_path):
    feature, since, until = _removed_worktree(git_repo, tmp_path)
    stranger = feature.parent / "stranger"
    stranger.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=stranger, env=_git_env(tmp_path), check=True, capture_output=True
    )
    common = str((git_repo / ".git").resolve())
    task = _task("a" * 32, "completed", cwd=str(feature), git_common_dir=common)

    assert GitLocator().locate(task) == GitRepo(common, via="recorded")
    assert GitLocator()(task, ["a.py"], None, since, until) == Acceptance(
        committed=True, via="recorded"
    )
    # Записанная репа удалена: догадка по соседям не подменяет запись.
    gone = _task("a" * 32, "completed", cwd=str(feature), git_common_dir=str(tmp_path / "x.git"))
    assert GitLocator().locate(gone) == Unknown("cwd_gone")


def test_timeouts_split_agents_that_worked_to_the_end_from_quiet_ones():
    def timeout(task_id, executor, age=..., minutes=28, status="failed"):
        meta = {} if age is ... else {"last_output_age_s": age}
        result = _result(status, error="timeout", meta=meta)
        return _task(task_id, status, executor=executor, minutes=minutes, result=result)

    tasks = [
        timeout("a" * 32, "codex/sol", 5.0, minutes=30),
        timeout("b" * 32, "codex/sol", 120.0, minutes=30, status="partial"),
        timeout("c" * 32, "codex/sol", 600.0, minutes=30),
        timeout("d" * 32, "codex/sol", minutes=24),
        timeout("e" * 32, "claude", None),
        timeout("f" * 32, "opencode/x5", 900.0),
        # Не таймаут: сторож молчания и обычный сбой.
        _task("g" * 32, "failed", result=_result("failed", error="stalled: no output for 900s")),
        _task("h" * 32, "failed", result=_result("failed", error="exit 1")),
    ]
    report = timeouts(tasks)

    assert {key: report[key] for key in ("count", "active", "quiet", "no_stream", "unknown")} == {
        "count": 6,
        "active": 2,
        "quiet": 2,
        "no_stream": 1,
        "unknown": 1,
    }
    assert list(report["by_executor"]) == ["codex/sol", "claude", "opencode/x5"]
    assert report["by_executor"]["codex/sol"] == {
        "count": 4,
        "active": 2,
        "quiet": 1,
        "no_stream": 0,
        "unknown": 1,
        "avg_minutes": 28.5,
    }
    text = render({**replay([]), "timeouts": report})
    assert "timeouts: 6, output within 120 s before the kill 2" in text
    assert "  codex/sol: 4, active 2, quiet 1, no stream 0, unknown 1, avg 28.5 min" in text


def _settings(adapter, tier, *, corporate=False):
    return ExecutorSettings(
        adapter=adapter,
        tier=tier,
        corporate=corporate,
        model="m" if adapter == "omp" else None,
    )


EXECUTORS = {
    # Исторические имена исполнителей из replay-телеметрии (codex/*, opencode/*)
    # остаются валидными ключами: адаптеры под ними теперь omp.
    "opencode/x5": _settings("omp", "balanced", corporate=True),
    "codex/luna": _settings("omp", "fast"),
    "codex/sol": _settings("omp", "balanced"),
    "claude/opus": _settings("claude", "strong"),
}


def _decision(task_id, capability, *, judgment=False, corporate=None, guard=None, candidates=None):
    judgments = {} if capability is None else {"capability": {"value": capability}}
    judgments["judgment"] = {"value": judgment}
    if corporate is not None:
        judgments["corporate_data"] = {"value": corporate[0], "confidence": corporate[1]}
    return Decision(
        task_id=task_id,
        router="jev",
        candidates=candidates or ["codex/luna", "codex/sol", "claude/opus"],
        choice="",
        guard_reason=guard,
        judgments=judgments,
    )


def test_router_vs_static_compares_jev_with_one_tier_for_everything():
    decisions = [
        _decision("a" * 32, "balanced"),
        _decision("b" * 32, "strong"),
        _decision("c" * 32, "fast"),
        # Суждение уводит к claude даже на balanced.
        _decision("d" * 32, "balanced", judgment=True),
        # Корпоративные данные: обе стороны внутри периметра, тир не важен.
        _decision("e" * 32, "strong", corporate=(True, 0.9), candidates=list(EXECUTORS)),
        # Ниже порога периметра: сужения нет, strong уходит к claude.
        _decision("f" * 32, "strong", corporate=(True, 0.1), candidates=list(EXECUTORS)),
        _decision("g" * 32, "strong", guard="low_confidence"),
        _decision("h" * 32, None),
        _decision("i" * 32, "fast", candidates=["codex/retired"]),
    ]
    statuses = {"a": "completed", "b": "completed", "c": "failed", "d": "failed"}
    statuses |= {"e": "completed", "f": "completed"}
    tasks = [_task(key * 32, status) for key, status in statuses.items()]
    report = router_vs_static(decisions, tasks, EXECUTORS, corporate_min_confidence=0.5)

    assert (report["compared"], report["agree"], report["agreement"]) == (6, 2, 0.333)
    assert report["capabilities"] == {"strong": 3, "balanced": 2, "fast": 1}
    assert report["disagreements"] == {
        "strong: jev claude/opus / static codex/sol": 1,
        "fast: jev codex/luna / static codex/sol": 1,
        "balanced: jev claude/opus / static codex/sol": 1,
        "strong: jev claude/opus / static opencode/x5": 1,
    }
    assert report["completed_when_agree"] == 1.0
    assert report["completed_when_disagree"] == 0.5
    assert report["skipped"] == {"no_capability": 1, "unknown_candidate": 1}
    assert "jev vs static tier 'balanced': 2 of 6 agree (33%)" in render(
        {**replay([]), "router_vs_static": report}
    )


def test_exploration_comparison_reports_outcome_and_acceptance_per_arm():
    def done(task_id, status, cwd):
        return _task(
            task_id,
            status,
            cwd=cwd,
            result=_result(status, changed=["a.py"]),
        )

    tasks = [
        done("a" * 32, "completed", "/explore-kept"),
        done("b" * 32, "failed", "/explore-dropped"),
        done("c" * 32, "completed", "/jev-reworked"),
        _task("d" * 32, "completed", cwd="/jev-no-diff"),
        # Старая запись Jev без метки эксперимента не смешивается с новым arm.
        _task("e" * 32, "failed"),
        # Задача в работе исхода не имеет: в доли не входит, считается отдельно.
        _task("f" * 32, "running", cwd="/explore-running"),
    ]
    decisions = [
        Decision(
            task_id=key * 32,
            router=arm,
            candidates=["codex/sol", "claude/opus"],
            choice="codex/sol" if arm == "explore" else "claude/opus",
            guard_reason=None,
            judgments={"exploration_arm": {"value": arm}},
        )
        for key, arm in (
            ("a", "explore"),
            ("b", "explore"),
            ("c", "jev"),
            ("d", "jev"),
            ("f", "explore"),
        )
    ]
    # Crossover: статический исполнитель был недоступен, arm остаётся explore.
    decisions[1].judgments["exploration_crossover"] = {"value": "codex/sol"}
    decisions.append(_decision("e" * 32, "strong"))
    verdicts = {
        "/explore-kept": Acceptance(committed=True, added=10, survived=9),
        "/explore-dropped": Acceptance(committed=False),
        "/jev-reworked": Acceptance(committed=True, added=10, survived=4),
    }

    report = exploration_comparison(decisions, tasks, check=_accepted(verdicts))

    assert report["arms"] == {
        "explore": {
            "n": 2,
            "in_flight": 1,
            "crossover": 1,
            "completed_share": 0.5,
            "committed_share": 0.5,
            "lines_kept_share": 0.9,
        },
        "jev": {
            "n": 2,
            "in_flight": 0,
            "crossover": 0,
            "completed_share": 1.0,
            "committed_share": 1.0,
            "lines_kept_share": 0.4,
        },
    }
    assert report["warning"] == "warning: fewer than 30 tasks in arm(s): explore (2), jev (2)"
    text = render({**replay([]), "exploration": report})
    assert (
        "routing experiment explore: n 2, completed 50%, committed 50%, lines kept 90%, "
        "in flight 1, crossover 1"
    ) in text
    assert report["warning"] in text


def test_exploration_comparison_default_check_judges_real_tasks():
    # Регрессия: дефолтная проверка получала Task вместо GitRepo и падала
    # AttributeError на первой задаче эксперимента с изменёнными файлами.
    task = _task(
        "a" * 32, "completed", cwd="/no/such/dir", result=_result("completed", changed=["a.py"])
    )
    decision = Decision(
        task_id="a" * 32,
        router="explore",
        candidates=["codex/sol"],
        choice="codex/sol",
        guard_reason=None,
        judgments={"exploration_arm": {"value": "explore"}},
    )

    report = exploration_comparison([decision], [task])

    assert report["arms"]["explore"]["n"] == 1
    assert report["arms"]["explore"]["committed_share"] is None


def test_router_vs_static_uses_logged_jev_choice_in_explore_arm():
    decision = _decision("a" * 32, "strong")
    decision.router = "explore"
    decision.choice = "codex/sol"
    decision.judgments["exploration_jev_choice"] = {"value": "claude/opus"}

    report = router_vs_static([decision], [_task("a" * 32, "completed")], EXECUTORS)

    assert report["compared"] == 1
    assert report["completed_when_disagree"] == 1.0
    assert report["choice_drift"] == 0


def test_router_vs_static_uses_capped_capability():
    decision = _decision("a" * 32, "strong")
    decision.capability = "balanced"
    decision.choice = "codex/sol"

    report = router_vs_static([decision], [_task("a" * 32, "completed")], EXECUTORS)

    assert report["capabilities"] == {"balanced": 1}
    assert report["agree"] == 1
    assert report["choice_drift"] == 0


def test_load_reads_cwd_and_routing_decisions(tmp_path):
    db = _db(tmp_path, [("a" * 32, "codex/sol", "10:00", "10:05", 5, None, _result("completed"))])
    conn = sqlite3.connect(db)
    conn.execute(
        "insert into routing_decisions (task_id, router, candidates_json, choice, confidence,"
        " scores_json, judgments_json, guard_reason, latency_ms, created_at)"
        " values (?, 'jev', '[\"codex/sol\"]', 'codex/sol', 0.9, '{}',"
        ' \'{"capability": {"value": "fast"}}\', null, 10, \'2026-09-23T10:00:00+00:00\')',
        ("a" * 32,),
    )
    conn.execute(
        "update tasks set decision_json=? where task_id=?",
        (json.dumps({"capability": "balanced"}), "a" * 32),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db)
    conn.execute(
        "insert into events (task_id, ts, kind, payload_json) values (?, '', 'git', ?)",
        ("a" * 32, json.dumps({"common_dir": "/repo/.git", "branch": "feature"})),
    )
    conn.commit()
    conn.close()

    (task,) = load(db)
    assert (task.cwd, task.git_common_dir) == ("/r", "/repo/.git")
    assert load_decisions(db) == [
        Decision(
            task_id="a" * 32,
            router="jev",
            candidates=["codex/sol"],
            choice="codex/sol",
            guard_reason=None,
            judgments={"capability": {"value": "fast"}},
            capability="balanced",
        )
    ]


def test_children_refusals_recount_the_quota_by_todays_rule():
    def child(task_id, of, status="completed", **kwargs):
        return _task(task_id, status, parent_task_id=of, **kwargs)

    def refused(task_id, of, count):
        error = f"sibling count {count} >= 2"
        return child(task_id, of, "failed", executor="", result=_result("failed", error=error))

    tasks = [
        # Старый счётчик видел родителя и ребёнка: 2 >= 2 при одном ребёнке.
        # Отменённый ребёнок и ретрай эскалации квоту не занимают.
        child("a" * 32, "p1"),
        child("b" * 32, "p1", "cancelled"),
        child("c" * 32, "p1", parent="a" * 32),
        refused("d" * 32, "p1", 2),
        refused("e" * 32, "p1", 3),
        refused("f" * 32, "p1", 4),
        # Два настоящих ребёнка: отказ верный и сегодня.
        child("g" * 32, "p2"),
        child("h" * 32, "p2"),
        refused("i" * 32, "p2", 2),
        # Дети без отказов в отчёт не попадают.
        child("j" * 32, "p3"),
    ]
    report = children_refusals(tasks, max_children=2)

    assert report == {
        "max_children": 2,
        "refused": 4,
        "admitted_now": 1,
        "retries_avoided": 3,
        "parents": [
            {"parent": "p1", "children": 1, "refused": 3, "admitted_now": 1},
            {"parent": "p2", "children": 2, "refused": 1, "admitted_now": 0},
        ],
    }
    assert "max_children refusals: 4, today's rule (limit 2) admits 1" in render(
        {**replay([]), "children_refusals": report}
    )


def test_perimeter_violations_follow_the_whole_lineage():
    """Запуск на внешнем исполнителе под периметром предка это нарушение, отказ нет."""
    root = _task("root0000", "failed", executor="internal")
    root.perimeter = "answered"
    leaked = _task("leak0000", "completed", executor="codex/sol", parent="root0000")
    nested = _task("nest0000", "completed", executor="codex/sol", parent_task_id="root0000")
    followup = _task("foll0000", "completed", executor="codex/sol")
    followup.followup_of = "root0000"
    refused = _task(
        "refu0000",
        "failed",
        executor="codex/sol",
        parent_task_id="root0000",
        result=_result("failed", meta={"guard": "perimeter"}),
    )
    free = _task("free0000", "completed", executor="codex/sol")
    inside = _task("insi0000", "completed", executor="internal", parent="root0000")

    report = perimeter_violations(
        [root, leaked, nested, followup, refused, free, inside], lambda name: name == "internal"
    )

    assert report["count"] == 3
    assert {row["task_id"] for row in report["runs"]} == {"leak0000", "nest0000", "foll0000"}


@pytest.mark.parametrize("link", ["escalated_from", "followup_of", "parent_task_id"])
def test_prod_replay_includes_refused_requests_and_unmarked_descendants(tmp_path, link):
    """Отказ до decision не скрывает prod и требование у последующих звеньев."""
    db = _db(
        tmp_path,
        [
            (
                "root0000",
                "",
                "10:00",
                "10:01",
                0,
                None,
                _result("failed", meta={"guard": "perimeter"}),
            ),
            (
                "child000",
                "external",
                "10:02",
                "10:03",
                1,
                None,
                _result(
                    "completed",
                    meta={
                        "review": {
                            "verdict": "skipped",
                            "reason": "perimeter: no corporate reviewer",
                        },
                        "redacted": {"replacements": 3, "fields": ["summary", "error"]},
                    },
                ),
            ),
            ("free0000", "external", "10:04", "10:05", 1, None, _result("completed")),
        ],
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "update tasks set request_json = ? where task_id = 'root0000'",
            (json.dumps({"prod": True}),),
        )
        if link == "followup_of":
            conn.execute(
                "update tasks set request_json = ? where task_id = 'child000'",
                (json.dumps({link: "root0000"}),),
            )
        else:
            conn.execute(f"update tasks set {link} = 'root0000' where task_id = 'child000'")
    tasks = load(db)
    assert perimeter_violations(tasks, lambda _: False) == {
        "count": 1,
        "runs": [{"task_id": "child000", "executor": "external", "perimeter": "prod"}],
    }
    report = replay(tasks)
    assert report["prod"] == {
        "tasks": 2,
        "executors": {"external": 1},
        "review_verdicts": {"pass": 0, "fail": 0, "unknown": 0, "skipped": 1},
        "redacted": {"tasks": 1, "replacements": 3, "fields": {"summary": 1, "error": 1}},
    }
    assert "prod tasks: 2" in render(report)
    # CLI отбирает окно после load: prod-предок уже не виден, но правило осталось.
    window = [task for task in tasks if task.task_id != "root0000"]
    assert replay(window)["prod"]["tasks"] == 1
    assert perimeter_violations(window, lambda _: False)["runs"] == [
        {"task_id": "child000", "executor": "external", "perimeter": "prod"}
    ]


def test_prod_lineage_overrides_other_perimeter_marks_and_survives_cycles():
    root = replace(_task("root0000", "completed"), perimeter="prod")
    child = replace(_task("child000", "completed", parent="root0000"), perimeter="answered")
    root.parent_task_id = child.task_id
    orphan = replace(_task("orphan00", "completed", parent="missing"), perimeter="unknown")
    report = perimeter_violations([child, orphan, root], lambda _: False)
    assert [row["perimeter"] for row in report["runs"]] == ["prod", "unknown", "prod"]
    assert replay([child, orphan, root])["prod"]["tasks"] == 2


def test_prod_replay_empty_section():
    assert replay([])["prod"] == {
        "tasks": 0,
        "executors": {},
        "review_verdicts": {"pass": 0, "fail": 0, "unknown": 0, "skipped": 0},
        "redacted": {"tasks": 0, "replacements": 0, "fields": {}},
    }


def test_prod_replay_aggregates_reviews_and_redactions_without_raw_data():
    tasks = []
    for index, verdict in enumerate(("pass", "fail", "unknown", "skipped")):
        tasks.append(
            replace(
                _task(
                    f"prod000{index}",
                    "completed",
                    executor="internal",
                    result=_result(
                        "completed",
                        meta={
                            "review": {"verdict": verdict},
                            "redacted": {"replacements": index + 1, "fields": ["summary"]},
                        },
                    ),
                ),
                prod=True,
            )
        )
    tasks.append(
        replace(
            _task("review00", "completed", executor="reviewer", parent_task_id="prod0000"),
            kind="review",
        )
    )
    # Исполнитель назначен, но запуска не было: задача ждёт слот или отменена
    # до старта. В «runs only» такие не входят, в число продовых задач входят.
    for task_id, status in (("waiting0", "running"), ("cancel00", "cancelled")):
        tasks.append(
            replace(_task(task_id, status, executor="internal"), prod=True, started_at=None)
        )
    report = replay(tasks)["prod"]
    assert report == {
        "tasks": 7,
        "executors": {"internal": 4, "reviewer": 1},
        "review_verdicts": {"pass": 1, "fail": 1, "unknown": 1, "skipped": 1},
        "redacted": {"tasks": 4, "replacements": 10, "fields": {"summary": 4}},
    }


@pytest.mark.parametrize("count", [0, 1, 2])
def test_queue_policy_metrics_exclude_legacy_and_handle_small_samples(tmp_path, count):
    db = _db(tmp_path, [("a" * 32, "codex/sol", "10:00", "10:01", 1, None, _result("completed"))])
    _slot_event(db, "a" * 32, "codex", 999)
    for index in range(count):
        _slot_event(db, "a" * 32, "codex", 10 + index * 20, queue_fallback=index == 1)
    report = replay(load(db))
    policy = report["queue_policy"]
    assert policy["samples"] == count
    assert policy["switched"] == (1 if count == 2 else 0)
    assert policy["switch_share"] == (None if count == 0 else 0 if count == 1 else 0.5)
    assert policy["avg_seconds"] == (None if count == 0 else 10 if count == 1 else 20)
    assert policy["median_seconds"] == policy["avg_seconds"]
    assert policy["p95_seconds"] == (29 if count == 2 else None)
    text = render(report)
    assert ("no policy slot events yet" in text) == (count == 0)
    assert ("unmeasured (requires 2 samples)" in text) == (count == 1)


def test_replay_parses_historical_codex_and_opencode_tasks(tmp_path):
    # Исполнители codex/* и opencode/* ушли из дефолтного конфига, но телеметрия
    # с ними разбирается как раньше: группы лимита и review_only считаются по
    # адаптеру без конфига.
    db = _db(
        tmp_path,
        [
            ("a" * 32, "codex/sol", "10:00", "10:01", 1, None, _result("failed", error=WEEKLY)),
            ("b" * 32, "opencode/x5-code-large", "11:00", "11:02", 2, None, _result("completed")),
        ],
    )
    report = replay(load(db))
    assert report["tasks"] == 2
    assert report["executor_failures"][0]["executor"] == "codex/sol"
    assert report["executor_failures"][0]["hours"] == 14.0
    assert report["avoidable_runs"] == []


def test_replay_counts_review_only_work_for_the_omp_source(tmp_path):
    db = _db(
        tmp_path,
        [
            ("a" * 32, "claude/opus", "10:00", "10:10", 10, None, _result("completed"), "omp"),
            ("b" * 32, "claude/opus", "11:00", "11:05", 5, None, _result("failed"), "omp"),
        ],
    )
    report = replay(load(db))
    assert (report["review_only_runs"], report["review_only_failed"]) == (2, 1)
    assert report["review_only_minutes"] == 15.0


def test_default_limit_group_splits_omp_by_provider():
    from evals.replay.harness import default_limit_group

    assert default_limit_group("omp/sol") == default_limit_group("omp/luna") == "omp:agent-lb"
    assert default_limit_group("omp/x5-code-large") == "omp:copilot-x5"
    assert default_limit_group("omp/copilot-code-flash") == "omp:copilot-x5"
    assert default_limit_group("codex/sol") == "codex"
    assert default_limit_group("opencode/x5-code-large") == "opencode/x5-code-large"


def test_successor_maps_history_to_omp_executors():
    from evals.replay.harness import successor

    assert successor("codex/sol") == "omp/sol"
    assert successor("codex") == "omp/sol"
    assert successor("opencode/x5-code-large") == "omp/x5-code-large"
    assert successor("claude/opus") == "claude/opus"
    assert successor("omp/luna") == "omp/luna"


def _priced(task_id, model, cost, *, session=..., status="completed"):
    meta = {} if session is None else {"session_id": task_id if session is ... else session}
    result = {**_result(status, meta=meta), "model": model, "usage": {"cost_usd": cost}}
    return _task(task_id, status, executor="omp/sol", result=result)


def test_cost_drift_compares_reported_cost_with_agent_lb_by_session():
    # Живой случай 2026-11-02: omp считал gpt-5.6-sol по своему каталогу
    # ($4/$0.4/$20), балансер по pricing.py ($5/$0.5/$30): 78% счёта.
    tasks = [
        _priced("a" * 32, "agent-lb/gpt-6.1-sol", 0.214),
        _priced("b" * 32, "agent-lb/gpt-5.6-sol", 0.290),
        _priced("c" * 32, "agent-lb/gpt-5.6-sol", 0.05),
        # Сессии нет в учёте балансера: copilot-x5 идёт мимо agent-lb.
        _priced("d" * 32, "copilot-x5/copilot-code-large", 0.0),
        # Без id сессии сверять не с чем.
        _priced("e" * 32, "agent-lb/gpt-6.1-sol", 0.1, session=None),
        # Отказ guard'а не запуск, хотя id и цена в результате есть.
        replace(_priced("f" * 32, "agent-lb/gpt-6.1-sol", 0.3, status="failed"), executor=""),
    ]
    billed = {
        "a" * 32: {"cost_usd": 0.214, "requests": 17},
        "b" * 32: {"cost_usd": 0.372, "requests": 13},
        # Балансер насчитал ноль, CLI нет: отношение не определено, это расхождение.
        "c" * 32: {"cost_usd": 0.0, "requests": 2},
        "f" * 32: {"cost_usd": 9.0, "requests": 1},
    }

    report = cost_drift(tasks, billed)

    assert (report["compared"], report["not_in_lb"], report["drifted"]) == (3, 1, 2)
    assert [(run["task_id"], run["ratio"]) for run in report["runs"]] == [
        ("b" * 8, 0.78),
        ("c" * 8, None),
    ]
    assert report["by_model"]["agent-lb/gpt-5.6-sol"] == {
        "tasks": 2,
        "reported_usd": 0.34,
        "billed_usd": 0.372,
        "ratio": 0.914,
    }
    assert report["by_model"]["agent-lb/gpt-6.1-sol"]["ratio"] == 1.0
    text = render({**replay([]), "cost_drift": report})
    assert "reported cost vs agent-lb (tolerance 10%): 3 compared, 2 drifted" in text
    assert "  agent-lb/gpt-5.6-sol: 2 tasks, reported $0.340, billed $0.372, ratio 0.914" in text
    assert f"  {'b' * 8} agent-lb/gpt-5.6-sol: reported $0.290, billed $0.372 (0.78)" in text
    assert f"  {'c' * 8} agent-lb/gpt-5.6-sol: reported $0.050, billed $0.000 (n/a)" in text
    assert recommendations({"cost_drift": report})[0]["metric"] == "cost_drift"


def test_cost_drift_section_says_why_it_was_skipped():
    text = render({**replay([]), "cost_drift": {"skipped": "no agent-lb db at /nope"}})

    assert "reported cost vs agent-lb: skipped (no agent-lb db at /nope)" in text


def test_agent_lb_costs_sum_billed_requests_per_session(tmp_path):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "create table request_logs (id integer primary key, client_session_id varchar,"
        " cost_usd float, status varchar not null, deleted_at datetime)"
    )
    conn.executemany(
        "insert into request_logs (client_session_id, cost_usd, status, deleted_at)"
        " values (?, ?, ?, ?)",
        [
            ("s1", 0.1, "success", None),
            ("s1", 0.2, "success", None),
            # Ошибка без токенов: цены нет, но запрос был.
            ("s1", None, "error", None),
            # Скрытый из дашборда запрос всё равно оплачен.
            ("s1", 0.05, "success", "2026-11-03"),
            ("s2", 0.5, "success", None),
            ("other", 1.0, "success", None),
        ],
    )
    conn.commit()
    conn.close()

    costs = agent_lb_costs(path, ["s1", "s2", "s3", "s1"])

    assert set(costs) == {"s1", "s2"}
    assert costs["s1"]["requests"] == 4
    assert costs["s1"]["cost_usd"] == pytest.approx(0.35)
    assert costs["s2"] == {"cost_usd": 0.5, "requests": 1}
    assert agent_lb_costs(path, []) == {}


def test_cost_drift_is_skipped_without_a_readable_agent_lb_db(tmp_path):
    from evals.replay.__main__ import _cost_drift

    tasks = [_priced("a" * 32, "agent-lb/gpt-6.1-sol", 0.1)]
    assert _cost_drift(tasks, tmp_path / "missing.db") == {
        "skipped": f"no agent-lb db at {tmp_path / 'missing.db'}"
    }
    # Старый agent-lb без `client_session_id`: секция пропускается, replay не падает.
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("create table request_logs (id integer primary key, cost_usd float)")
    conn.close()
    assert _cost_drift(tasks, old)["skipped"].startswith("agent-lb db unreadable: ")
    # Без сессий в задачах базу не открываем вовсе.
    assert _cost_drift([], old)["compared"] == 0


def test_post_review_counts_verdicts_skips_and_chain_outcomes():
    def edited(task_id, status, verdict, *, child=None, reason=None, escalated_to=None):
        review = {"by": "omp/sol", "task_id": child, "verdict": verdict, "summary": "s"}
        if reason:
            review["reason"] = reason
        meta = {"review": review}
        if escalated_to:
            meta["escalated_to"] = escalated_to
        return _task(task_id, status, result=_result(status, changed=["a.py"], meta=meta))

    def reviewer(task_id, parent, cost):
        result = {**_result("completed"), "usage": {"cost_usd": cost}}
        task = _task(task_id, "completed", executor="omp/sol", result=result)
        return replace(task, kind="review", parent_task_id=parent)

    a, b, c, d, e, f, g, h = (letter * 32 for letter in "abcdefgh")
    tasks = [
        edited(a, "completed", "pass", child="r1"),
        reviewer("r1", a, 0.1),
        # fail → следующее звено довело до completed.
        edited(b, "partial", "fail", child="r2", escalated_to=c),
        reviewer("r2", b, 0.25),
        _task(c, "completed", parent=b),
        # fail → цепочка кончилась ничем.
        edited(d, "partial", "fail", child="r3", escalated_to=e),
        reviewer("r3", d, 0.05),
        _task(e, "failed", parent=d, result=_result("failed", error="exit 1")),
        edited(f, "completed", "skipped", reason="max_hops: hop 2 >= 2"),
        edited(g, "completed", "unknown", child="r4"),
        edited(h, "completed", "skipped", reason="perimeter: corporate perimeter required"),
        # Ревью, которое вызывающий заказал сам, к post_review не относится.
        replace(_task("z" * 32, "completed", result=_result("completed")), kind="review"),
    ]
    report = post_review(tasks)

    assert report["reviews"] == 6
    assert report["verdicts"] == {"pass": 1, "fail": 2, "unknown": 1, "skipped": 2}
    assert report["skipped_reasons"] == {"max_hops": 1, "perimeter": 1}
    assert report["review_failed"] == {"escalated": 2, "completed": 1, "not_completed": 1}
    assert report["cost_usd"] == 0.4
    assert report["skipped_share"] == pytest.approx(2 / 6)

    text = render({**replay([]), "post_review": report})
    assert "post-merge reviews by review_by: 6, pass 1, fail 2, unknown 1, skipped 2" in text
    assert "skipped: max_hops 1, perimeter 1" in text
    assert "review_failed escalations 2: completed 1, not completed 1" in text
    items = recommendations({"post_review": report})
    assert [item["metric"] for item in items] == ["post_review.skipped_share"]
    assert recommendations({"post_review": post_review(tasks[:2])}) == []


def test_post_review_counts_a_chain_with_two_failed_reviews_once():
    def failed(task_id, *, parent=None, escalated_to=None):
        meta = {"review": {"by": "omp/sol", "task_id": None, "verdict": "fail", "summary": ""}}
        if escalated_to:
            meta["escalated_to"] = escalated_to
        result = _result("partial", changed=["a.py"], meta=meta)
        return _task(task_id, "partial", parent=parent, result=result)

    a, b, c = ("a" * 32, "b" * 32, "c" * 32)
    # Оба звена провалили ревью, третье довело до completed: цепочка одна.
    tasks = [
        failed(a, escalated_to=b),
        failed(b, parent=a, escalated_to=c),
        _task(c, "completed", parent=b),
    ]
    report = post_review(tasks)

    assert report["verdicts"]["fail"] == 2
    assert report["review_failed"] == {"escalated": 1, "completed": 1, "not_completed": 0}
