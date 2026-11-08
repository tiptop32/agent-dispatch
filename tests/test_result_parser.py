from pathlib import Path

import pytest

from agent_dispatch.executors.process import ProcessOutcome
from agent_dispatch.executors.result_parser import extract_result_block, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "agent_output"


def outcome(**kwargs):
    values = {"exit_code": 0, "stdout": "out", "stderr": "", "timed_out": False, "duration_ms": 12}
    values.update(kwargs)
    return ProcessOutcome(**values)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_extract_one_block():
    assert extract_result_block(fixture("one.txt"))["summary"] == "ok"


def test_extract_two_blocks_returns_last():
    assert extract_result_block(fixture("two.txt"))["summary"] == "new"


def test_extract_ignores_musor_before_and_after():
    assert extract_result_block(fixture("musor.txt"))["status"] == "completed"


def test_extract_without_closing_backticks_returns_none():
    assert extract_result_block(fixture("unclosed.txt")) is None


def test_extract_invalid_json_returns_none():
    assert extract_result_block(fixture("invalid.txt")) is None


def test_extract_json_language_without_tag_returns_none():
    assert extract_result_block(fixture("json_language.txt")) is None


def test_extract_empty_text_returns_none():
    assert extract_result_block(fixture("empty.txt")) is None


def test_extract_json_array_returns_none():
    assert extract_result_block("```agent-dispatch-result\n[]\n```") is None


def test_normalize_valid_block_copies_all_fields():
    raw = {
        "status": "completed",
        "summary": "ok",
        "changed_files": ["raw.py"],
        "tests": {"command": "pytest", "result": "passed"},
        "confidence": 0.9,
        "needs_escalation": True,
    }
    result = normalize(raw, outcome(), None, "omp", "m")
    assert result.status == "completed"
    assert result.summary == "ok"
    assert result.changed_files == ["raw.py"]
    assert result.tests.command == "pytest" and result.tests.result == "passed"
    assert result.confidence == 0.9
    assert result.needs_escalation is True


def test_normalize_no_block_exit_zero_is_partial_with_stdout_tail():
    stdout = "x" * 5000
    result = normalize(None, outcome(stdout=stdout), None, "omp", None)
    assert result.status == "partial"
    assert result.summary == stdout[-2000:]
    assert len(result.summary) == 2000
    assert result.meta["parse_error"] == "no result block"


def test_normalize_nonzero_exit_is_failed_with_error_and_stdout_tails():
    stdout, stderr = "o" * 5000, "e" * 5000
    result = normalize(None, outcome(exit_code=3, stdout=stdout, stderr=stderr), None, "omp", None)
    assert result.status == "failed"
    assert result.error == stderr[-2000:]
    assert result.summary == stdout[-2000:]


def test_normalize_timeout_is_failed_with_timeout_error():
    result = normalize(
        None,
        outcome(timed_out=True, last_output_age_s=4.5, timeout_seconds=30),
        None,
        "omp",
        None,
    )
    assert result.status == "failed"
    assert result.error == "timeout"
    assert result.meta["failure_cause"] == "timeout"
    assert result.meta["timeout_type"] == "task_deadline"
    assert result.meta["last_activity_age_s"] == 4.5
    assert result.meta["timeout_seconds"] == 30


def test_normalize_timeout_with_changed_files_is_partial():
    result = normalize(None, outcome(timed_out=True), ["a.py"], "omp", None)
    assert result.status == "partial"
    assert result.error == "timeout"
    assert result.changed_files == ["a.py"]
    assert result.meta["timed_out"] is True


def test_normalize_stalled_is_failed_with_idle_error():
    result = normalize(None, outcome(stalled=True, idle_seconds=0.5), None, "omp", None)
    assert result.status == "failed"
    assert result.error.startswith("stalled: no output for")
    assert result.meta["failure_cause"] == "stream_stalled"
    assert result.meta["timeout_type"] == "stream_stalled"
    assert result.meta["idle_timeout_seconds"] == 0.5


def test_normalize_timeout_has_machine_readable_timing_without_output():
    result = normalize(
        None,
        outcome(
            timed_out=True,
            duration_ms=1234,
            last_output_age_s=4.2,
            timeout_seconds=10,
            stdout="task output that stays in the summary",
        ),
        None,
        "omp",
        None,
    )
    assert result.meta == {
        "exit_code": 0,
        "duration_ms": 1234,
        "last_output_age_s": 4.2,
        "last_activity_age_s": 4.2,
        "timeout_seconds": 10,
        "failure_cause": "timeout",
        "timeout_type": "task_deadline",
        "timed_out": True,
    }
    assert "prompt" not in result.meta


