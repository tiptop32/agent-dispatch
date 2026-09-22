from __future__ import annotations

from agent_dispatch.config import Settings
from agent_dispatch.executors.workspace import is_git_repo
from agent_dispatch.models import (
    DispatchRequest,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
    SourceAgent,
)


def _event(reason: GuardReason, detail: str, executor: str | None = None) -> GuardEvent:
    return GuardEvent(reason=reason, detail=detail, executor=executor)


def pre_guards(
    req: DispatchRequest,
    settings: Settings,
    unavailable: set[str],
    parent_exists: bool,
    sibling_count: int,
) -> GuardEvent | RouteDecision | None:
    if not is_git_repo(req.cwd):
        return _event(GuardReason.bad_cwd, f"cwd is not a git repository: {req.cwd}")
    if req.hop >= settings.routing.max_hops:
        return _event(GuardReason.max_hops, f"hop {req.hop} >= {settings.routing.max_hops}")
    if req.parent_task_id and not parent_exists:
        return _event(GuardReason.unknown_parent, f"parent task not found: {req.parent_task_id}")
    if req.parent_task_id and sibling_count >= settings.routing.max_children:
        return _event(
            GuardReason.max_children,
            f"sibling count {sibling_count} >= {settings.routing.max_children}",
        )
    if req.executor is not None:
        if req.executor not in settings.executors or not settings.executors[req.executor].enabled:
            return _event(
                GuardReason.disabled, f"executor is disabled: {req.executor}", req.executor
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


def classify_confidence(confidence: float, settings: Settings) -> str:
    """Насколько решению можно доверять.

    `autonomous` — можно выполнять как есть, `advisory` — стоит посмотреть самому,
    `fallback` — решение не несёт информации и подменяется запасным исполнителем.
    """
    if confidence >= settings.routing.autonomous_confidence:
        return "autonomous"
    if confidence >= settings.routing.min_confidence:
        return "advisory"
    return "fallback"


def post_guards(
    decision: RouteDecision, settings: Settings, candidates: list[str]
) -> tuple[RouteDecision, list[GuardEvent]]:
    result = decision.model_copy(deep=True)
    result.confidence_tier = classify_confidence(result.confidence, settings)
    events: list[GuardEvent] = []
    ranked = sorted(result.scores.items(), key=lambda item: item[1], reverse=True)
    top1 = ranked[0][0] if ranked else result.executor
    top_score = ranked[0][1] if ranked else result.confidence
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if result.confidence < settings.routing.min_confidence:
        result.executor = settings.routing.fallback_executor
        result.reason = GuardReason.low_confidence
        events.append(
            _event(GuardReason.low_confidence, f"confidence {decision.confidence} below threshold")
        )
    elif top_score - second_score < settings.routing.min_margin - 1e-9:
        result.executor = settings.routing.fallback_executor
        result.reason = GuardReason.low_margin
        events.append(
            _event(GuardReason.low_margin, f"margin {top_score - second_score} below threshold")
        )
    if result.executor == settings.routing.fallback_executor and result.executor not in candidates:
        result.executor = top1
        warning = f"fallback executor unavailable: {settings.routing.fallback_executor}"
        result.meta["warning"] = warning
        events.append(_event(GuardReason.unavailable, warning, settings.routing.fallback_executor))
    return result, events
