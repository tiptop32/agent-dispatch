from __future__ import annotations

from collections.abc import Iterable

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.executors.workspace import is_git_repo
from agent_dispatch.models import (
    ConfidenceTier,
    DispatchRequest,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
    SourceAgent,
)
from agent_dispatch.routing.capability import (
    NO_IN_PERIMETER_NOTE,
    PERIMETER_UNKNOWN_NOTE,
    TIERS,
    perimeter_required,
    select,
)


def _event(reason: GuardReason, detail: str, executor: str | None = None) -> GuardEvent:
    return GuardEvent(reason=reason, detail=detail, executor=executor)


#: Сколько task_id работающих sibling'ов помещается в отказ: компактный
#: ответ MCP показывает `error` целиком только до 500 символов.
MAX_SIBLING_IDS_IN_REFUSAL = 6


def _running_siblings(ids: list[str]) -> str:
    shown = ids[:MAX_SIBLING_IDS_IN_REFUSAL]
    tail = f" (+{len(ids) - len(shown)} more)" if len(ids) > len(shown) else ""
    return ", ".join(shown) + tail


def pre_guards(
    req: DispatchRequest,
    settings: Settings,
    unavailable: set[str],
    parent_exists: bool,
    sibling_count: int,
    review_only: set[str] | frozenset[str] = frozenset(),
    running_siblings: list[str] | None = None,
) -> GuardEvent | RouteDecision | None:
    if not is_git_repo(req.cwd):
        return _event(GuardReason.bad_cwd, f"cwd is not a git repository: {req.cwd}")
    if req.hop >= settings.routing.max_hops:
        return _event(GuardReason.max_hops, f"hop {req.hop} >= {settings.routing.max_hops}")
    if req.parent_task_id and not parent_exists:
        return _event(GuardReason.unknown_parent, f"parent task not found: {req.parent_task_id}")
    if req.parent_task_id and sibling_count >= settings.routing.max_children:
        detail = f"sibling count {sibling_count} >= {settings.routing.max_children}"
        if running_siblings:
            # Слот освободит один из работающих sibling'ов: повторный dispatch
            # откажет снова, ждать надо через status, а не через dispatch.
            detail += (
                f"; running siblings: {_running_siblings(running_siblings)};"
                " wait for one via status(task_id, wait_seconds) instead of dispatching again"
            )
        return _event(GuardReason.max_children, detail)
    if req.executor is not None:
        if req.executor not in settings.executors or not settings.executors[req.executor].enabled:
            return _event(
                GuardReason.disabled, f"executor is disabled: {req.executor}", req.executor
            )
        if req.executor in review_only:
            return _event(
                GuardReason.review_only,
                f"{req.executor} takes only reviews from {req.source_agent.value}:"
                " pass kind='review' to ask for a review, or dispatch without executor"
                " so the router picks someone else for the work",
                req.executor,
            )
        if req.executor in unavailable:
            return _event(
                GuardReason.unavailable, f"executor is unavailable: {req.executor}", req.executor
            )
        return RouteDecision(
            router=RouterKind.override,
            executor=req.executor,
            reason=GuardReason.user_override,
            confidence=1.0,
            scores={req.executor: 1.0},
        )
    return None


def candidates(
    settings: Settings, unavailable: set[str], source_agent: SourceAgent, hop: int
) -> list[str]:
    result: list[str] = []
    for name, executor in settings.executors.items():
        if not executor.enabled or name in unavailable:
            continue
        if (
            settings.routing.exclude_source_agent
            and hop == 0
            and source_agent in {SourceAgent.claude, SourceAgent.codex, SourceAgent.opencode}
            and executor.adapter == source_agent.value
        ):
            continue
        result.append(name)
    return result


def single_candidate_decision(name: str) -> RouteDecision:
    return RouteDecision(
        router=RouterKind.fallback,
        executor=name,
        confidence=1.0,
        scores={name: 1.0},
        reason=GuardReason.single_candidate,
    )


def classify_confidence(confidence: float, settings: Settings) -> ConfidenceTier:
    """Насколько решению можно доверять.

    `autonomous` — можно выполнять как есть, `advisory` — стоит посмотреть самому,
    `fallback` — решение не несёт информации и подменяется запасным исполнителем.
    """
    if confidence >= settings.routing.autonomous_confidence:
        return "autonomous"
    if confidence >= settings.routing.min_confidence:
        return "advisory"
    return "fallback"


def pick_fallback(settings: Settings, candidates: Iterable[str]) -> str | None:
    """Первый доступный исполнитель из цепочки `fallback_executor`, иначе None."""
    allowed = set(candidates)
    return next((name for name in settings.routing.fallback_chain if name in allowed), None)


def fallback_unavailable(settings: Settings) -> str:
    return "fallback executor unavailable: " + ", ".join(settings.routing.fallback_chain)


def _perimeter_warning(settings: Settings) -> str:
    return "no fallback executor inside the corporate perimeter: " + ", ".join(
        settings.routing.fallback_chain
    )


def _within_ceiling(
    pool: dict[str, ExecutorSettings], ceiling: str | None
) -> dict[str, ExecutorSettings]:
    if ceiling not in TIERS:
        return pool
    return {
        name: item
        for name, item in pool.items()
        if item.tier in TIERS and TIERS.index(item.tier) <= TIERS.index(ceiling)
    }


