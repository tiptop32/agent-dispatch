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
from agent_dispatch.models import Judgment

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
    capability: str | None = None
    capability_uncapped: str | None = None


def tiers_present(candidates: dict[str, ExecutorSettings]) -> list[str]:
    """Тиры, у которых есть хотя бы один кандидат, в порядке от fast к strong."""
    present = {settings.tier for settings in candidates.values()}
    return [tier for tier in TIERS if tier in present]


def has_corporate(candidates: dict[str, ExecutorSettings]) -> bool:
    return any(settings.corporate for settings in candidates.values())


#: Почему пул сужен без ответа Jev: вопрос не задавался или ответа нет.
PERIMETER_UNKNOWN_NOTE = (
    "no corporate_data answer (router fallback or a router that does not ask); "
    "the corporate perimeter is on, so the pool was narrowed to in-perimeter "
    "executors (fail closed)"
)

#: Внутри кандидатов нет исполнителя периметра: сужать некого, но молчать нельзя.
NO_IN_PERIMETER_NOTE = "task involves corporate data but no in-perimeter executor is available"


@dataclass
class PerimeterVerdict:
    """Применим ли периметр к решению и почему.

    `required` значит, что пул сужен до `corporate: true`; `status` —
    `answered` (по ответу Jev) или `unknown` (ответа нет, сужение fail closed);
    оба пусты, когда периметр не применим.
    """

    required: bool
    status: str | None = None
    note: str | None = None


def narrows_to_perimeter(
    candidates: dict[str, ExecutorSettings],
    *,
    answered_yes: bool,
    confidence: float,
    perimeter: bool,
    min_confidence: float,
) -> bool:
    """Сужать ли пул до корпоративного периметра по ответу о корп. данных.

    Одно правило для Jev, статического плеча эксперимента и replay: периметр
    включён, в пуле есть корпоративный исполнитель, ответ «да» и его
    уверенность не ниже `routing.corporate_min_confidence`.
    """
    return bool(
        perimeter and has_corporate(candidates) and answered_yes and confidence >= min_confidence
    )


def perimeter_applies(perimeter: bool, configured: dict[str, ExecutorSettings]) -> bool:
    """Периметр включён и в конфиге есть включённый `corporate: true` исполнитель.

    Считается по конфигу, а не по доступным сейчас кандидатам: остывание всех
    корпоративных исполнителей не должно молча открывать периметр.
    """
    return perimeter and any(item.corporate and item.enabled for item in configured.values())


def perimeter_required(
    candidates: dict[str, ExecutorSettings],
    *,
    perimeter: bool,
    corporate: Judgment | None,
    min_confidence: float,
    configured: dict[str, ExecutorSettings] | None = None,
) -> PerimeterVerdict:
    """Единственное место, решающее, применим ли периметр к решению.

    Три случая. Периметр выключен или среди кандидатов нет `corporate: true` —
    не применим. Ответ Jev есть — прежнее правило `narrows_to_perimeter`
    вместе с `corporate_min_confidence`. Ответа нет, пока периметр включён и
    корпоративные исполнители в пуле есть, — применим (fail closed): fallback
    роутеров и `claude_local`, который вопрос не задаёт, не должны выводить
    задачу за периметр молча.

    `configured` (исполнители из конфига) решает, применим ли периметр вообще;
    без него судят по `candidates`. Требование без корпоративных кандидатов
    остаётся требованием: отказ выносит диспетчер (`guard: perimeter`).
    """
    if not perimeter_applies(perimeter, {**(configured or {}), **candidates}):
        return PerimeterVerdict(required=False)
    if corporate is None:
        return PerimeterVerdict(required=True, status="unknown", note=PERIMETER_UNKNOWN_NOTE)
    # То же правило, что `narrows_to_perimeter`, но без проверки кандидатов:
    # применимость уже решена по конфигу выше.
    if corporate.value and corporate.confidence >= min_confidence:
        return PerimeterVerdict(required=True, status="answered")
    if corporate.value:
        # Ответ «да», но не увереннее порога: это монетка, а не решение.
        return PerimeterVerdict(
            required=False,
            note=(
                f"corporate_data answered yes but only at confidence {corporate.confidence:.2f}, "
                f"below routing.corporate_min_confidence={min_confidence:.2f}: "
                "the perimeter filter was not applied"
            ),
        )
    return PerimeterVerdict(required=False)


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
    max_capability: str | None = None,
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
    capability_uncapped: str | None = None
    if (
        capability in TIERS
        and max_capability in TIERS
        and TIERS.index(capability) > TIERS.index(max_capability)
    ):
        capability_uncapped = capability
        capability = max_capability
        notes.append(f"capability capped: {capability_uncapped} -> {capability} (max_capability)")

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
        if max_capability in TIERS:
            # Потолок вызывающего сильнее совета «нужно суждение»: иначе
            # reasoning-исполнитель уровня strong обошёл бы его молча. Периметр
            # данных выше по коду остаётся сильнее потолка.
            within = {
                name: s
                for name, s in judgment_pool.items()
                if s.tier in TIERS and TIERS.index(s.tier) <= TIERS.index(max_capability)
            }
            if judgment_pool and not within:
                notes.append(
                    "task needs judgment, but no reasoning agent within "
                    f"max_capability '{max_capability}'; the ceiling was kept"
                )
            judgment_pool = within
        if judgment_pool:
            pool = judgment_pool
            notes.append("task needs judgment rather than execution; sent to a reasoning agent")

    executor = _pick(pool, capability)
    return Selection(
        executor=executor,
        scores=_scores(pool, probabilities or {}),
        notes=notes,
        capability=capability,
        capability_uncapped=capability_uncapped,
    )


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
