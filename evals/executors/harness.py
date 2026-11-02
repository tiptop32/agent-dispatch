"""Replay omp adapter argv and event outcomes without invoking the CLI."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.omp import OmpAdapter
from agent_dispatch.executors.process import ProcessOutcome

REQUIRED_FIELDS = {"id", "kind", "input", "expected"}

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "omp"


def load_events_fixture(name: str) -> list[dict]:
    lines = (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@dataclass
class Row:
    id: str
    ok: bool
    reason: str


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        missing = REQUIRED_FIELDS - case.keys()
        if missing:
            raise ValueError(f"{case.get('id', '?')}: missing fields {sorted(missing)}")
        if case["kind"] not in ("argv", "outcome"):
            raise ValueError(f"{case['id']}: unknown kind {case['kind']!r}")
        cases.append(case)
    return cases


def run_case(case: dict, tmp: Path) -> Row:
    adapter = OmpAdapter(
        "omp",
        ExecutorSettings(
            adapter="omp",
            command="omp",
            model="m",
            extra_args=case["input"].get("extra_args", []),
            corporate=case["input"].get("corporate", False),
        ),
        data_dir=tmp,
    )
    ctx = RunContext(
        cwd=str(tmp),
        timeout_seconds=case["input"].get("timeout_seconds", 1),
        env={},
        log_path=tmp / "run.log",
        task_id="eval",
        prompt="task",
        read_only=case["input"].get("read_only", False),
        resume_session=case["input"].get("resume_session"),
    )
    if case["kind"] == "argv":
        argv = adapter.build_argv(ctx)
        # Полный argv, а не только хвост: флаг с аргументом мог бы спрятаться
        # до начала сравниваемого хвоста.
        for token in case["expected"].get("forbidden", []):
            if any(arg == token or arg.startswith(f"{token}=") for arg in argv):
                return Row(case["id"], False, f"argv contains forbidden {token!r}")

        def contains(sequence: list[str]) -> bool:
            size = len(sequence)
            return any(argv[i : i + size] == sequence for i in range(len(argv) - size + 1))

        for sequence in case["expected"].get("contains", []):
            if not contains(sequence):
                return Row(case["id"], False, f"argv misses {sequence!r}")
        expected_tail = case["expected"].get("tail")
        if expected_tail is not None:
            actual_tail = argv[-len(expected_tail) :]
            if actual_tail != expected_tail:
                return Row(case["id"], False, f"argv tail {actual_tail!r} != {expected_tail!r}")
        return Row(case["id"], True, "")

    input = case["input"]
    if "events_fixture" in input:
        events = load_events_fixture(input["events_fixture"])
    else:
        events = input["events"]
    stdout = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    outcome = ProcessOutcome(
        input.get("exit_code", 0), stdout, "", False, input.get("duration_ms", 1)
    )
    result = adapter._parse_result(outcome, input.get("changed", []))
    expected = case["expected"]
    if result.status != expected.get("status"):
        return Row(case["id"], False, f"status {result.status!r} != {expected.get('status')!r}")
    if "error" in expected and result.error != expected["error"]:
        return Row(case["id"], False, f"error {result.error!r} != {expected['error']!r}")
    for key, value in expected.get("meta", {}).items():
        if result.meta.get(key) != value:
            return Row(case["id"], False, f"meta[{key!r}] {result.meta.get(key)!r} != {value!r}")
    if "usage" in expected:
        if result.usage is None:
            return Row(case["id"], False, "usage is None")
        for key, value in expected["usage"].items():
            actual = getattr(result.usage, key)
            if actual != value:
                return Row(case["id"], False, f"usage.{key} {actual!r} != {value!r}")
    return Row(case["id"], True, "")


def replay(cases: list[dict], tmp: Path | None = None) -> dict:
    own_tmp = tmp is None
    if own_tmp:
        tmp = Path(tempfile.mkdtemp(prefix="agent-dispatch-evals-executors-"))
    try:
        rows = [run_case(case, tmp) for case in cases]
    finally:
        if own_tmp:
            tmp.rmdir()  # type: ignore[union-attr]
    passed = sum(row.ok for row in rows)
    return {
        "cases": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "failures": [row.__dict__ for row in rows if not row.ok],
        "pass": passed == len(rows),
    }


def render(report: dict) -> str:
    lines = [f"omp adapter corpus: {report['passed']}/{report['cases']} passed"]
    lines += [f"  FAIL {row['id']}: {row['reason']}" for row in report["failures"]]
    return "\n".join(lines)
