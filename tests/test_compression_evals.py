"""Consumer-visible regressions for the frozen compression evaluation harness."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_dispatch.dispatch import report_recovery
from agent_dispatch.dispatch.context_blocks import split_context
from agent_dispatch.dispatch.context_compression import CompressionInput, compress_context
from agent_dispatch.mcp.client import DaemonUnavailable
from agent_dispatch.models import ExecutionResult, TaskStatus, TestsInfo, Usage
from agent_dispatch.serve_state import ServeState
from agent_dispatch.telemetry.storage import Storage
from evals.compression import __main__ as cli
from evals.compression import harness
from evals.compression.__main__ import parser


@pytest.fixture(scope="module")
def cases():
    return harness.load_corpus()


def case_data(cases, case_id):
    return next(case.model_dump() for case in cases if case.id == case_id)


async def test_all_frozen_rows_reach_real_selector_or_dispatcher(cases, tmp_path):
    rows = await harness.run_offline(cases, tmp_path)
    failures = {row["id"]: row["reasons"] for row in rows if not row["ok"]}
    assert failures == {}
    assert {row["id"] for row in rows} == {case.id for case in cases}
    report = harness.report_document("offline", rows)
    assert report["ok"] is True
    assert report["corpus_revision"] == 3
    assert report["corpus_sha256"] == (
        "f61fc184f2071540b881baf1a643ffb72"  # pragma: allowlist secret
        "f7e4ed673080b20f32976827ee1a66d"  # pragma: allowlist secret
    )
    assert report["threshold"] == 0.60
    # Recorded envelopes intentionally omit usage; passing replay is not measured free Jev.
    assert report["accounting"]["cost_usd"]["total"] is None
    assert report["accounting"]["cost_usd"]["unknown_components"] > 0
    duplicate = next(row for row in rows if row["id"] == "sem-03-duplicate")
    assert [block["id"] for block in duplicate["transitions"][1]["blocks"]] == ["b0001", "b0002"]
    assert duplicate["kept_ids"] == ["b0001"]
    controls = {row["id"]: row for row in rows if row["kind"] == "control"}
    assert controls["control-12-cancel-inflight"]["ledger"][1]["status"] == "cancelled"
    assert controls["control-13-deadline"]["observed"]["current_http_cancelled"] is True
    assert controls["control-19-pinned-service"]["observed"]["executor_started"] is True
    assert controls["control-20-never-run-child"]["observed"]["followup_selected_task_id"] == (
        "synthetic-ran-parent"
    )


async def test_lost_exception_fails_even_when_recorded_keep_labels_agree(cases, tmp_path):
    value = case_data(cases, "sem-02-exception")
    # Corrupted labels agree with the destructive selection. Facts remain independent.
    value["recorded_steps"][1]["response"]["answers"]["delete_safe"]["noul"] = 0.99
    value["recorded_steps"][2]["retained_ids_before"] = ["b0000", "b0002"]
    value["expected_kept_ids"] = ["b0000"]
    case = harness.validate_case(value)
    row = await harness.select_case(case, harness.offline_settings(tmp_path), tmp_path)
    assert row["ok"] is False
    assert row["required_facts_missing"] == ["f2"]
    assert any("Required facts lost" in reason for reason in row["reasons"])


async def test_replay_detects_stale_current_retained_set(cases, tmp_path):
    value = case_data(cases, "sem-03-duplicate")
    value["recorded_steps"][1]["retained_ids_before"].insert(0, "b0000")
    case = harness.validate_case(value)
    row = await harness.select_case(case, harness.offline_settings(tmp_path), tmp_path)
    assert row["ok"] is False
    assert "Current retained state mismatch" in row["reasons"][0]


def test_wrong_output_beats_success_report_and_assertions_are_withheld(cases, tmp_path):
    case = next(case for case in cases if case.id == "code-13-safe-retry")
    repo = tmp_path / "repo"
    harness.materialize(case, repo, git=False)
    (repo / "solution.py").write_text(
        "def retryable(method, status, ambiguous):\n"
        "    return method.upper() in ('GET', 'HEAD') and status in (429, 502, 503)\n"
    )
    request = harness.paired_request(case, repo, "selected-omp", "treatment", 60)
    wire = request.model_dump(mode="json")
    assert set(path.name for path in repo.iterdir()) == {"solution.py"}
    assert "assertion" not in wire and "required_facts" not in wire
    assert "recorded_steps" not in wire and "expected_kept_ids" not in wire
    assert case.assertion.source not in json.dumps(wire)
    check = harness.check_candidate(case, repo, tmp_path / "withheld")
    assert check["executed"] is True
    assert check["exit_code"] != 0
    result = ExecutionResult(
        status="completed",
        executor="selected-omp",
        model="pinned-model",
        summary="All tests passed",
        tests=TestsInfo(command="my tests", result="passed"),
    )
    reasons = harness.coding_verdict(result, check, {"complete": True}, [], [])
    assert reasons == ["Independent check missing, timed out or failed"]
    assert not (repo / "checker.py").exists()


@pytest.mark.parametrize(
    "check",
    [
        None,
        {},
        {"ok": True},
        {"executed": False, "ok": True, "exit_code": 0, "timed_out": False},
        {"executed": True, "ok": True, "exit_code": None, "timed_out": False},
        {"executed": True, "ok": True, "exit_code": 1, "timed_out": False},
        {"executed": True, "ok": True, "exit_code": 0, "timed_out": True},
    ],
)
def test_missing_failed_or_timed_out_check_never_passes(check):
    result = ExecutionResult(status="completed", executor="eval", model="test", summary="ok")
    assert "Independent check missing, timed out or failed" in harness.coding_verdict(
        result, check, {"complete": True}, [], []
    )


def test_checker_timeout_is_failed_and_uses_exact_isolated_argv(cases, tmp_path, monkeypatch):
    case = next(case for case in cases if case.id == "code-01-stable-unique")
    repo = tmp_path / "repo"
    harness.materialize(case, repo, git=False)
    called = []

    def timeout(argv, **kwargs):
        called.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(harness.subprocess, "run", timeout)
    result = harness.check_candidate(case, repo, tmp_path / "checker")
    argv, kwargs = called[0]
    assert argv[1:3] == ["-B", "-I"]
    assert Path(argv[3]).read_text() == case.assertion.source
    assert Path(argv[3]).parent != repo
    assert argv[4] == str(repo.resolve())
    assert "PYTHONOPTIMIZE" not in kwargs["env"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert kwargs["timeout"] == 5
    assert result["ok"] is False and result["timed_out"] is True


@pytest.mark.parametrize("extra", [None, "extra.py", "__pycache__/executor.pyc"])
def test_valid_checker_creates_no_files_and_preserves_executor_file_violations(
    cases, tmp_path, extra
):
    case = next(case for case in cases if case.id == "code-01-stable-unique")
    repo = tmp_path / "repo"
    harness.materialize(case, repo, git=False)
    (repo / "solution.py").write_text(
        "def stable_unique(values):\n    return list(dict.fromkeys(values))\n"
    )
    if extra is not None:
        path = repo / extra
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"executor-created extra file")
    before = {
        path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()
    }
    check = harness.check_candidate(case, repo, tmp_path / "withheld")
    after = {
        path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()
    }
    assert check["executed"] is True and check["ok"] is True and check["exit_code"] == 0
    assert after == before
    violations = harness.fixture_violations(case, repo)
    assert violations == ([] if extra is None else [f"Unexpected file: {extra}"])
    result = ExecutionResult(status="completed", executor="eval", model="test", summary="ok")
    assert harness.coding_verdict(result, check, {"complete": True}, violations, []) == violations


@pytest.mark.parametrize("dimension", ["cost", "input_tokens", "output_tokens"])
async def test_live_semantic_missing_usage_fails_without_paid_calls(
    cases, tmp_path, monkeypatch, dimension
):
    case = next(case for case in cases if case.id == "sem-01-negation")
    index = 0

    def respond(request):
        nonlocal index
        response = copy.deepcopy(case.recorded_steps[index].response)
        index += 1
        response["usage"] = {
            "cost": 0.001,
            "input_tokens": 10,
            "output_tokens": 1,
            "is_byok": False,
        }
        response["usage"].pop(dimension)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        harness.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond)
    )
    row = await harness.select_case(case, harness.offline_settings(tmp_path), tmp_path, live=True)
    assert row["ok"] is False
    assert row["required_facts_missing"] == []
    assert "Incomplete usage or unknown BYOK provider bill" in row["reasons"]
    report = harness.report_document("live-semantic", [row])
    assert report["accounting"]["full_cost_usd"] is None


def accounting_fixture():
    def task(task_id, cost, *, parent=None, escalation=None, review=False):
        return {
            "task_id": task_id,
            "parent_task_id": parent,
            "escalated_from": escalation,
            "status": "completed",
            "started_at": "2026-11-18T00:00:00+00:00",
            "duration_ms": 100,
            "request_json": json.dumps({"kind": "review" if review else "task"}),
            "result_json": json.dumps(
                {
                    "usage": {"cost_usd": cost, "input_tokens": 10, "output_tokens": 2},
                    "meta": {
                        "executor_started": True,
                        "fix_rounds": [
                            {
                                "round": 1,
                                "trigger": "verification_failed",
                                "stage": None,
                                "verdict": "pass",
                                "usage": {"cost_usd": 0.2, "input_tokens": 3, "output_tokens": 1},
                                "duration_s": 0.03,
                            }
                        ],
                        "review": {"task_id": "review", "cost_usd": 0.1},
                        "context_compression": {"attempted_steps": 1, "usage": {"cost": 0.02}},
                    }
                    if task_id == "root"
                    else {"executor_started": True},
                }
            ),
        }

    step = {
        "task_id": "root",
        "step_index": 0,
        "status": "succeeded",
        "cost_usd": 0.02,
        "input_tokens": 20,
        "output_tokens": 1,
        "latency_ms": 3,
        "is_byok": 0,
    }
    return {
        "tasks": [
            task("root", 0.8),
            task("review", 0.1, parent="root", review=True),
            task("escalation", 0.3, parent="root", escalation="root"),
        ],
        "compression_steps": [step, step.copy()],
        "routing_decisions": [
            {"id": 1, "task_id": "root", "router": "override", "latency_ms": 0, "cost_usd": None}
        ],
    }


@pytest.mark.parametrize("repaired", [False, True])
def test_cost_union_does_not_recount_fix_round_review_or_compression_mirrors(repaired):
    snapshot = accounting_fixture()
    if not repaired:
        result = json.loads(snapshot["tasks"][0]["result_json"])
        result["meta"].pop("fix_rounds")
        snapshot["tasks"][0]["result_json"] = json.dumps(result)
    snapshot["tasks"].append(snapshot["tasks"][1].copy())
    result = harness.account_chain(snapshot)
    assert result["cost_usd"]["known_total"] == pytest.approx(1.22)
    assert result["input_tokens"]["known_total"] == 50
    if repaired:
        assert result["complete"] is False and result["full_cost_usd"] is None
        assert result["cost_usd"]["total"] is None
        assert result["input_tokens"]["total"] is None
        assert result["output_tokens"]["total"] is None
        assert "Initial attempt usage unavailable for repaired task root" in result["unknowns"]
    else:
        assert result["complete"] is True
        assert result["full_cost_usd"] == pytest.approx(1.22)
        assert result["input_tokens"]["total"] == 50
    assert result["reviews"] == 1 and result["escalations"] == 1
    assert result["compression_latency_ms"]["total"] == 3
    assert result["inclusive_task_latency_ms"]["total"] == 300
    assert result["review_inclusive_latency_ms"]["total"] == 100


@pytest.mark.parametrize(
    ("missing", "known_cost", "known_input", "known_output"),
    [
        (None, 1.02, 50, 7),
        ("cost_usd", 1.02, 53, 8),
        ("input_tokens", 1.22, 50, 8),
        ("output_tokens", 1.22, 53, 7),
    ],
)
def test_real_fix_round_missing_usage_cannot_make_merged_total_complete(
    missing, known_cost, known_input, known_output
):
    snapshot = accounting_fixture()
    result = json.loads(snapshot["tasks"][0]["result_json"])
    initial = Usage(cost_usd=0.6, input_tokens=10, output_tokens=2)
    repair = Usage(cost_usd=0.2, input_tokens=3, output_tokens=1) if missing else None
    if repair is not None:
        setattr(repair, missing, None)
    # Same merge and metadata shape as Dispatcher._verify_and_review.
    result["usage"] = report_recovery._usage(initial, repair).model_dump()
    result["meta"]["fix_rounds"][0]["usage"] = repair.model_dump() if repair else None
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(known_cost)
    assert accounted["input_tokens"]["known_total"] == known_input
    assert accounted["output_tokens"]["known_total"] == known_output
    assert accounted["complete"] is False and accounted["full_cost_usd"] is None
    assert any("Unobserved fix-round usage root:0" == reason for reason in accounted["unknowns"])
    dimension = "cost_usd" if missing in (None, "cost_usd") else missing
    assert accounted[dimension]["total"] is None
    assert accounted["fix_rounds"][0]["usage"] == (repair.model_dump() if repair else None)


@pytest.mark.parametrize(
    ("missing", "known_cost", "known_input", "known_output"),
    [
        (None, 0.62, 43, 6),
        ("cost_usd", 0.62, 53, 8),
        ("input_tokens", 1.22, 43, 8),
        ("output_tokens", 1.22, 53, 6),
    ],
)
def test_complete_repair_cannot_prove_initial_attempt_usage(
    missing, known_cost, known_input, known_output
):
    snapshot = accounting_fixture()
    result = json.loads(snapshot["tasks"][0]["result_json"])
    initial = Usage(cost_usd=0.6, input_tokens=10, output_tokens=2) if missing else None
    if initial is not None:
        setattr(initial, missing, None)
    repair = Usage(cost_usd=0.2, input_tokens=3, output_tokens=1)
    # Inverse of missing repair: _usage keeps the complete repair even when the
    # initial attempt is unknown. Production stores no independent initial usage.
    result["usage"] = report_recovery._usage(initial, repair).model_dump()
    result["meta"]["fix_rounds"][0]["usage"] = repair.model_dump()
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(known_cost)
    assert accounted["input_tokens"]["known_total"] == known_input
    assert accounted["output_tokens"]["known_total"] == known_output
    assert accounted["complete"] is False and accounted["full_cost_usd"] is None
    assert accounted["cost_usd"]["total"] is None
    assert accounted["input_tokens"]["total"] is None
    assert accounted["output_tokens"]["total"] is None
    assert "Initial attempt usage unavailable for repaired task root" in accounted["unknowns"]
    assert accounted["fix_rounds"][0]["usage"] == repair.model_dump()


@pytest.mark.parametrize("location", ["task", "fix_round"])
@pytest.mark.parametrize(
    "recovery_usage",
    [None, Usage(cost_usd=0.1, input_tokens=1, output_tokens=1)],
)
def test_report_recovery_missing_attempt_usage_invalidates_total(location, recovery_usage):
    snapshot = accounting_fixture()
    original = ExecutionResult(
        status="partial",
        executor="eval",
        model="offline",
        summary="initial",
        usage=Usage(cost_usd=0.6, input_tokens=10, output_tokens=2),
    )
    recovered = report_recovery.merge_report(
        original,
        ExecutionResult(
            status="completed",
            executor="eval",
            model="offline",
            summary="recovered",
            usage=recovery_usage,
        ),
    )
    result = json.loads(snapshot["tasks"][0]["result_json"])
    if location == "task":
        result["meta"].pop("fix_rounds")
        result["meta"].update(recovered.meta)
    else:
        result["meta"]["fix_rounds"][0].update(
            usage=recovered.usage.model_dump(), report_recovery=recovered.meta["report_recovery"]
        )
    result["usage"] = recovered.usage.model_dump()
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(
        1.02 if recovery_usage is None else 1.12
    )
    assert accounted["cost_usd"]["total"] is None
    assert accounted["input_tokens"]["total"] is None
    assert accounted["output_tokens"]["total"] is None
    assert accounted["complete"] is False and accounted["full_cost_usd"] is None
    assert any("usage unavailable" in reason for reason in accounted["unknowns"])


@pytest.mark.parametrize(
    ("started", "finished", "cancelled"),
    [(1, 0, True), (2, 1, True), (2, 1, False), (1, 1, True), (2, 2, False), (None, 1, True)],
)
def test_supervision_completeness_preserves_merged_known_charge(started, finished, cancelled):
    snapshot = accounting_fixture()
    result = json.loads(snapshot["tasks"][0]["result_json"])
    result["meta"].pop("fix_rounds")
    initial = Usage(cost_usd=0.6, input_tokens=10, output_tokens=2)
    supervisor = None
    for _ in range(finished):
        supervisor = report_recovery._usage(
            supervisor, Usage(cost_usd=0.1, input_tokens=3, output_tokens=1)
        )
    # supervision.finish merges only finished checks, then cancels the in-flight
    # check without observing its bill. _mark_cancelled has no started counter.
    result["usage"] = report_recovery._usage(initial, supervisor).model_dump()
    metadata = {
        "checks_finished": finished,
        "usage": supervisor.model_dump() if supervisor else None,
    }
    if started is None:
        metadata["inflight_check_usage"] = "unknown after cancellation"
    else:
        metadata.update(checks_started=started, inflight_check_cancelled=cancelled)
    result["meta"]["supervision_usage"] = metadata
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    known_cost = 1.02 + 0.1 * finished
    assert accounted["cost_usd"]["known_total"] == pytest.approx(known_cost)
    assert accounted["input_tokens"]["known_total"] == 50 + 3 * finished
    assert accounted["output_tokens"]["known_total"] == 7 + finished
    if cancelled or started != finished or (started is not None and started > 1):
        assert accounted["complete"] is False and accounted["full_cost_usd"] is None
        assert accounted["cost_usd"]["total"] is None
        assert accounted["input_tokens"]["total"] is None
        assert accounted["output_tokens"]["total"] is None
        assert "Unobserved supervision usage root" in accounted["unknowns"]
    else:
        assert accounted["complete"] is True
        assert accounted["full_cost_usd"] == pytest.approx(known_cost)
        assert accounted["unknowns"] == []


@pytest.mark.parametrize("missing", [None, "cost_usd", "input_tokens", "output_tokens"])
def test_finished_supervision_missing_usage_cannot_make_merged_total_complete(missing):
    snapshot = accounting_fixture()
    result = json.loads(snapshot["tasks"][0]["result_json"])
    result["meta"].pop("fix_rounds")
    initial = Usage(cost_usd=0.6, input_tokens=10, output_tokens=2)
    supervisor = Usage(cost_usd=0.1, input_tokens=3, output_tokens=1) if missing else None
    if supervisor is not None:
        setattr(supervisor, missing, None)
    result["usage"] = report_recovery._usage(initial, supervisor).model_dump()
    result["meta"]["supervision_usage"] = {
        "checks_started": 1,
        "checks_finished": 1,
        "usage": supervisor.model_dump() if supervisor else None,
        "inflight_check_cancelled": False,
    }
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(
        1.02 if missing in (None, "cost_usd") else 1.12
    )
    assert accounted["input_tokens"]["known_total"] == (
        50 if missing in (None, "input_tokens") else 53
    )
    assert accounted["output_tokens"]["known_total"] == (
        7 if missing in (None, "output_tokens") else 8
    )
    assert accounted["complete"] is False and accounted["full_cost_usd"] is None
    dimension = "cost_usd" if missing is None else missing
    assert accounted[dimension]["total"] is None
    assert "Unobserved supervision usage root" in accounted["unknowns"]


@pytest.mark.parametrize("first", [None, Usage(cost_usd=0.1, input_tokens=4, output_tokens=2)])
def test_multiple_supervision_checks_have_unknown_per_attempt_completeness(first):
    snapshot = accounting_fixture()
    result = json.loads(snapshot["tasks"][0]["result_json"])
    result["meta"].pop("fix_rounds")
    second = Usage(cost_usd=0.2, input_tokens=3, output_tokens=1)
    aggregate = report_recovery._usage(first, second)
    result["usage"] = report_recovery._usage(
        Usage(cost_usd=0.6, input_tokens=10, output_tokens=2), aggregate
    ).model_dump()
    result["meta"]["supervision_usage"] = {
        "checks_started": 2,
        "checks_finished": 2,
        "usage": aggregate.model_dump(),
        "inflight_check_cancelled": False,
    }
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(1.32 if first else 1.22)
    assert accounted["complete"] is False
    assert accounted["full_cost_usd"] is None
    assert accounted["cost_usd"]["total"] is None


@pytest.mark.parametrize("missing", ["executor_cost", "compression_cost", "byok", "verification"])
def test_unknown_cost_bill_or_missing_verification_cannot_be_success(missing):
    snapshot = accounting_fixture()
    # Isolate each missing dimension from the conservative repaired-run rule.
    result = json.loads(snapshot["tasks"][0]["result_json"])
    result["meta"].pop("fix_rounds")
    snapshot["tasks"][0]["result_json"] = json.dumps(result)
    assert harness.account_chain(snapshot)["complete"] is True
    if missing == "executor_cost":
        result = json.loads(snapshot["tasks"][0]["result_json"])
        result["usage"].pop("cost_usd")
        snapshot["tasks"][0]["result_json"] = json.dumps(result)
    elif missing == "compression_cost":
        for step in snapshot["compression_steps"]:
            step["cost_usd"] = None
    elif missing == "byok":
        for step in snapshot["compression_steps"]:
            step["is_byok"] = 1
    else:
        snapshot["tasks"][0]["request_json"] = json.dumps({"verify": ["check"]})
    accounted = harness.account_chain(snapshot)
    assert accounted["complete"] is False
    assert accounted["full_cost_usd"] is None
    assert accounted["cost_usd"]["known_total"] > 0
    result = ExecutionResult(status="completed", executor="eval", model="test", summary="ok")
    check = {"executed": True, "ok": True, "exit_code": 0, "timed_out": False}
    assert "Incomplete full-chain usage/cost/check accounting" in harness.coding_verdict(
        result, check, accounted, [], []
    )


@pytest.mark.parametrize(
    ("event_recovered", "meta_recovered", "recovery_cost", "complete"),
    [
        (True, True, None, False),
        (False, False, None, False),
        (False, None, 0.2, False),
        (None, True, None, False),
        (None, False, None, True),
    ],
)
async def test_persisted_verdict_recovery_attempts_make_merged_usage_incomplete(
    tmp_path, event_recovered, meta_recovered, recovery_cost, complete
):
    settings = harness.offline_settings(tmp_path)
    storage = Storage(settings.db_path)
    await storage.open()
    root = harness.ancestor_record(
        "root", harness.DispatchRequest(task="Synthetic coding task", cwd=str(tmp_path)), ran=True
    )
    root.status = TaskStatus.completed
    root.result = ExecutionResult(
        status="completed",
        executor="eval",
        model="offline",
        summary="initial",
        usage=Usage(cost_usd=0.6, input_tokens=10, output_tokens=2),
        meta={"executor_started": True},
    )
    review = harness.ancestor_record(
        "review",
        harness.DispatchRequest(task="Synthetic review", cwd=str(tmp_path), kind="review"),
        ran=True,
    )
    review.parent_task_id = root.task_id
    review.status = TaskStatus.completed
    review.result = root.result.model_copy(deep=True)
    initial = Usage(cost_usd=0.1, input_tokens=10, output_tokens=2)
    continuation = (
        Usage(cost_usd=recovery_cost, input_tokens=3, output_tokens=1)
        if recovery_cost is not None
        else None
    )
    # _recover_verdict retains the initial result and merges usage even if the
    # continuation fails to provide a verdict. Its event records that attempt.
    review.result.usage = report_recovery._usage(initial, continuation)
    if meta_recovered is not None:
        review.result.meta["verdict_recovered"] = meta_recovered
    if meta_recovered is True:
        review.result.verdict = "pass"
    unrelated = harness.ancestor_record(
        "unrelated", harness.DispatchRequest(task="Other lineage", cwd=str(tmp_path)), ran=True
    )
    try:
        for record in (root, review, unrelated):
            await storage.insert_task(record)
        await storage.add_event("unrelated", "verdict_recovery", {"recovered": False})
        if event_recovered is not None:
            await storage.add_event("review", "verdict_recovery", {"recovered": event_recovered})
        snapshot = await asyncio.to_thread(harness.chain_snapshot, settings.db_path, "root")
    finally:
        await storage.close()
    assert {task["task_id"] for task in snapshot["tasks"]} == {"root", "review"}
    assert [event["task_id"] for event in snapshot["events"]] == (
        ["review"] if event_recovered is not None else []
    )
    if event_recovered is not None:
        assert snapshot["events"][0]["kind"] == "verdict_recovery"
        assert json.loads(snapshot["events"][0]["payload_json"]) == {"recovered": event_recovered}
    accounted = harness.account_chain(snapshot)
    assert accounted["cost_usd"]["known_total"] == pytest.approx(
        0.7 if recovery_cost is None else 0.9
    )
    assert accounted["input_tokens"]["known_total"] == (20 if recovery_cost is None else 23)
    assert accounted["output_tokens"]["known_total"] == (4 if recovery_cost is None else 5)
    assert accounted["reviews"] == 1 and accounted["executions"] == 2
    assert accounted["complete"] is complete
    if complete:
        assert accounted["full_cost_usd"] == pytest.approx(0.7)
        assert accounted["unknowns"] == []
    else:
        assert accounted["full_cost_usd"] is None
        assert accounted["cost_usd"]["total"] is None
        assert accounted["input_tokens"]["total"] is None
        assert accounted["output_tokens"]["total"] is None
        assert (
            "Recovery attempt usage unavailable review: verdict_recovery" in accounted["unknowns"]
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(unrecognized=True),
        lambda row: row.update(schema_version=True),
        lambda row: row.update(split="holdout"),
        lambda row: row["tags"].append(row["tags"][0]),
        lambda row: row["request"].update(context_mode="prompt_summary"),
        lambda row: row["request"].update(cwd="/a/leaked/path"),
        lambda row: row["request"].update(files=["../secret"]),
        lambda row: row["required_facts"][0]["alternatives"][0].update(block_ids=["b9999"]),
        lambda row: row["required_facts"][0]["alternatives"][0].update(fragments=["not present"]),
        lambda row: row["recorded_steps"].pop(),
        lambda row: row.update(expected_kept_ids=["b0000", "b0000"]),
    ],
)
def test_strict_schema_rejects_malformed_cases(cases, mutation):
    value = case_data(cases, "sem-01-negation")
    mutation(value)
    with pytest.raises(ValueError):
        harness.validate_case(value)


@pytest.mark.parametrize("path", ["../x", "/x", "a/../x", "a//x", "./x", ".git/config", "C:\\x"])
def test_fixture_paths_cannot_escape_or_alias_materialization(cases, path):
    value = case_data(cases, "code-01-stable-unique")
    value["fixture"]["files"][path] = "x = 1"
    with pytest.raises(ValueError, match="Unsafe fixture path"):
        harness.validate_case(value)


def test_fixture_symlink_and_immutable_or_extra_files_fail(cases, tmp_path):
    case = next(case for case in cases if case.id == "code-01-stable-unique")
    repo = tmp_path / "repo"
    harness.materialize(case, repo, git=False)
    (repo / "solution.py").unlink()
    (repo / "solution.py").symlink_to(tmp_path / "outside.py")
    (repo / "unexpected.py").write_text("pass\n")
    violations = harness.fixture_violations(case, repo)
    assert "Symlink in fixture: solution.py" in violations
    assert "Unexpected file: unexpected.py" in violations
    with pytest.raises(ValueError, match="outside"):
        harness.check_candidate(case, repo, repo / "checker")


@pytest.mark.parametrize(
    "malformation", ["duplicate_key", "duplicate_id", "missing_row", "no_lf", "changed_probability"]
)
def test_corpus_malformed_is_rejected_before_replay(tmp_path, malformation):
    data = harness.CORPUS.read_text()
    lines = data.splitlines()
    if malformation == "duplicate_key":
        lines[0] = lines[0].replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    elif malformation == "duplicate_id":
        lines.append(lines[0])
    elif malformation == "missing_row":
        lines.pop()
    elif malformation == "changed_probability":
        lines[21] = lines[21].replace('"noul":0.5999999999999999', '"noul":0.59')
    target = tmp_path / "cases.jsonl"
    target.write_text("\n".join(lines) + ("" if malformation == "no_lf" else "\n"))
    match = "frozen revision 3 SHA-256" if malformation == "changed_probability" else None
    with pytest.raises(ValueError, match=match):
        harness.load_corpus(target)


def test_cli_default_free_and_paid_lanes_are_separate():
    cli = parser()
    args = cli.parse_args([])
    assert args.live is False and args.paired is False and args.executor is None
    assert cli.parse_args(["--live"]).live is True
    assert cli.parse_args(["--live-semantic"]).live is True
    with pytest.raises(SystemExit):
        cli.parse_args(["--live", "--paired"])


def test_paired_schedule_balance_isolation_and_baseline_absent_opt_in(cases, tmp_path):
    coding = [case for case in cases if isinstance(case, harness.CodingCase)]
    schedule = harness.paired_schedule(coding)
    assert len(schedule) == 120
    first_arms = [schedule[index][2] for index in range(0, 120, 2)]
    assert first_arms.count("baseline") == first_arms.count("treatment") == 30
    assert schedule[:6] == [
        (coding[0], 0, "baseline"),
        (coding[0], 0, "treatment"),
        (coding[0], 1, "treatment"),
        (coding[0], 1, "baseline"),
        (coding[0], 2, "baseline"),
        (coding[0], 2, "treatment"),
    ]
    seen = set()
    for index, (case, _, arm) in enumerate(schedule):
        repo = tmp_path / f"run-{index}"
        harness.materialize(case, repo, git=False)
        req = harness.paired_request(case, repo, "explicit-omp", arm, 60)
        wire = req.model_dump(mode="json")
        assert req.executor == "explicit-omp" and req.allow_escalation is False
        assert req.timeout_seconds == 60
        if arm == "baseline":
            assert "context_compression" not in wire
        else:
            assert wire["context_compression"] == "jev"
        assert req.cwd not in seen
        seen.add(req.cwd)
        assert (repo / "solution.py").read_text() == case.fixture.files["solution.py"]


def test_paired_gate_keeps_failed_attempts_in_byte_median_and_denominator(cases):
    coding = [case for case in cases if isinstance(case, harness.CodingCase)]
    rows = []
    for case, repetition, arm in harness.paired_schedule(coding):
        rows.append(
            {
                "id": case.id,
                "split": case.split,
                "repetition": repetition,
                "arm": arm,
                "ok": True,
                "check": {"ok": True},
                "accounting": {"complete": True, "full_cost_usd": 0.01},
                "context_bytes": 100 if arm == "baseline" else 50,
                "wall_ms": 1000,
                "missing_facts": [],
            }
        )
    assert harness.paired_gate(rows)["ok"] is True
    for row in rows:
        if row["arm"] == "treatment":
            row.update(ok=False, context_bytes=100, accounting={"complete": False})
            row["check"] = {"ok": False}
    gate = harness.paired_gate(rows)
    assert gate["ok"] is False
    assert gate["arms"]["treatment"]["attempted"] == 60
    assert gate["arms"]["treatment"]["median_context_bytes"] == 100
    assert gate["arms"]["treatment"]["full_cost_usd"]["total"] is None


async def test_paired_harness_checks_real_files_despite_fake_success_report(
    cases, tmp_path, monkeypatch
):
    settings = harness.offline_settings(tmp_path)
    storage = Storage(settings.db_path)
    await storage.open()
    case = next(case for case in cases if case.id == "code-01-stable-unique")
    schedule = [(case, 0, "baseline"), (case, 0, "treatment")]
    requests = []
    records = {}

    class FakeDaemon:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def health(self):
            return {"pid": 123, "router_backend": "static"}

        async def executors(self):
            return [{"name": "eval", "adapter": "omp", "model": "offline", "enabled": True}]

        async def submit(self, req):
            repo = Path(req.cwd)
            original = await asyncio.to_thread((repo / "solution.py").read_text)
            assert original == case.fixture.files["solution.py"]
            requests.append(req.model_dump(mode="json"))
            record = harness.ancestor_record(f"task-{len(requests)}", req, ran=True)
            await storage.insert_task(record)
            metrics = None
            if req.context_compression is not None:
                index = 0

                def respond(request):
                    nonlocal index
                    response = copy.deepcopy(case.recorded_steps[index].response)
                    index += 1
                    response["usage"] = {
                        "cost": 0.001,
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "is_byok": False,
                    }
                    return httpx.Response(200, json=response)

                async def before(step, acknowledge):
                    await storage.start_compression_step(
                        record.task_id, step, acknowledge=acknowledge
                    )

                async def after(step):
                    await storage.finish_compression_step(record.task_id, step)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    selected = await compress_context(
                        CompressionInput(
                            record.task_id, req, split_context(req.context), None, None
                        ),
                        settings=settings,
                        client=client,
                        before_step=before,
                        after_step=after,
                    )
                metrics = selected.metrics
            source = (
                harness.REFERENCES[1]
                if req.context_compression is not None
                else "def stable_unique(values):\n    return []\n"
            )
            await asyncio.to_thread((repo / "solution.py").write_text, source)
            record.status = TaskStatus.completed
            record.result = ExecutionResult(
                status="completed",
                executor="eval",
                model="offline",
                summary="All tests passed",
                tests=TestsInfo(command="claimed test", result="passed"),
                usage=Usage(cost_usd=0.01, input_tokens=10, output_tokens=2),
                meta={
                    "executor_started": True,
                    **({"context_compression": metrics} if metrics is not None else {}),
                },
            )
            await storage.update_task(record)
            records[record.task_id] = record
            return record

        async def status(self, task_id, wait=0):
            return records[task_id]

    async def daemon(settings):
        return object()

    monkeypatch.setattr(harness, "ensure_daemon", daemon)
    monkeypatch.setattr(harness, "DispatchClient", lambda *args, **kwargs: FakeDaemon())
    monkeypatch.setattr(harness, "load_settings", lambda: settings)
    monkeypatch.setattr(harness, "paired_schedule", lambda cases: schedule)
    root = tmp_path / "runs"
    await asyncio.to_thread(root.mkdir)
    rows = []
    checkpoints = []
    try:
        await harness.run_paired(
            cases,
            settings,
            "eval",
            60,
            root,
            rows=rows,
            checkpoint=lambda: checkpoints.append(len(rows)),
        )
    finally:
        await storage.close()
    assert len(rows) == 2
    assert checkpoints == [1, 2]
    baseline, treatment = rows
    assert baseline["ok"] is False
    assert baseline["check"]["exit_code"] != 0
    assert baseline["accounting"]["complete"] is True
    assert baseline["reasons"] == ["Independent check missing, timed out or failed"]
    assert treatment["ok"] is True
    assert treatment["check"]["exit_code"] == 0
    assert treatment["accounting"]["full_cost_usd"] == pytest.approx(0.012)
    assert treatment["context_bytes"] < baseline["context_bytes"]
    assert baseline["repo"] != treatment["repo"]
    assert "context_compression" not in requests[0]
    assert requests[1]["context_compression"] == "jev"
    assert all(
        "assertion" not in request and "recorded_steps" not in request for request in requests
    )


async def test_live_refusal_records_attempt_before_stopping_without_retry(
    cases, tmp_path, monkeypatch
):
    settings = harness.offline_settings(tmp_path)
    rows = []
    checkpoints = []
    calls = []

    def refused(request):
        calls.append(request)
        return httpx.Response(503, text="synthetic refusal")

    monkeypatch.setattr(harness, "load_settings", lambda: settings)
    monkeypatch.setattr(harness, "TRUSTED_ROOT", tmp_path)
    monkeypatch.setattr(
        harness.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(refused)
    )
    with pytest.raises(RuntimeError, match="without retry or fallback"):
        await harness.run_semantic(
            cases,
            settings,
            rows=rows,
            checkpoint=lambda: checkpoints.append(len(rows)),
        )
    assert len(calls) == 1
    assert checkpoints == [1]
    assert rows[0]["id"] == "sem-01-negation"
    assert rows[0]["ok"] is False
    assert rows[0]["metrics"]["error_code"] == "compression_http"
    assert rows[0]["metrics"]["usage"]["cost"] is None
    assert harness.report_document("live-semantic", rows)["ok"] is False


@pytest.mark.parametrize(
    ("failure", "cancel_confirmed"),
    [("connection", False), ("timeout", False), ("auth", False), ("connection", True)],
)
async def test_real_client_unavailable_after_submit_checkpoints_task_and_stops(
    cases, tmp_path, monkeypatch, failure, cancel_confirmed
):
    settings = harness.offline_settings(tmp_path)
    state = ServeState(pid=123, port=12345, token="synthetic", started_at=datetime.now(UTC))
    storage = Storage(settings.db_path)
    await storage.open()
    requests = []
    record = None

    def unavailable(request):
        if failure == "connection":
            raise httpx.ConnectError("synthetic connection failure", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(401)

    async def respond(request):
        nonlocal record
        requests.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"pid": 123})
        if request.url.path == "/executors":
            return httpx.Response(
                200, json=[{"name": "eval", "adapter": "omp", "model": "offline", "enabled": True}]
            )
        if request.method == "POST" and request.url.path == "/tasks":
            req = harness.DispatchRequest.model_validate_json(request.content)
            record = harness.ancestor_record("paid-running-task", req, ran=True)
            record.status = TaskStatus.running
            record.finished_at = None
            record.result.usage = Usage(cost_usd=0.01, input_tokens=10, output_tokens=2)
            await storage.insert_task(record)
            return httpx.Response(200, json={**record.model_dump(mode="json"), "log_tail": ""})
        if request.method == "DELETE" and cancel_confirmed:
            record.status = TaskStatus.cancelled
            record.finished_at = datetime.now(UTC)
            await storage.update_task(record)
            return httpx.Response(200, json={**record.model_dump(mode="json"), "log_tail": ""})
        return unavailable(request)

    real_client = httpx.AsyncClient

    def mocked_transport(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    async def daemon(settings):
        return state

    monkeypatch.setattr(harness, "ensure_daemon", daemon)
    monkeypatch.setattr(harness, "load_settings", lambda: settings)
    monkeypatch.setattr(httpx, "AsyncClient", mocked_transport)
    root = tmp_path / "runs"
    root.mkdir()
    rows = []
    with tempfile.TemporaryDirectory(prefix="compression-failure-", dir="/tmp") as directory:
        report_path = Path(directory) / "report.json"

        def checkpoint():
            harness.write_report(report_path, harness.report_document("paired", rows))

        try:
            with pytest.raises(RuntimeError, match="stopping without retry or fallback"):
                await harness.run_paired(
                    cases, settings, "eval", 60, root, rows=rows, checkpoint=checkpoint
                )
            report = json.loads(report_path.read_text())
        finally:
            await storage.close()
    assert requests.count(("POST", "/tasks")) == 1
    assert requests.count(("GET", "/tasks/paid-running-task")) == 1
    assert requests.count(("DELETE", "/tasks/paid-running-task")) == 1
    assert len(rows) == 1 and report["rows"] == rows
    row = rows[0]
    assert row["task_id"] == "paid-running-task" and row["ok"] is False
    assert "Dispatch failed: DaemonUnavailable" in row["reasons"]
    assert row["termination_confirmed"] is cancel_confirmed
    assert row["last_task_status"] == ("cancelled" if cancel_confirmed else "running")
    assert row["cancellation_attempted"] is True
    assert row["cancellation_error"] == (None if cancel_confirmed else "DaemonUnavailable")
    assert row["check"]["executed"] is cancel_confirmed
    assert row["accounting"]["cost_usd"]["known_total"] == pytest.approx(0.01)
    if not cancel_confirmed:
        assert row["accounting"]["complete"] is False
        assert row["accounting"]["full_cost_usd"] is None
        assert "Unfinished task paid-running-task" in row["accounting"]["unknowns"]
    assert report["ok"] is False and report["full_gate_passed"] is False
    assert await asyncio.to_thread(Path(row["repo"]).is_dir)


async def test_cli_daemon_unavailable_still_writes_failure_report(monkeypatch):
    def unavailable():
        raise DaemonUnavailable("synthetic authentication failure")

    monkeypatch.setattr(cli, "live_settings", unavailable)
    with tempfile.TemporaryDirectory(prefix="compression-cli-", dir="/tmp") as directory:
        report_path = Path(directory) / "report.json"
        args = parser().parse_args(["--paired", "--executor", "eval", "--report", str(report_path)])
        assert await cli.run(args) == 1
        report = json.loads(report_path.read_text())
    assert report["rows"] == [] and report["ok"] is False
    assert report["full_gate_passed"] is False
    assert "DaemonUnavailable" in report["error"]


@pytest.mark.parametrize("selected", [None, "calibration", "holdout", "all"])
async def test_live_cli_split_calls_only_selected_semantic_cases(
    cases, tmp_path, monkeypatch, selected
):
    settings = harness.offline_settings(tmp_path)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content)["state"])
        return httpx.Response(
            200,
            json={
                "answers": {"delete_safe": {"type": "noul", "noul": 0.1}},
                "usage": {"cost": 0.001, "input_tokens": 10, "output_tokens": 1, "is_byok": False},
            },
        )

    monkeypatch.setattr(cli, "live_settings", lambda: settings)
    monkeypatch.setattr(harness, "load_settings", lambda: settings)
    monkeypatch.setattr(harness, "TRUSTED_ROOT", tmp_path)
    monkeypatch.setattr(
        harness.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond)
    )
    split = selected or "all"
    expected = [
        case
        for case in cases
        if case.kind == "semantic" and (split == "all" or case.split == split)
    ]
    with tempfile.TemporaryDirectory(prefix="compression-split-", dir="/tmp") as directory:
        report_path = Path(directory) / "report.json"
        argv = ["--live-semantic", "--report", str(report_path)]
        if selected is not None:
            argv.extend(["--split", selected])
        assert await cli.run(parser().parse_args(argv)) == 0
        report = json.loads(report_path.read_text())
    assert [row["id"] for row in report["rows"]] == [case.id for case in expected]
    assert len(calls) == sum(len(split_context(case.request.context)) for case in expected)
    assert {state["task"] for state in calls} == {case.request.task for case in expected}
    assert report["split"] == split and report["complete"] is True
    assert report["expected_rows"] == {"calibration": 10, "holdout": 20, "all": 30}[split]
    assert report["full_gate_expected_rows"] == 30
    assert report["full_gate_passed"] is (split == "all")
    assert report["splits"]["calibration"]["expected"] == (0 if split == "holdout" else 10)
    assert report["splits"]["holdout"]["expected"] == (0 if split == "calibration" else 20)
    if split != "all":
        assert "not the full gate" in report["scope"]
        assert harness.report_document("live-semantic", report["rows"])["ok"] is False
        incomplete = harness.report_document("live-semantic", report["rows"][:-1], split=split)
        assert incomplete["ok"] is False


@pytest.mark.parametrize("lane", [["--offline"], ["--paired", "--executor", "eval"]])
@pytest.mark.parametrize("split", ["calibration", "all"])
def test_split_flag_rejected_outside_live_semantic(monkeypatch, lane, split):
    monkeypatch.setattr("sys.argv", ["compression", *lane, "--split", split])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