def fallback_choice(
    settings: Settings,
    pool: dict[str, ExecutorSettings],
    *,
    perimeter: bool,
    ceiling: str | None,
    keep: str | None,
    capability: str | None,
) -> tuple[str | None, list[str], str | None]:
    """Замена слабого или отсутствующего решения: исполнитель, заметки, предупреждение.

    Пул сужается до периметра (если он требуется и внутри кто-то есть), затем
    до `max_capability`. Внутри — первый из `fallback_chain`. Цепочки в пуле
    нет: в периметре остаётся `keep`, если он внутри, иначе `select` по тиру;
    вне периметра — `keep`, если он не выше потолка. None — оставить выбор
    роутера (он уже ограничен потолком).
    """
    notes: list[str] = []
    inside = {name: item for name, item in pool.items() if item.corporate} if perimeter else {}
    if perimeter and not inside:
        # Применить некому: отказ выносит диспетчер (`guard: perimeter`).
        notes.append(NO_IN_PERIMETER_NOTE)
    base = inside or pool
    capped = _within_ceiling(base, ceiling)
    if not capped:
        if not inside and keep is not None:
            notes.append(f"no fallback within max_capability '{ceiling}'; kept the router choice")
            return None, notes, None
        notes.append(
            f"no fallback executor within max_capability '{ceiling}' inside the "
            "corporate perimeter; the perimeter was kept"
            if inside
            else f"no executor within max_capability '{ceiling}'; the ceiling was not applied"
        )
        capped = base
    fallback = pick_fallback(settings, capped)
    if fallback is not None:
        return fallback, notes, None
    if inside:
        if keep not in capped:
            keep = select(capability or ceiling or "balanced", capped, corporate=True).executor
        return keep, notes, _perimeter_warning(settings)
    # Вне периметра `keep` выше потолка не берётся: None оставляет выбор
    # роутера, уже ограниченный `max_capability`.
    return (keep if keep in capped else None), notes, fallback_unavailable(settings)


def post_guards(
    decision: RouteDecision,
    settings: Settings,
    candidates: list[str],
    *,
    max_capability: str | None = None,
) -> tuple[RouteDecision, list[GuardEvent]]:
    result = decision.model_copy(deep=True)
    result.confidence_tier = classify_confidence(result.confidence, settings)
    events: list[GuardEvent] = []
    # Оценки исполнителей вне кандидатов не в счёт: роутер (claude_local)
    # может вернуть их, и тогда подмена fallback увела бы задачу к тому, кого
    # guards исключили (остывающему или закрытому правилом review_only).
    allowed = set(candidates)
    ranked = sorted(
        ((name, score) for name, score in result.scores.items() if name in allowed),
        key=lambda item: item[1],
        reverse=True,
    )
    top1 = ranked[0][0] if ranked else (candidates[0] if candidates else result.executor)
    top_score = ranked[0][1] if ranked else result.confidence
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    # Периметр применяется к решению целиком, не только к fallback: роутер,
    # который не задавал вопроса о корп. данных, не выбирает внешнего
    # исполнителя уверенно.
    pool = {name: item for name in candidates if (item := settings.executors.get(name)) is not None}
    verdict = perimeter_required(
        pool,
        perimeter=settings.routing.corporate_perimeter,
        corporate=decision.judgments.get("corporate_data"),
        min_confidence=settings.routing.corporate_min_confidence,
        configured=settings.executors,
    )
    if verdict.required:
        result.meta["perimeter"] = verdict.status
    inside = {name: item for name, item in pool.items() if item.corporate}
    if verdict.status == "unknown" and inside:
        notes = result.meta.setdefault("selection_notes", [])
        if PERIMETER_UNKNOWN_NOTE not in notes:
            notes.append(PERIMETER_UNKNOWN_NOTE)
    ceiling = max_capability if max_capability in TIERS else None

    weak = False
    if result.confidence < settings.routing.min_confidence:
        weak = True
        result.reason = GuardReason.low_confidence
        events.append(
            _event(GuardReason.low_confidence, f"confidence {decision.confidence} below threshold")
        )
    elif top_score - second_score < settings.routing.min_margin - 1e-9:
        weak = True
        result.reason = GuardReason.low_margin
        events.append(
            _event(GuardReason.low_margin, f"margin {top_score - second_score} below threshold")
        )
    outside = verdict.required and inside and result.executor not in inside
    if weak or outside:
        # Слабое решение подменяется fallback; уверенный выбор вне периметра
        # (роутер вопроса не задавал) сужается тем же правилом.
        fallback, notes, warning = fallback_choice(
            settings,
            pool,
            perimeter=verdict.required,
            ceiling=ceiling,
            keep=top1 if weak else None,
            capability=decision.capability,
        )
        for note in notes:
            listed = result.meta.setdefault("selection_notes", [])
            if note not in listed:
                listed.append(note)
        if fallback is not None:
            result.executor = fallback
        if warning is not None and weak:
            result.meta["warning"] = warning
            events.append(
                _event(GuardReason.unavailable, warning, settings.routing.fallback_chain[0])
            )
    elif verdict.required and not inside:
        listed = result.meta.setdefault("selection_notes", [])
        if NO_IN_PERIMETER_NOTE not in listed:
            listed.append(NO_IN_PERIMETER_NOTE)
    if candidates and result.executor not in allowed:
        chosen = result.executor
        result.executor = top1
        events.append(
            _event(GuardReason.unavailable, f"router chose a non-candidate: {chosen}", chosen)
        )
    return result, events


def review_only_executors(req: DispatchRequest, settings: Settings) -> set[str]:
    """Исполнители, которым этот источник может отдать только ревью, а не задачу.

    Правило `routing.review_only` бережёт токены дорогого исполнителя: Codex
    делает работу сам или отдаёт её дешёвым, а у Claude просит только ревью.
    """
    if req.kind == "review":
        return set()
    adapters = set(settings.routing.review_only.get(req.source_agent.value, []))
    return {name for name, item in settings.executors.items() if item.adapter in adapters}
