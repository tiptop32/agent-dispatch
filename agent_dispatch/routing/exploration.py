"""Детерминированное разбиение задач для сравнения Jev со статикой."""

from __future__ import annotations

import hashlib
from collections.abc import Collection

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import DispatchRequest, Judgment, RouteDecision, RouterKind

from .capability import has_corporate, select

STATIC_BASELINE_TIER = "balanced"
ARM_JUDGMENT = "exploration_arm"
JEV_CHOICE_JUDGMENT = "exploration_jev_choice"
STATIC_CHOICE_JUDGMENT = "exploration_static_choice"
CROSSOVER_JUDGMENT = "exploration_crossover"


def exploration_fraction(task_id: str) -> float:
    """Вернуть стабильную долю [0, 1) из идентификатора задачи.

    Первые 64 бита SHA-256 дают одинаковый arm на разных процессах и после
    рестарта, в отличие от встроенного `hash()` и глобального генератора.
    """
    prefix = hashlib.sha256(task_id.encode()).digest()[:8]
    return int.from_bytes(prefix, "big") / 2**64


def is_explore_task(task_id: str, share: float) -> bool:
    """Попадает ли задача в экспериментальную долю трафика."""
    if share <= 0.0:
        return False
    if share >= 1.0:
        return True
    return exploration_fraction(task_id) < share


def _choice(value: str, confidence: float) -> Judgment:
    return Judgment(kind="choice", value=value, confidence=confidence, probabilities={})


def _static_executor(
    decision: RouteDecision,
    candidates: dict[str, ExecutorSettings],
    settings: Settings,
) -> str:
    corporate = decision.judgments.get("corporate_data")
    narrow = bool(
        settings.routing.corporate_perimeter
        and has_corporate(candidates)
        and corporate
        and corporate.value
        and corporate.confidence >= settings.routing.corporate_min_confidence
    )
    return select(STATIC_BASELINE_TIER, candidates, corporate=narrow).executor


def apply_exploration(
    task_id: str | None,
    request: DispatchRequest,
    decision: RouteDecision,
    candidates: dict[str, ExecutorSettings],
    settings: Settings,
    *,
    is_escalation: bool = False,
    unavailable: Collection[str] = (),
) -> RouteDecision:
    """Подменить пригодное решение Jev статическим executor для выбранного arm.

    Решение сначала проходит обычные guards. Поэтому эксперимент видит только
    фактический выбор Jev и получает уже отфильтрованный пул кандидатов. Явные
    назначения, специальные связи и fallback-решения остаются нетронутыми.
    """
    if (
        task_id is None
        or settings.routing.explore_share <= 0.0
        or request.executor is not None
        or request.kind == "review"
        or request.followup_of is not None
        or is_escalation
        or decision.router != RouterKind.jev
        or decision.reason is not None
        or not candidates
    ):
        return decision

    result = decision.model_copy(deep=True)
    jev_executor = result.executor
    static_executor = _static_executor(result, candidates, settings)
    explored = is_explore_task(task_id, settings.routing.explore_share)
    arm = "explore" if explored else "jev"
    result.judgments[ARM_JUDGMENT] = _choice(arm, 1.0)
    result.judgments[JEV_CHOICE_JUDGMENT] = _choice(jev_executor, result.confidence)
    result.judgments[STATIC_CHOICE_JUDGMENT] = _choice(static_executor, 1.0)

    if not explored:
        return result
    if static_executor in unavailable:
        # Arm остаётся `explore`: перенос в контрольную группу смешал бы её
        # с задачами, совпавшими с недоступностью (нагрузка, лимиты). Replay
        # считает crossover отдельно и судит по назначенному arm.
        result.judgments[CROSSOVER_JUDGMENT] = _choice(static_executor, 1.0)
        result.meta["exploration_note"] = (
            f"статический исполнитель {static_executor} стал недоступен; сохранён выбор Jev"
        )
        return result

    result.executor = static_executor
    result.router = RouterKind.explore
    return result
