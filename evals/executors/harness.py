"""Replay OpenCode adapter argv and event outcomes without invoking the CLI."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.opencode import OpenCodeAdapter
from agent_dispatch.executors.process import ProcessOutcome

REQUIRED_FIELDS = {"id", "kind", "input", "expected"}


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
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode",
            command="opencode",
            model="m",
            extra_args=case["input"].get("extra_args", []),
        ),
    )
    ctx = RunContext(
        cwd=str(tmp),
        timeout_seconds=1,
        env={},
        log_path=tmp / "run.log",
        task_id="eval",
        prompt="task",
        read_only=case["input"].get("read_only", False),
    )
    if case["kind"] == "argv":
        argv = adapter.build_argv(ctx)
        expected_tail = case["expected"]["tail"]
        actual_tail = argv[-len(expected_tail) :]
        if actual_tail != expected_tail:
            return Row(case["id"], False, f"argv tail {actual_tail!r} != {expected_tail!r}")
        return Row(case["id"], True, "")

    stdout = "".join(
        json.dumps(event, ensure_ascii=False) + "\n" for event in case["input"]["events"]
    )
    outcome = ProcessOutcome(
        case["input"].get("exit_code", 0), stdout, "", False, case["input"].get("duration_ms", 1)
    )
    result = adapter._parse_result(outcome, case["input"].get("changed", []))
    expected = case["expected"]
    if result.status != expected.get("status"):
        return Row(case["id"], False, f"status {result.status!r} != {expected.get('status')!r}")
    if "error" in expected and result.error != expected["error"]:
        return Row(case["id"], False, f"error {result.error!r} != {expected['error']!r}")
    for key, value in expected.get("meta", {}).items():
        if result.meta.get(key) != value:
            return Row(case["id"], False, f"meta[{key!r}] {result.meta.get(key)!r} != {value!r}")
    if "usage" in expected:
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
    lines = [f"opencode adapter corpus: {report['passed']}/{report['cases']} passed"]
    lines += [f"  FAIL {row['id']}: {row['reason']}" for row in report["failures"]]
    return "\n".join(lines)
