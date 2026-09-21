from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent_dispatch.models import (
    Availability,
    ContextMode,
    DispatchRequest,
    ExecutionResult,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
    SourceAgent,
    TaskRecord,
    TaskStatus,
    TaskView,
    Usage,
)
from agent_dispatch.schemas import load_agent_result_schema, validate_agent_result


def request_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"task": "Fix the tests", "cwd": "/tmp/repo"}
    data.update(overrides)
    return data


def test_dispatch_request_rejects_empty_task() -> None:
    with pytest.raises(ValidationError):
        DispatchRequest(**request_data(task=""))


def test_dispatch_request_rejects_negative_wait_seconds() -> None:
    with pytest.raises(ValidationError):
        DispatchRequest(**request_data(wait_seconds=-1))


def test_dispatch_request_hop_defaults_to_zero() -> None:
    assert DispatchRequest(**request_data()).hop == 0


@pytest.mark.parametrize("file_name", ["../secret", "src/../secret", "/etc/passwd"])
def test_dispatch_request_rejects_unsafe_file_paths(file_name: str) -> None:
    with pytest.raises(ValidationError):
        DispatchRequest(**request_data(files=[file_name]))


def test_context_mode_prompt_summary_parses() -> None:
    assert ContextMode("prompt+summary") is ContextMode.prompt_summary


def test_execution_result_round_trips_json() -> None:
    result = ExecutionResult(
        status="completed",
        executor="codex",
        model=None,
        summary="Fixed",
        changed_files=["a.py"],
    )

    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result


def test_execution_result_rejects_non_terminal_status() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(status="queued", executor="codex", model=None, summary="Not finished")


def test_optional_result_details_default_to_none() -> None:
    from agent_dispatch.models import TestsInfo as ExecutionTestsInfo

    now = datetime.now(UTC)

    availability = Availability(available=True, checked_at=now)

    assert availability.version is None
    assert availability.error is None
    assert ExecutionTestsInfo() == ExecutionTestsInfo(command=None, result=None, output_tail=None)
    assert Usage() == Usage(input_tokens=None, output_tokens=None, cost_usd=None)


def test_agent_result_schema_is_valid_draft_2020_12() -> None:
    schema = load_agent_result_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_agent_result_schema_accepts_required_fields() -> None:
    assert validate_agent_result({"status": "completed", "summary": "Done"}) == []


def test_agent_result_schema_rejects_additional_field() -> None:
    errors = validate_agent_result({"status": "completed", "summary": "Done", "unexpected": True})

    assert errors


def test_agent_result_schema_rejects_running_status() -> None:
    errors = validate_agent_result({"status": "running", "summary": "Working"})

    assert errors


def test_remaining_domain_models_compose_into_task_view() -> None:
    now = datetime.now(UTC)
    request = DispatchRequest(**request_data())
    decision = RouteDecision(
        executor="codex",
        confidence=0.9,
        scores={"codex": 0.9},
        router=RouterKind.jev,
    )
    result = ExecutionResult(status="completed", executor="codex", model=None, summary="Done")
    record = TaskRecord(
        task_id="task-1",
        parent_task_id=None,
        escalated_from=None,
        root_agent=SourceAgent.cli,
        source_agent=SourceAgent.cli,
        hop=0,
        request=request,
        status=TaskStatus.completed,
        decision=decision,
        result=result,
        log_path="/tmp/task-1.log",
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    view = TaskView(**record.model_dump(), log_tail="finished")
    guard = GuardEvent(reason=GuardReason.single_candidate, detail="only codex")
    availability = Availability(available=True, version="1.0", error=None, checked_at=now)

    assert view.log_tail == "finished"
    assert guard.reason is GuardReason.single_candidate
    assert availability.available is True
