from __future__ import annotations

from typing import Any

from agent_dispatch.models import DispatchRequest, Judgment

from .base import RouterError
from .capability import CAPABILITY_CRITERIA


def build_state(req: DispatchRequest) -> dict[str, Any]:
    return {
        "task": req.task,
        "context": req.context,
        "files": req.files,
        "constraints": req.constraints,
        "source_agent": req.source_agent.value,
    }


def build_questions(capabilities: list[str], *, ask_corporate: bool = False) -> dict[str, Any]:
    """Вопросы одного вызова Jev.

    Несущий вопрос это `capability`: какого уровня работы требует задача. Имена
    моделей Jev не показываются, соответствие подбирает `capability.select`.
    """
    questions: dict[str, Any] = {
        "capability": {
            "type": "choice",
            "instructions": (
                "What level of coding capability does this task demand from the agent that "
                "will implement it?"
            ),
            "criteria": {tier: CAPABILITY_CRITERIA[tier] for tier in capabilities},
        },
        "judgment": {
            "type": "noul",
            "instructions": (
                "Does this task require resolving trade-offs or ambiguous requirements, rather "
                "than only executing a clear specification?"
            ),
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
    if ask_corporate:
        questions["corporate_data"] = {
            "type": "noul",
            "instructions": (
                "Does this task involve internal corporate code or data that must not be sent "
                "to an external model provider?"
            ),
        }
    return questions


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


def _judgment_from(name: str, answer: dict[str, Any]) -> Judgment | None:
    kind = answer.get("type")
    if kind == "noul":
        probability = float(answer.get("noul", answer.get("probability", 0.0)))
        return Judgment(
            kind="noul",
            value=noul_yes(probability),
            confidence=noul_certainty(probability),
            probabilities={"true": probability, "false": 1 - probability},
        )
    if kind == "score":
        probs = _probabilities(answer)
        legend = answer.get("legend", {})
        mapped = {str(legend.get(str(k), {}).get("label", k)): v for k, v in probs.items()}
        return Judgment(
            kind="score",
            value=float(answer.get("score")),
            confidence=float(answer.get("confidence", 0.0)),
            probabilities=mapped,
        )
    if kind == "choice":
        return Judgment(
            kind="choice",
            value=answer.get("choice"),
            confidence=float(answer.get("confidence", 0.0)),
            probabilities=_probabilities(answer),
        )
    return None


def noul_certainty(probability: float) -> float:
    """Уверенность noul-ответа: 0.5 это «не знаю», а не «средне»."""
    return min(1.0, abs(probability - 0.5) * 2)


def noul_yes(probability: float) -> bool:
    return probability >= 0.5


def _parse_answers(
    answers: dict[str, Any], capabilities: list[str]
) -> tuple[str, float, dict[str, float], dict[str, Judgment]]:
    capability_answer = answers.get("capability")
    choice = capability_answer.get("choice") if isinstance(capability_answer, dict) else None
    if choice is None or choice not in capabilities:
        raise RouterError("missing or unknown capability choice")
    confidence = float(capability_answer.get("confidence", 0.0))
    probabilities = _probabilities(capability_answer)
    if abs(sum(probabilities.values()) - 1.0) > 0.02:
        raise RouterError("capability probabilities do not sum to 1")
    judgments: dict[str, Judgment] = {
        "capability": Judgment(
            kind="choice", value=choice, confidence=confidence, probabilities=probabilities
        )
    }
    for name, answer in answers.items():
        if name == "capability" or not isinstance(answer, dict):
            continue
        judgment = _judgment_from(name, answer)
        if judgment is not None:
            judgments[name] = judgment
    return choice, confidence, probabilities, judgments


def parse_answers(
    answers: dict[str, Any], capabilities: list[str]
) -> tuple[str, float, dict[str, float], dict[str, Judgment]]:
    try:
        return _parse_answers(answers, capabilities)
    except RouterError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RouterError("invalid router answer") from exc
