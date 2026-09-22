from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_dispatch.models import DispatchRequest


@dataclass(frozen=True)
class Summary:
    accuracy: float
    confusion: dict[str, dict[str, int]]
    cost: float
    latency_p50: float


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    required = {"id", "task", "context", "files", "constraints", "expected_executor", "rationale"}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        missing = required - case.keys()
        if missing:
            raise ValueError(f"case {case.get('id', '?')} missing fields: {sorted(missing)}")
        cases.append(case)
    return cases


def build_request(case: dict[str, Any], source_agent: str) -> DispatchRequest:
    return DispatchRequest(
        task=case["task"],
        cwd=case.get("cwd", str(Path.cwd())),
        context=case.get("context"),
        files=case.get("files", []),
        constraints=case.get("constraints", []),
        source_agent=source_agent,
    )


def score(results: list[dict[str, Any]]) -> Summary:
    if not results:
        return Summary(0.0, {}, 0.0, 0.0)
    confusion: dict[str, dict[str, int]] = {}
    correct = 0
    costs: list[float] = []
    latencies: list[float] = []
    for result in results:
        expected, got = result["expected"], result["got"]
        row = confusion.setdefault(expected, {})
        row[got] = row.get(got, 0) + 1
        correct += expected == got
        costs.append(float(result.get("cost_usd") or 0.0))
        latencies.append(float(result.get("latency_ms") or 0.0))
    return Summary(correct / len(results), confusion, sum(costs), statistics.median(latencies))


def render(summary: Summary) -> str:
    lines = [
        f"accuracy: {summary.accuracy:.1%}",
        f"cost_usd: {summary.cost:.4f}",
        f"latency_p50_ms: {summary.latency_p50:.1f}",
        "confusion (expected -> got):",
    ]
    for expected, row in sorted(summary.confusion.items()):
        lines.append(
            f"  {expected}: " + ", ".join(f"{got}={count}" for got, count in sorted(row.items()))
        )
    return "\n".join(lines)
