"""Отображение ответа Jev о нужной capability на конкретного исполнителя.

Jev спрашивается о том, что требует задача (`fast`, `balanced`, `strong`), а не
о том, какая модель её возьмёт. Вендорские описания моделей неразличимы, и выбор
между десятком имён даёт плоское распределение; выбор между тремя категориями
требований даёт уверенность около единицы. Соответствие категории и исполнителя
живёт здесь, в обычном коде, и меняется правкой `tier` в конфиге.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_dispatch.config import ExecutorSettings

TIERS: tuple[str, ...] = ("fast", "balanced", "strong")

CAPABILITY_CRITERIA: dict[str, str] = {
    "fast": (
        "Mechanical or fully specified work: a rename, a typo, a docstring, formatting, "
        "boilerplate, a one-file edit whose shape is obvious from the request."
    ),
    "balanced": (
        "Ordinary engineering inside one repository: a bug fix from a failing test, a small "
        "feature, a local refactor, tests written by example."
    ),
    "strong": (
        "Work that needs design decisions, trade-offs resolved, or reasoning across several "
        "modules before any code can be written."
    ),
}

#: Адаптер, которому отдаётся задача, требующая суждения, а не исполнения.
JUDGMENT_ADAPTER = "claude"


@dataclass
class Selection:
    executor: str
    scores: dict[str, float]
    notes: list[str] = field(default_factory=list)


def tiers_present(candidates: dict[str, ExecutorSettings]) -> list[str]:
    """Тиры, у которых есть хотя бы один кандидат, в порядке от fast к strong."""
    present = {settings.tier for settings in candidates.values()}
    return [tier for tier in TIERS if tier in present]


def has_corporate(candidates: dict[str, ExecutorSettings]) -> bool:
    return any(settings.corporate for settings in candidates.values())


def _nearest_tiers(tier: str) -> list[str]:
    """Тиры по удалённости от запрошенного: точное совпадение, затем вверх, затем вниз."""
    if tier not in TIERS:
        return list(TIERS)
    index = TIERS.index(tier)
    order = [tier]
    order.extend(TIERS[index + 1 :])
    order.extend(reversed(TIERS[:index]))
    return order


def _pick(pool: dict[str, ExecutorSettings], tier: str) -> str:
    for candidate_tier in _nearest_tiers(tier):
        for name, settings in pool.items():
            if settings.tier == candidate_tier:
                return name
    return next(iter(pool))


def _at(confidence: float | None) -> str:
    return "" if confidence is None else f" (confidence {confidence:.2f})"


def select(
    capability: str,
    candidates: dict[str, ExecutorSettings],
    *,
    judgment: bool = False,
    corporate: bool = False,
    corporate_confidence: float | None = None,
    probabilities: dict[str, float] | None = None,
) -> Selection:
    """Выбрать исполнителя под запрошенную capability.

    Порядок сужения: периметр корпоративных данных, потребность в суждении, тир.
    Внутри равных побеждает тот, кто идёт раньше в конфиге.

    `corporate_confidence` только попадает в notes: решение сузить периметр
    принимается снаружи, здесь оно уже готово в `corporate`.
    """
    if not candidates:
        raise ValueError("no candidates")
    notes: list[str] = []
    pool = dict(candidates)

    corporate_pool = {name: s for name, s in pool.items() if s.corporate}
    if corporate and corporate_pool:
        pool = corporate_pool
        notes.append(
            f"task involves corporate data{_at(corporate_confidence)}; "
            "only in-perimeter executors were considered"
        )
        tiers = {settings.tier for settings in pool.values()}
        if capability in TIERS and capability not in tiers:
            # Периметр может не содержать нужного уровня: задача уедет к
            # соседнему тиру, и об этом надо сказать, а не молча понизить.
            notes.append(
                f"no in-perimeter executor at the requested level '{capability}'; "
                f"available inside the perimeter: {', '.join(sorted(tiers))}"
            )
    elif corporate:
        notes.append(
            f"task involves corporate data{_at(corporate_confidence)} "
            "but no in-perimeter executor is available"
        )

    if judgment:
        judgment_pool = {name: s for name, s in pool.items() if s.adapter == JUDGMENT_ADAPTER}
        if judgment_pool:
            pool = judgment_pool
            notes.append("task needs judgment rather than execution; sent to a reasoning agent")

    executor = _pick(pool, capability)
    return Selection(executor=executor, scores=_scores(pool, probabilities or {}), notes=notes)


def _scores(pool: dict[str, ExecutorSettings], probabilities: dict[str, float]) -> dict[str, float]:
    """Вероятности тиров, перенесённые на представителя каждого тира.

    Так `post_guards` может считать отрыв первого кандидата от второго теми же
    правилами, что и при прямом выборе исполнителя.
    """
    scores: dict[str, float] = {}
    for tier, probability in probabilities.items():
        if tier not in TIERS:
            continue
        representative = _pick(pool, tier)
        scores[representative] = scores.get(representative, 0.0) + float(probability)
    return scores
