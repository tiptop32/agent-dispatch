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
from .capability import TIERS, narrows_to_perimeter, select
from .claude_local import ClaudeLocalRouter
from .guards import fallback_unavailable, pick_fallback, post_guards, single_candidate_decision
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

    corporate = decision.judgments.get("corporate_data")
    narrow = narrows_to_perimeter(
        candidates,
        answered_yes=bool(corporate and corporate.value),
        confidence=corporate.confidence if corporate else 0.0,
        perimeter=settings.routing.corporate_perimeter,
        min_confidence=settings.routing.corporate_min_confidence,
    )
    judgment = decision.judgments.get("judgment")
    selection = select(
        capability,
        candidates,
        max_capability=ceiling,
        judgment=bool(judgment and judgment.value),
        corporate=narrow,
        corporate_confidence=corporate.confidence if corporate else None,
    )
    result = decision.model_copy(deep=True)
    result.executor = selection.executor
    result.capability = selection.capability
    result.meta["capability_uncapped"] = selection.capability_uncapped
    notes = list(result.meta.get("selection_notes", []))
    notes.append(
        f"capability capped: {selection.capability_uncapped} -> "
        f"{selection.capability} (max_capability)"
    )
    result.meta["selection_notes"] = notes
    return result


async def decide_with_fallback(
    req: DispatchRequest,
    candidates: dict[str, ExecutorSettings],
    settings: Settings,
    routers: list[Router],
) -> tuple[RouteDecision, list[GuardEvent]]:
    names = list(candidates)
    if len(names) == 1:
        return single_candidate_decision(names[0]), []
    events: list[GuardEvent] = []
    for router in routers:
        try:
            decision = await router.decide(req, candidates)
            decision = _apply_capability_ceiling(req, decision, candidates, settings)
            final, post_events = post_guards(decision, settings, names)
            return final, events + post_events
        except RouterError as exc:
            events.append(
                GuardEvent(reason=GuardReason.router_unavailable, detail=f"{router.name}: {exc}")
            )
    fallback = pick_fallback(settings, names)
    extra_events: list[GuardEvent] = []
    meta: dict[str, str] = {}
    if fallback is None:
        fallback = names[0]
        warning = fallback_unavailable(settings)
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
