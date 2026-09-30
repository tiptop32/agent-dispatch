"""Replay правил диспатчера по телеметрии: синтетическая база с известным ответом."""

from __future__ import annotations

import json
import sqlite3
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
    for task_id, executor, created, finished, minutes, parent, result in rows:
        conn.execute(
            "insert into tasks (task_id, escalated_from, root_agent, source_agent, hop, cwd,"
            " status, executor, request_json, result_json, log_path, created_at, finished_at,"
            " duration_ms) values (?, ?, 'claude', 'claude', 0, '/r', ?, ?, '{}', ?, '', ?, ?, ?)",
            (
                task_id,
                parent,
                result["status"],
                executor,
                json.dumps(result),
                f"2026-09-23T{created}:00+00:00",
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
    report = replay(load(db), cooldown_seconds=900)

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
