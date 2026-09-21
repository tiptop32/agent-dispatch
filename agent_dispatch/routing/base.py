from __future__ import annotations

from typing import Protocol

from agent_dispatch.models import DispatchRequest, RouteDecision


class RouterError(Exception):
    """A router could not produce a valid decision."""


class Router(Protocol):
    name: str

    async def decide(self, req: DispatchRequest, candidates: dict[str, str]) -> RouteDecision: ...