def test_normalize_stall_has_machine_readable_timing_without_output():
    result = normalize(
        None,
        outcome(
            stalled=True,
            idle_seconds=2.5,
            duration_ms=2500,
            last_output_age_s=2.5,
            stdout="task output that stays in the summary",
        ),
        None,
        "omp",
        None,
    )
    assert result.meta["failure_cause"] == "stream_stalled"
    assert result.meta["stalled"] is True
    assert result.meta["idle_timeout_seconds"] == 2.5
    assert result.meta["last_output_age_s"] == 2.5
    assert result.meta["duration_ms"] == 2500
    assert "prompt" not in result.meta


def test_normalize_git_changed_files_override_agent_files():
    raw = {"status": "completed", "summary": "ok", "changed_files": ["raw.py"]}
    assert normalize(raw, outcome(), ["a.py"], "omp", None).changed_files == ["a.py"]


def test_normalize_empty_git_changed_files_override_agent_files():
    raw = {"status": "completed", "summary": "ok", "changed_files": ["raw.py"]}
    assert normalize(raw, outcome(), [], "omp", None).changed_files == []


def test_normalize_none_git_changed_files_uses_agent_files():
    raw = {"status": "completed", "summary": "ok", "changed_files": ["raw.py"]}
    assert normalize(raw, outcome(), None, "omp", None).changed_files == ["raw.py"]


def test_normalize_invalid_status_is_partial_with_parse_error():
    raw = {"status": "running", "summary": "still working"}
    result = normalize(raw, outcome(), None, "omp", None)
    assert result.status == "partial"
    assert result.meta["parse_error"]


@pytest.mark.parametrize(
    "status",
    ["success", "SUCCEEDED", "successful", "done", "complete", "ok"],
)
def test_normalize_coerces_success_status_synonyms(status):
    result = normalize({"status": status, "summary": "ok"}, outcome(), None, "omp", None)

    assert result.status == "completed"
    assert result.meta["coerced"] == [f"status: {status!r} -> completed"]


@pytest.mark.parametrize("status", ["error", "FAILURE", "fail"])
def test_normalize_coerces_failure_status_synonyms(status):
    result = normalize({"status": status, "summary": "bad"}, outcome(), None, "omp", None)

    assert result.status == "failed"
    assert result.meta["coerced"] == [f"status: {status!r} -> failed"]


def test_normalize_unknown_status_still_is_partial_with_parse_error():
    result = normalize({"status": "banana", "summary": "unknown"}, outcome(), None, "omp", None)

    assert result.status == "partial"
    assert result.meta["parse_error"]


def test_normalize_extra_field_is_partial_with_parse_error():
    raw = {"status": "completed", "summary": "ok", "unexpected": True}
    result = normalize(raw, outcome(), None, "omp", None)
    assert result.status == "partial"
    assert result.meta["parse_error"]


def test_normalize_meta_always_contains_exit_and_duration():
    result = normalize(None, outcome(exit_code=3, duration_ms=44), None, "omp", None)
    assert result.meta["exit_code"] == 3
    assert result.meta["duration_ms"] == 44


def test_normalize_missing_tests_is_none():
    raw = {"status": "completed", "summary": "ok"}
    assert normalize(raw, outcome(), None, "omp", None).tests is None


def test_normalize_coerces_natural_language_tests_result():
    raw = {
        "status": "completed",
        "summary": "ok",
        "tests": {"command": "pytest", "result": "1 passed"},
    }
    result = normalize(raw, ProcessOutcome(0, "", "", False, 1), ["a.py"], "claude", None)
    assert result.status == "completed"
    assert result.tests is not None and result.tests.result == "passed"
    assert result.meta["coerced"] == ["tests.result: '1 passed' -> passed"]


def test_normalize_coerces_failed_wording():
    raw = {
        "status": "partial",
        "summary": "x",
        "tests": {"command": "pytest", "result": "2 failed, 1 passed"},
    }
    result = normalize(raw, ProcessOutcome(0, "", "", False, 1), [], "claude", None)
    assert result.tests is not None and result.tests.result == "failed"


def _block(**fields):
    return {"status": "completed", "summary": "done", **fields}


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        pytest.param("high", 0.9, id="word"),
        pytest.param("Low", 0.3, id="word-case"),
        pytest.param("0.8", 0.8, id="numeric-string"),
        pytest.param("85%", 0.85, id="percent-string"),
        pytest.param(85, 0.85, id="percent-number"),
    ],
)
def test_normalize_coerces_confidence_instead_of_losing_the_report(confidence, expected):
    # 09-29: готовая работа с "confidence": "high" вернулась как partial.
    result = normalize(_block(confidence=confidence), outcome(), ["a.py"], "x", None)
    assert result.status == "completed"
    assert result.confidence == pytest.approx(expected)
    assert result.meta["coerced"] == [f"confidence: {confidence!r} -> {expected}"]


