"""Offline checks for the prompt contracts emitted by AgentDispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_dispatch.config import ExecutorSettings, RoutingSettings, Settings
from agent_dispatch.dispatch.task_package import build_task_package, render_prompt
from agent_dispatch.models import DispatchRequest, SourceAgent

TASK_EXECUTOR_OWNERSHIP = (
    "You own the implementation, the regression tests and deterministic evals for the "
    "affected scope, and nothing else. Run only what your change touches: no repeats of green "
    "unchanged checks, no full-repo suite for unrelated scope, no paid evals (caller-owned)."
)
TASK_CALLER_OWNERSHIP = (
    "The caller owns independent review, the final combined gate and the commit."
)
REVIEW_EXECUTOR_OWNERSHIP = "Findings only: you observe and report,"
REVIEW_CALLER_OWNERSHIP = "the caller fixes, reruns what is needed and commits."
REVIEW_UNCHANGED_CHECKS = "Do not rerun green checks unchanged by the diff you see."
TASK_RULES = (
    "Do not commit or push: leave all changes in the working tree, the caller reviews and commits."
)
REVIEW_RULES = "Do not edit files, commit or push."
REVIEW_ONLY = (
    "This is a review request. Read the code and report findings. Do NOT create, modify or "
    "delete any file: your tools are read-only, and the caller fixes what you find. Put the "
    "findings in `summary`, most severe first, each with `path:line`, what is wrong and the "
    "concrete failure it causes. Leave `changed_files` empty. If you find nothing worth fixing, "
    "say so plainly."
)


@dataclass(frozen=True)
class Verdict:
    case_id: str
    kind: str
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _settings() -> Settings:
    return Settings(
        executors={"codex": ExecutorSettings(adapter="codex")},
        routing=RoutingSettings(fallback_executor="codex", max_hops=2, max_children=2),
    )


def _section(prompt: str, heading: str) -> str | None:
    marker = f"# {heading}\n"
    if prompt.count(marker) != 1:
        return None
    return prompt.split(marker, 1)[1].split("\n#", 1)[0].strip()


def _request(case: dict[str, Any], repo: Path) -> DispatchRequest:
    return DispatchRequest(
        task=f"Workflow contract case: {case['id']}",
        cwd=str(repo),
        kind=case["kind"],
        context="A caller supplied this context.",
        files=["README.md"],
        constraints=["Keep the public contract stable."],
        success_criteria=["The focused check passes."],
        context_mode=case["context_mode"],
        source_agent=SourceAgent.codex,
        hop=case["hop"],
    )


def evaluate(cases: list[dict[str, Any]], repo: Path) -> list[Verdict]:
    settings = _settings()
    verdicts: list[Verdict] = []
    for case in cases:
        package = build_task_package(_request(case, repo), settings)
        prompt = render_prompt(package, case["adapter"], settings)
        failures = _check_prompt(case, prompt)
        verdicts.append(Verdict(case["id"], case["kind"], tuple(failures)))
    return verdicts


def _check_prompt(case: dict[str, Any], prompt: str) -> list[str]:
    failures: list[str] = []
    ownership = _section(prompt, "Ownership")
    rules = _section(prompt, "Rules")

    if case["kind"] == "task":
        if ownership is None or TASK_EXECUTOR_OWNERSHIP not in ownership:
            failures.append("executor ownership")
        if ownership is None or TASK_CALLER_OWNERSHIP not in ownership:
            failures.append("caller ownership")
        if ownership != f"{TASK_EXECUTOR_OWNERSHIP} {TASK_CALLER_OWNERSHIP}":
            failures.append("task ownership format")
        if rules != TASK_RULES:
            failures.append("task no-commit/push rule")
    else:
        review = _section(prompt, "Review only")
        if review is None or review.split("\n## Working copy", 1)[0] != REVIEW_ONLY:
            failures.append("read-only review contract")
        if ownership is None or REVIEW_EXECUTOR_OWNERSHIP not in ownership:
            failures.append("reviewer ownership")
        if ownership is None or REVIEW_CALLER_OWNERSHIP not in ownership:
            failures.append("review caller ownership")
        expected = (
            f"{REVIEW_EXECUTOR_OWNERSHIP} {REVIEW_CALLER_OWNERSHIP} {REVIEW_UNCHANGED_CHECKS}"
        )
        if ownership != expected:
            failures.append("review ownership format")
        if rules != REVIEW_RULES:
            failures.append("review no-edit/commit/push rule")
        if "diff --git a/README.md b/README.md" not in prompt:
            failures.append("review package diff")

    _check_adapter(case, prompt, failures)
    _check_context(case, prompt, failures)
    _check_hop(case, prompt, failures)
    return failures


def _check_adapter(case: dict[str, Any], prompt: str, failures: list[str]) -> None:
    result_format = _section(prompt, "Result format") or ""
    if case["adapter"] == "codex":
        if "structured output schema" not in result_format:
            failures.append("codex result format")
    elif "```agent-dispatch-result" not in result_format:
        failures.append(f"{case['adapter']} result format")


def _check_context(case: dict[str, Any], prompt: str, failures: list[str]) -> None:
    mode = case["context_mode"]
    if mode == "prompt":
        if "# Context\n" in prompt or "# Repository state\n" in prompt:
            failures.append("prompt context mode")
        return
    if "# Context\nA caller supplied this context." not in prompt:
        failures.append(f"{mode} context mode")
    if mode == "full" and ("# Repository state\n" not in prompt or "README.md" not in prompt):
        failures.append("full repository state")


def _check_hop(case: dict[str, Any], prompt: str, failures: list[str]) -> None:
    if case["kind"] == "review":
        expected = "Do NOT delegate this review: answer it yourself."
    elif case["hop"] == 1:
        expected = "Do NOT delegate further: the hop limit is reached."
    else:
        expected = "You may split this task into at most 2 independent subtasks"
    if expected not in prompt:
        failures.append("delegation hop")


def render(verdicts: list[Verdict]) -> str:
    passed = sum(verdict.ok for verdict in verdicts)
    lines = [f"prompt contracts: {passed}/{len(verdicts)} passed"]
    for verdict in verdicts:
        detail = "ok" if verdict.ok else "; ".join(verdict.failures)
        lines.append(f"  [{verdict.kind}] {verdict.case_id}: {detail}")
    return "\n".join(lines)
