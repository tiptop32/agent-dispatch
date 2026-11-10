"""CLI-only executor capacity, including leases shared by nested tasks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agent_dispatch.config import Settings
from agent_dispatch.telemetry.storage import Storage


@dataclass
class _Lease:
    executor: str
    semaphore: asyncio.Semaphore
    users: int = 0


class ExecutorConcurrency:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._active: dict[str, _Lease] = {}
        # Waiting child -> (active ancestor task IDs, requested executor).
        self._waiting: dict[str, tuple[set[str], str]] = {}

    async def _ancestors(self, task_id: str) -> list[tuple[str, _Lease]]:
        record = await self.storage.get_task(task_id)
        if record is None or record.hop == 0:
            return []
        parent_id = record.parent_task_id
        seen = {task_id}
        ancestors = []
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            lease = self._active.get(parent_id)
            if lease is not None:
                ancestors.append((parent_id, lease))
            parent = await self.storage.get_task(parent_id)
            parent_id = parent.parent_task_id if parent else None
        return ancestors

    def _would_cycle(self, ancestors: set[str], executor: str) -> bool:
        # Edges are from each active ancestor to the current holders of the
        # executor its descendant is waiting for. Rebuild from live leases so
        # a finished holder cannot leave a stale dependency behind.
        edges: dict[str, set[str]] = {}
        for sources, target in self._waiting.values():
            holders = {task for task, lease in self._active.items() if lease.executor == target}
            for source in sources:
                edges.setdefault(source, set()).update(holders)
        pending = [task for task, lease in self._active.items() if lease.executor == executor]
        seen: set[str] = set()
        while pending:
            task = pending.pop()
            if task in ancestors:
                return True
            if task not in seen:
                seen.add(task)
                pending.extend(edges.get(task, ()))
        return False

    def _semaphore(self, executor: str) -> asyncio.Semaphore | None:
        limit = self.settings.executors[executor].max_concurrent
        if limit is None:
            return None
        semaphore = self._semaphores.get(executor)
        if semaphore is None:
            semaphore = self._semaphores[executor] = asyncio.Semaphore(limit)
        return semaphore

    def busy(self, executor: str) -> bool:
        semaphore = self._semaphore(executor)
        return semaphore is not None and semaphore.locked()

    @asynccontextmanager
    async def slot(self, task_id: str, executor: str) -> AsyncIterator[None]:
        started = time.monotonic()
        semaphore = self._semaphore(executor)
        inherited_from = None
        lease = None
        if semaphore is not None:
            ancestors = await self._ancestors(task_id)
            record = await self.storage.get_task(task_id)
            ancestor = next(
                ((task, held) for task, held in ancestors if held.executor == executor), None
            )
            if ancestor is not None and record is not None and record.request.kind != "review":
                inherited_from, lease = ancestor
            else:
                if ancestor is not None and semaphore.locked():
                    raise RuntimeError(f"executor capacity deadlock: {executor}")
                sources = {task for task, held in ancestors if held.executor != executor}
                if semaphore.locked() and sources:
                    if self._would_cycle(sources, executor):
                        raise RuntimeError(f"executor capacity deadlock: {executor}")
                    self._waiting[task_id] = (sources, executor)
                try:
                    await semaphore.acquire()
                finally:
                    self._waiting.pop(task_id, None)
                lease = _Lease(executor, semaphore)
            lease.users += 1
            self._active[task_id] = lease
        try:
            await self.storage.add_event(
                task_id,
                "slot",
                {
                    "executor": executor,
                    "limit_group": self.settings.limit_group(executor),
                    "executor_wait_seconds": round(time.monotonic() - started, 6),
                    "inherited_from": inherited_from,
                },
            )
            yield
        finally:
            if lease is not None:
                self._active.pop(task_id, None)
                lease.users -= 1
                # A child may outlive its parent's CLI: retain their shared slot
                # until the last running member of that lease has stopped.
                if lease.users == 0:
                    lease.semaphore.release()
