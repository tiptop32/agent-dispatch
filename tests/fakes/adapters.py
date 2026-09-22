from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from agent_dispatch.models import Availability, ExecutionResult, RouteDecision
from agent_dispatch.routing.base import RouterError


class FakeAdapter:
    def __init__(
        self,
        name: str,
        result: ExecutionResult | None = None,
        gate: asyncio.Event | None = None,
        on_execute: Callable[[object], Awaitable[None]] | None = None,
        raise_exc: Exception | None = None,
    ):
        self.name, self.result, self.gate, self.on_execute, self.raise_exc = (
            name,
            result,
            gate,
            on_execute,
            raise_exc,
        )
        self.calls: list[object] = []
        self.cancelled = False
        self.started = asyncio.Event()

    async def execute(self, ctx):
        self.calls.append(ctx)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.on_execute is not None:
                await self.on_execute(ctx)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.raise_exc:
            raise self.raise_exc
        return self.result or ExecutionResult(
            status="completed", executor=self.name, model=None, summary="ok"
        )

    async def check(self):
        return Availability(available=True, version="fake", checked_at=datetime.now(UTC))


class FakeRouter:
    name = "fake"

    def __init__(self, decision: RouteDecision | None = None, error: Exception | None = None):
        self.decision, self.error, self.calls = decision, error, 0

    async def decide(self, req, candidates):
        self.calls += 1
        if self.error:
            raise (
                self.error if isinstance(self.error, RouterError) else RouterError(str(self.error))
            )
        if self.decision is None:
            raise RouterError("no decision")
        return self.decision
