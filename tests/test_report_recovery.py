"""Дозапрос отчёта: когда он нужен и как его итог ложится на исходный запуск."""

from __future__ import annotations

import pytest

from agent_dispatch.dispatch.report_recovery import merge_report, needs_report
from agent_dispatch.models import ExecutionResult, Usage
from agent_dispatch.models import TestsInfo as ResultTestsInfo


def _missing(**update) -> ExecutionResult:
    """Запуск, у которого работа в дереве есть, а блока отчёта нет."""
    result = ExecutionResult(
        status="partial",
        executor="opencode/x5",
        model="m",
        summary="stdout tail",
        changed_files=["a.py", "b.py"],
        usage=Usage(input_tokens=1000, output_tokens=200, cost_usd=0.0),
        meta={
            "parse_error": "no result block",
            "session_id": "ses_1",
            "log_path": "/logs/t.log",
            "duration_ms": 60000,
        },
    )
    return result.model_copy(update=update)


def _report(**update) -> ExecutionResult:
    result = ExecutionResult(
        status="completed",
        executor="opencode/x5",
        model="m",
        summary="added the endpoint",
        changed_files=[],
        tests=ResultTestsInfo(command="pytest", result="passed"),
        confidence=0.9,
        usage=Usage(input_tokens=300, output_tokens=50, cost_usd=0.0),
        meta={"session_id": "ses_1", "log_path": "/logs/t.log", "duration_ms": 4000},
    )
    return result.model_copy(update=update)


def test_missing_report_with_work_and_a_session_is_asked_again():
    assert needs_report(_missing())


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(_missing(changed_files=[]), id="no-work: escalates as no_result"),
        pytest.param(_missing(error="timeout"), id="killed-by-timeout"),
        pytest.param(_missing(status="completed"), id="already-reported"),
        pytest.param(
            _missing(meta={"parse_error": "'high' is not of type 'number'", "session_id": "s"}),
            id="block-present-but-invalid",
        ),
        pytest.param(_missing(meta={"parse_error": "no result block"}), id="no-session"),
    ],
)
def test_other_results_are_not_asked_again(result):
    assert not needs_report(result)


def test_recovered_report_takes_status_and_keeps_the_work_of_the_first_run():
    merged = merge_report(_missing(), _report(changed_files=["b.py", "c.py"]))

    assert merged.status == "completed"
    assert merged.summary == "added the endpoint"
    assert merged.tests == ResultTestsInfo(command="pytest", result="passed")
    assert merged.changed_files == ["a.py", "b.py", "c.py"]
    assert merged.usage == Usage(input_tokens=1300, output_tokens=250, cost_usd=0.0)
    assert merged.meta["report_recovery"] == "recovered"
    assert "parse_error" not in merged.meta
    assert merged.meta["duration_ms"] == 60000  # метаданные запуска от первого


def test_recovered_report_keeps_its_coercions():
    merged = merge_report(_missing(), _report(meta={"coerced": ["confidence: 'high' -> 0.9"]}))
    assert merged.meta["coerced"] == ["confidence: 'high' -> 0.9"]


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        pytest.param(
            _report(status="partial", meta={"parse_error": "no result block"}),
            "failed: no result block",
            id="still-no-block",
        ),
        pytest.param(
            _report(status="failed", error="No cookie auth credentials found"),
            "failed: No cookie auth credentials found",
            id="cli-error",
        ),
    ],
)
def test_failed_recovery_keeps_the_original_partial(report, reason):
    original = _missing()
    merged = merge_report(original, report)

    assert merged.status == "partial"
    assert merged.summary == original.summary
    assert merged.changed_files == original.changed_files
    assert "executor_failure" not in merged.meta
    assert merged.meta["parse_error"] == "no result block"
    assert merged.meta["report_recovery"] == reason
    assert merged.usage.input_tokens == 1300  # дозапрос оплачен и без отчёта


def test_failed_recovery_keeps_files_it_touched_and_its_cooldown():
    report = _report(
        status="failed",
        error="timeout",
        changed_files=["c.py"],
        meta={"executor_failure": "network", "cooldown_until": "2026-10-21T13:00:00+00:00"},
    )
    merged = merge_report(_missing(), report)

    assert merged.changed_files == ["a.py", "b.py", "c.py"]
    assert merged.meta["executor_failure"] == "network"
    assert merged.meta["cooldown_until"] == "2026-10-21T13:00:00+00:00"


def test_usage_survives_when_one_run_did_not_report_it():
    assert merge_report(_missing(usage=None), _report()).usage.input_tokens == 300
