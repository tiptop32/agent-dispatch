"""Replay правил диспатчера по телеметрии: синтетическая база с известным ответом."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from evals.replay.harness import load, render, replay

SCHEMA = Path(__file__).parent.parent / "agent_dispatch" / "telemetry" / "schema.sql"
OFFLINE = '{"type":"error","message":"Reconnecting... 5/5 (request timed out)"}'


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
                _result("failed", error="timeout", summary=OFFLINE),
            ),
            # Через 5 минут его же снова берут: этот запуск остывание сняло бы.
            (
                "b" * 32,
                "codex/sol",
                "10:35",
                "11:05",
                30,
                None,
                _result("failed", error="timeout", summary=OFFLINE),
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
                _result("failed", error="timeout", summary=OFFLINE),
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
                _result("failed", error="timeout", summary=OFFLINE),
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
                _result("failed", error="timeout", summary=OFFLINE),
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
                _result("failed", error="timeout", summary=OFFLINE),
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
                _result("failed", error="timeout", summary=OFFLINE),
            ),
            ("b" * 32, "codex/sol", "13:00", "13:01", 1, None, _result("completed"), "claude"),
            (
                "c" * 32,
                "codex/sol",
                "13:30",
                "13:31",
                1,
                None,
                _result("failed", error="timeout", summary=OFFLINE),
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
