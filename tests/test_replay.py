"""Replay правил диспатчера по телеметрии: синтетическая база с известным ответом."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.models import ExecutionResult
from evals.replay.harness import (
    Acceptance,
    Decision,
    Task,
    acceptance,
    added_lines,
    delegations,
    git_acceptance,
    load,
    load_decisions,
    render,
    replay,
    router_vs_static,
)

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


def _task(task_id, status, *, executor="codex/sol", parent=None, minutes=1, result=None, cwd="/r"):
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

    def check(cwd, files, patch, since, until):
        assert until - since == timedelta(hours=48)
        return verdicts[cwd]

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
        # Не дошло до рабочей копии: судить вызывающего не за что.
        done("e" * 32, "/held", integrated=False),
        # Ревью без изменений файлов.
        _task("f" * 32, "completed", cwd="/review"),
    ]
    verdicts = {
        "/kept": Acceptance(committed=True, added=10, survived=9),
        "/rewritten": Acceptance(committed=True, added=10, survived=1),
        "/dropped": Acceptance(committed=False),
        "/gone": None,
    }
    report = acceptance(tasks, _accepted(verdicts))

    assert (report["diffs"], report["committed"], report["not_committed"], report["unknown"]) == (
        4,
        2,
        1,
        1,
    )
    assert report["committed_share"] == 0.667
    assert (report["kept"], report["reworked"], report["rewritten"]) == (1, 0, 1)
    assert report["lines_survived_share"] == 0.5
    text = render({**replay([]), "acceptance": report})
    assert "diffs committed by the caller within 48 h: 2 of 3 (67%), unknown 1" in text
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
    assert git_acceptance(str(git_repo), ["a.py"], patch, since, until) == Acceptance(
        committed=False
    )

    commit("caller keeps half", "x = 1\nvalue = compute(x)\n")
    assert git_acceptance(str(git_repo), ["a.py"], patch, since, until) == Acceptance(
        committed=True, added=2, survived=1
    )
    sub = git_repo / "sub"
    sub.mkdir()
    assert git_acceptance(str(sub), ["a.py"], patch, since, until) == Acceptance(
        committed=True, added=2, survived=1
    )
    assert git_acceptance(str(tmp_path / "missing"), ["a.py"], patch, since, until) is None
    assert git_acceptance(str(tmp_path), ["a.py"], patch, since, until) is None


def _settings(adapter, tier, *, corporate=False):
    return ExecutorSettings(
        adapter=adapter,
        tier=tier,
        corporate=corporate,
        model="m" if adapter == "opencode" else None,
    )


EXECUTORS = {
    "opencode/x5": _settings("opencode", "balanced", corporate=True),
    "codex/luna": _settings("codex", "fast"),
    "codex/sol": _settings("codex", "balanced"),
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
    conn.commit()
    conn.close()

    assert load(db)[0].cwd == "/r"
    assert load_decisions(db) == [
        Decision(
            task_id="a" * 32,
            router="jev",
            candidates=["codex/sol"],
            choice="codex/sol",
            guard_reason=None,
            judgments={"capability": {"value": "fast"}},
        )
    ]