@pytest.mark.parametrize("confidence", ["very sure", 250, True, -1])
def test_normalize_drops_unusable_confidence_and_keeps_the_report(confidence):
    result = normalize(_block(confidence=confidence), outcome(), ["a.py"], "x", None)
    assert result.status == "completed" and result.confidence is None
    assert result.meta["coerced"] == [f"confidence: {confidence!r} -> dropped"]


def test_normalize_drops_nulls():
    # 09-24: исполнитель написал null в необязательных полях, отчёт стал partial.
    raw = _block(
        changed_files=None,
        tests={"command": None, "result": "not_run"},
        confidence=None,
        needs_escalation=None,
    )
    result = normalize(raw, outcome(), ["done.txt"], "omp/luna", None)
    assert result.status == "completed" and "parse_error" not in result.meta
    assert result.tests.result == "not_run" and result.tests.command is None
    assert result.needs_escalation is False
    assert result.meta["coerced"] == [
        "changed_files: None -> dropped",
        "confidence: None -> dropped",
        "needs_escalation: None -> dropped",
        "tests.command: None -> dropped",
    ]


def test_normalize_valid_confidence_is_not_reported_as_coerced():
    result = normalize(_block(confidence=0.7), outcome(), [], "x", None)
    assert result.confidence == 0.7 and "coerced" not in result.meta


def test_normalize_reads_the_review_verdict():
    raw = {"status": "completed", "summary": "a.py:1 bug", "verdict": "fail"}
    assert normalize(raw, outcome(), None, "omp", None).verdict == "fail"
    # Старые отчёты без поля остаются валидными.
    old = normalize({"status": "completed", "summary": "ok"}, outcome(), None, "omp", None)
    assert old.status == "completed" and old.verdict is None


def test_normalize_rejects_an_unknown_verdict():
    raw = {"status": "completed", "summary": "ok", "verdict": "maybe"}
    result = normalize(raw, outcome(), None, "omp", None)
    assert result.status == "partial" and "'maybe' is not one of" in result.meta["parse_error"]


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (["поправил a.py", "добавил тест"], "поправил a.py\nдобавил тест"),
        ({"verdict": "ок", "notes": 2}, '{"verdict": "ок", "notes": 2}'),
        (42, "42"),
    ],
    ids=["list", "object", "number"],
)
def test_normalize_turns_a_non_string_summary_into_text(summary, expected):
    # Живой случай 2026-11-04: ревью omp/sol вернуло `summary` списком, и разбор
    # падал `ValidationError`, теряя отчёт целиком.
    raw = {"status": "completed", "summary": summary, "verdict": "pass"}
    result = normalize(raw, outcome(), ["a.py"], "omp", None)

    assert result.status == "completed"
    assert result.summary == expected
    assert result.verdict == "pass"
    assert result.meta["coerced"] == [f"summary: {type(summary).__name__} -> str"]


def test_normalize_non_string_summary_in_an_invalid_report_stays_partial():
    raw = {"status": "running", "summary": ["шаг 1", "шаг 2"]}
    result = normalize(raw, outcome(), None, "omp", None)

    assert result.status == "partial"
    assert result.summary == "шаг 1\nшаг 2"
    assert result.meta["parse_error"]


def test_normalize_turns_a_non_string_tests_command_into_text():
    raw = {
        "status": "completed",
        "summary": "ok",
        "tests": {"command": ["uv run pytest", "uv run ruff check ."], "result": "passed"},
    }
    result = normalize(raw, outcome(), None, "omp", None)

    assert result.status == "completed"
    assert result.tests.command == "uv run pytest\nuv run ruff check ."
    assert result.tests.result == "passed"


@pytest.mark.parametrize(
    ("value", "expected", "kind"),
    [
        pytest.param(True, "not_run", "bool", id="bool"),
        pytest.param(["3 passed"], "passed", "list", id="list"),
        pytest.param({"failed": 2}, "failed", "dict", id="dict"),
    ],
)
def test_normalize_coerces_non_string_tests_result(value, expected, kind):
    raw = _block(tests={"command": "pytest", "result": value})
    result = normalize(raw, outcome(), [], "omp", None)
    assert result.status == "completed" and "parse_error" not in result.meta
    assert result.tests.result == expected
    assert f"tests.result: {kind} -> str" in result.meta["coerced"]


def test_normalize_coerces_non_string_changed_files_items():
    raw = _block(changed_files=["a.py", 7, {"path": "b.py"}])
    result = normalize(raw, outcome(), None, "omp", None)
    assert result.status == "completed" and "parse_error" not in result.meta
    assert result.changed_files == ["a.py", "7", '{"path": "b.py"}']
    assert result.meta["coerced"] == ["changed_files: dict, int -> str"]
