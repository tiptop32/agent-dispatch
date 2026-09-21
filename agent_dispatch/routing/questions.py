from __future__ import annotations

from typing import Any

from agent_dispatch.models import DispatchRequest, Judgment

from .base import RouterError


def build_state(req: DispatchRequest) -> dict[str, Any]:
    return {
        "task": req.task,
        "context": req.context,
        "files": req.files,
        "constraints": req.constraints,
        "source_agent": req.source_agent.value,
    }


def build_questions(candidates: dict[str, str]) -> dict[str, Any]:
    return {
        "executor": {
            "type": "choice",
            "instructions": "Which coding agent should execute this task?",
            "criteria": candidates,
        },
        "difficulty": {
            "type": "score",
            "instructions": "How hard is this coding task?",
            "criteria": [
                {"label": "trivial", "description": "one-line or config change"},
                {"label": "moderate", "description": "localized bug fix in one module"},
                {"label": "hard", "description": "cross-module reasoning or design decisions"},
            ],
        },
        "task_type": {
            "type": "choice",
            "instructions": "What kind of task is this?",
            "criteria": {
                "bugfix": "fix a defect or failing test",
                "feature": "add new behavior",
                "refactor": "restructure without changing behavior",
                "docs": "documentation or comments",
                "test": "add or fix tests only",
                "ops": "build, CI, config, tooling",
            },
        },
        "risk": {
            "type": "score",
            "instructions": "How risky is this change for the codebase?",
            "criteria": [
                {"label": "low", "description": "isolated change, easy to revert"},
                {"label": "medium", "description": "touches shared code or behavior"},
                {"label": "high", "description": "public API, data, or infrastructure"},
            ],
        },
        "ambiguity": {
            "type": "score",
            "instructions": "How clearly is the task specified?",
            "criteria": [
                {"label": "clear", "description": "fully specified"},
                {"label": "some", "description": "minor open questions"},
                {"label": "vague", "description": "goal or scope unclear"},
            ],
        },
        "decomposable": {
            "type": "noul",
            "instructions": "Can this task be split into independent subtasks handled in parallel?",
        },
    }


def _probabilities(answer: dict[str, Any], labels: list[str] | None = None) -> dict[str, float]:
    probs = answer.get("probabilities")
    if probs is None:
        probs = {}
    try:
        probs = {str(k): float(v) for k, v in probs.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise RouterError("invalid probabilities") from exc
    if labels and not probs:
        value = answer.get("score", answer.get("choice"))
        probs = {label: 1.0 if str(value) == label else 0.0 for label in labels}
    return probs


def _parse_answers(
    answers: dict[str, Any], candidates: list[str]
) -> tuple[str, float, dict[str, float], dict[str, Judgment]]:
    executor = answers.get("executor")
    choice = executor.get("choice") if isinstance(executor, dict) else None
    if choice is None or choice not in candidates:
        raise RouterError("missing or unknown executor choice")
    confidence = float(executor.get("confidence", 0.0))
    probabilities = _probabilities(executor)
    if abs(sum(probabilities.values()) - 1.0) > 0.02:
        raise RouterError("executor probabilities do not sum to 1")
    judgments: dict[str, Judgment] = {
        "executor": Judgment(
            kind="choice", value=choice, confidence=confidence, probabilities=probabilities
        )
    }
    for name, answer in answers.items():
        if name == "executor" or not isinstance(answer, dict):
            continue
        kind = answer.get("type")
        if kind == "noul":
            p = float(answer.get("noul", answer.get("probability", 0.0)))
            judgments[name] = Judgment(
                kind="noul",
                value=p >= 0.5,
                confidence=abs(p - 0.5) * 2,
                probabilities={"true": p, "false": 1 - p},
            )
        elif kind == "score":
            value = float(answer.get("score"))
            probs = _probabilities(answer)
            legend = answer.get("legend", {})
            mapped = {str(legend.get(str(k), {}).get("label", k)): v for k, v in probs.items()}
            judgments[name] = Judgment(
                kind="score",
                value=value,
                confidence=float(answer.get("confidence", 0.0)),
                probabilities=mapped,
            )
        elif kind == "choice":
            value = answer.get("choice")
            judgments[name] = Judgment(
                kind="choice",
                value=value,
                confidence=float(answer.get("confidence", 0.0)),
                probabilities=_probabilities(answer),
            )
    return choice, confidence, probabilities, judgments


def parse_answers(
    answers: dict[str, Any], candidates: list[str]
) -> tuple[str, float, dict[str, float], dict[str, Judgment]]:
    try:
        return _parse_answers(answers, candidates)
    except RouterError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RouterError("invalid router answer") from exc
