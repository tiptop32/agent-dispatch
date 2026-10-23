from __future__ import annotations

import httpx

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import (
    DispatchRequest,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
)

from .base import Router, RouterError
from .capability import (
    PERIMETER_UNKNOWN_NOTE,
    TIERS,
    perimeter_applies,
    perimeter_required,
    select,
)
from .claude_local import ClaudeLocalRouter
from .guards import fallback_choice, post_guards, single_candidate_decision
from .jev import JevRouter


def _apply_capability_ceiling(
    req: DispatchRequest,
    decision: RouteDecision,
    candidates: dict[str, ExecutorSettings],
    settings: Settings,
) -> RouteDecision:
    """Ограничить выбор роутера уровнем запроса, не ослабляя сужения пула."""
    ceiling = req.max_capability
    if ceiling is None or decision.router not in {RouterKind.jev, RouterKind.claude_local}:
        return decision
    capability = decision.capability
    if capability is None and decision.executor in candidates:
        capability = candidates[decision.executor].tier
    if capability not in TIERS or TIERS.index(capability) <= TIERS.index(ceiling):
        return decision

    verdict = perimeter_required(
        candidates,
        perimeter=settings.routing.corporate_perimeter,
        corporate=decision.judgments.get("corporate_data"),
        min_confidence=settings.routing.corporate_min_confidence,
        configured=settings.executors,
    )
    corporate = decision.judgments.get("corporate_data")
    judgment = decision.judgments.get("judgment")
    pool_args = {
        "judgment": bool(judgment and judgment.value),
        "corporate": verdict.required,
        "corporate_confidence": corporate.confidence if corporate else None,
    }
    selection = select(capability, candidates, max_capability=ceiling, **pool_args)
    if verdict.status == "unknown" and PERIMETER_UNKNOWN_NOTE not in selection.notes:
        selection.notes.append(PERIMETER_UNKNOWN_NOTE)
    # Заметки выбора без потолка («sent to a reasoning agent») описывают уже
    # отменённый выбор: их заменяют заметки нового. Заметки самого роутера
    # (например, о низкой уверенности ответа про корп. данные) остаются.
    stale = set(select(capability, candidates, **pool_args).notes)
    kept = [note for note in decision.meta.get("selection_notes", []) if note not in stale]
    result = decision.model_copy(deep=True)
    result.executor = selection.executor
    result.capability = selection.capability
    result.meta["capability_uncapped"] = selection.capability_uncapped
    result.meta["selection_notes"] = kept + selection.notes
    return result


async def decide_with_fallback(
    req: DispatchRequest,
    candidates: dict[str, ExecutorSettings],
    settings: Settings,
    routers: list[Router],
) -> tuple[RouteDecision, list[GuardEvent]]:
    names = list(candidates)
    configured = settings.executors
    perimeter = perimeter_applies(settings.routing.corporate_perimeter, configured)
    if len(names) == 1 and not perimeter:
        return single_candidate_decision(names[0]), []
    # При применимом периметре короткого пути нет: ответ о корп. данных решает
    # судьбу всей линии задачи (эскалации, вложенных задач). Без вопроса
    # пришлось бы либо выпускать её наружу, либо запирать в периметре вслепую;
    # «неизвестно» остаётся только на случай отказа роутеров.
    events: list[GuardEvent] = []
    for router in routers:
        try:
            decision = await router.decide(req, candidates)
            decision = _apply_capability_ceiling(req, decision, candidates, settings)
            final, post_events = post_guards(
                decision, settings, names, max_capability=req.max_capability
            )
            return final, events + post_events
        except RouterError as exc:
            events.append(
                GuardEvent(reason=GuardReason.router_unavailable, detail=f"{router.name}: {exc}")
            )
    # Роутеров нет или все упали: ответа о корп. данных нет. При включённом
    # периметре это «неизвестно», и fallback не выходит за периметр (fail
    # closed): отказ обоих роутеров не должен выводить задачу наружу молча.
    pool = {name: item for name in names if (item := configured.get(name)) is not None}
    fallback, notes, warning = fallback_choice(
        settings,
        pool,
        perimeter=perimeter,
        ceiling=req.max_capability,
        keep=None,
        capability=req.max_capability,
    )
    meta: dict[str, str | list[str]] = {}
    extra_events: list[GuardEvent] = []
    if perimeter:
        meta["perimeter"] = "unknown"
        if any(item.corporate for item in pool.values()):
            notes.append(PERIMETER_UNKNOWN_NOTE)
    if fallback is None:
        capped = [
            name
            for name, item in pool.items()
            if req.max_capability not in TIERS
            or (item.tier in TIERS and TIERS.index(item.tier) <= TIERS.index(req.max_capability))
        ]
        fallback = (capped or names)[0]
    if notes:
        meta["selection_notes"] = notes
    if warning is not None:
        meta["warning"] = warning
        extra_events.append(
            GuardEvent(
                reason=GuardReason.unavailable,
                detail=warning,
                executor=settings.routing.fallback_chain[0],
            )
        )
    decision = RouteDecision(
        router=RouterKind.fallback,
        executor=fallback,
        confidence=0.0,
        scores={},
        reason=GuardReason.router_unavailable,
        meta=meta,
    )
    return decision, events + extra_events


def build_routers(settings: Settings, client: httpx.AsyncClient) -> list[Router]:
    local = ClaudeLocalRouter(settings)
    if settings.router.backend == "claude_local":
        return [local]
    return [JevRouter(settings, client), local]
