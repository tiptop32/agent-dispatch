"""Deterministic capability routing without an LLM or network call."""

from __future__ import annotations

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import DispatchRequest, RouteDecision, RouterKind

from .base import RouterError
from .capability import perimeter_required, select


class StaticRouter:
    """Pick the requested capability (balanced by default) using local policy."""

    name = "static"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def decide(
        self, req: DispatchRequest, candidates: dict[str, ExecutorSettings]
    ) -> RouteDecision:
        if not candidates:
            raise RouterError("no candidates")
        verdict = perimeter_required(
            candidates,
            perimeter=self.settings.routing.corporate_perimeter,
            corporate=None,
            min_confidence=self.settings.routing.corporate_min_confidence,
            configured=self.settings.executors,
            prod=req.prod,
        )
        selection = select(
            req.capability or "balanced",
            candidates,
            max_capability=req.max_capability,
            corporate=verdict.required,
        )
        notes = list(selection.notes)
        if verdict.note and verdict.note not in notes:
            notes.append(verdict.note)
        meta: dict[str, object] = {}
        if selection.capability_uncapped is not None:
            meta["capability_uncapped"] = selection.capability_uncapped
        if notes:
            meta["selection_notes"] = notes
        if verdict.required:
            meta["perimeter"] = verdict.status
        return RouteDecision(
            router=RouterKind.static,
            executor=selection.executor,
            confidence=1.0,
            scores=selection.scores or {selection.executor: 1.0},
            capability=selection.capability,
            meta=meta,
        )
