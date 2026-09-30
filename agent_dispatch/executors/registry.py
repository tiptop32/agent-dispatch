from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


@dataclass(frozen=True)
class Cooldown:
    """Исполнитель выведен из ротации после сбоя, который повторится на любой задаче."""

    kind: str
    detail: str
    until: float
    until_utc: datetime


class AvailabilityCache:
    """Доступность исполнителей: проверка CLI с TTL плюс остывание после сбоев.

    Остывание живёт отдельно от результатов `check`: CLI установлен и отвечает
    на `--version`, но лимит расходов исчерпан, и повторная проверка этого не
    увидит. Поэтому `check_all` остывание не снимает, его снимают только часы.
    """

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
        self._cooldowns: dict[str, Cooldown] = {}

    async def check_all(self, force: bool = False) -> dict[str, Availability]:
        now = self.clock()
        if not force and self._checked_at is not None and now - self._checked_at < self.ttl_seconds:
            return dict(self._values)
        values = await asyncio.gather(*(adapter.check() for adapter in self.adapters.values()))
        self._values = dict(zip(self.adapters, values, strict=True))
        self._checked_at = now
        return dict(self._values)

    def get(self, name: str) -> Availability | None:
        cooldown = self.cooldown(name)
        value = self._values.get(name)
        if cooldown is None:
            return value
        error = f"cooldown until {cooldown.until_utc.isoformat()}: {cooldown.kind}"
        return Availability(
            available=False,
            version=value.version if value else None,
            error=error,
            checked_at=value.checked_at if value else cooldown.until_utc,
        )

    def unavailable(self) -> set[str]:
        down = {name for name, value in self._values.items() if not value.available}
        return down | set(self.cooldowns())

    def trip(self, name: str, kind: str, detail: str, seconds: float) -> Cooldown | None:
        """Вывести исполнителя из ротации на `seconds`; 0 выключает механизм."""
        if seconds <= 0:
            return None
        cooldown = Cooldown(
            kind=kind,
            detail=detail[:300],
            until=self.clock() + seconds,
            until_utc=datetime.now(UTC) + timedelta(seconds=seconds),
        )
        self._cooldowns[name] = cooldown
        return cooldown

    def cooldown(self, name: str) -> Cooldown | None:
        cooldown = self._cooldowns.get(name)
        if cooldown is not None and self.clock() >= cooldown.until:
            del self._cooldowns[name]
            return None
        return cooldown

    def cooldowns(self) -> dict[str, Cooldown]:
        """Действующие остывания; истёкшие снимаются по дороге."""
        return {
            name: cooldown
            for name in list(self._cooldowns)
            if (cooldown := self.cooldown(name)) is not None
        }
