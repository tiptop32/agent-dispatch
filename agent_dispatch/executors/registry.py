from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from agent_dispatch.config import Settings
from agent_dispatch.executors.base import ExecutorAdapter
from agent_dispatch.executors.claude import ClaudeAdapter
from agent_dispatch.executors.codex import CodexAdapter
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.opencode import OpenCodeAdapter
from agent_dispatch.models import Availability


def build_adapters(settings: Settings) -> dict[str, ExecutorAdapter]:
    classes = {"claude": ClaudeAdapter, "codex": CodexAdapter, "opencode": OpenCodeAdapter}
    return {
        name: classes[item.adapter](name, item, child_env(settings))
        for name, item in settings.executors.items()
        if item.enabled
    }


class AvailabilityCache:
    def __init__(
        self,
        adapters: dict[str, ExecutorAdapter],
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.adapters = adapters
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._values: dict[str, Availability] = {}
        self._checked_at: float | None = None

    async def check_all(self, force: bool = False) -> dict[str, Availability]:
        now = self.clock()
        if not force and self._checked_at is not None and now - self._checked_at < self.ttl_seconds:
            return dict(self._values)
        values = await asyncio.gather(*(adapter.check() for adapter in self.adapters.values()))
        self._values = dict(zip(self.adapters, values, strict=True))
        self._checked_at = now
        return dict(self._values)

    def get(self, name: str) -> Availability | None:
        return self._values.get(name)

    def unavailable(self) -> set[str]:
        return {name for name, value in self._values.items() if not value.available}
