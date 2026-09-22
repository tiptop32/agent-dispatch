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
from .claude_local import ClaudeLocalRouter
from .guards import post_guards, single_candidate_decision
from .jev import JevRouter


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
            final, post_events = post_guards(decision, settings, names)
            return final, events + post_events
        except RouterError as exc:
            events.append(
                GuardEvent(reason=GuardReason.router_unavailable, detail=f"{router.name}: {exc}")
            )
    fallback = settings.routing.fallback_executor
    extra_events: list[GuardEvent] = []
    meta: dict[str, str] = {}
    if fallback not in names:
        fallback = names[0]
        warning = f"fallback executor unavailable: {settings.routing.fallback_executor}"
        meta["warning"] = warning
        extra_events.append(
            GuardEvent(
                reason=GuardReason.unavailable,
                detail=warning,
                executor=settings.routing.fallback_executor,
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
