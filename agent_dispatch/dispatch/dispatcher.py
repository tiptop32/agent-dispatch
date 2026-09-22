from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agent_dispatch.config import Settings
from agent_dispatch.dispatch.task_package import build_task_package, render_prompt
from agent_dispatch.executors.base import ExecutorAdapter, RunContext
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
    TaskRecord,
    TaskStatus,
)
from agent_dispatch.routing.base import Router
from agent_dispatch.routing.decision import decide_with_fallback
from agent_dispatch.routing.guards import candidates, pre_guards
from agent_dispatch.telemetry.storage import Storage

_FINAL = {
    TaskStatus.completed,
    TaskStatus.partial,
    TaskStatus.failed,
    TaskStatus.needs_context,
    TaskStatus.needs_escalation,
    TaskStatus.cancelled,
}


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        adapters: dict[str, ExecutorAdapter],
        availability: AvailabilityCache,
        routers: list[Router],
    ):
        self.settings, self.storage = settings, storage
        self.adapters, self.availability, self.routers = adapters, availability, routers
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._semaphore = asyncio.Semaphore(settings.server.max_concurrent_tasks)
        self._cwd_locks: dict[str, asyncio.Lock] = {}
        # Cleanup-таски из done-колбэка: shutdown их дожидается, иначе они
        # могут обратиться к уже закрытому storage.
        self._cleanup: set[asyncio.Task[None]] = set()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def submit(self, req: DispatchRequest) -> TaskRecord:
        task_id = uuid.uuid4().hex
        root = req.root_agent or req.source_agent
        log_path = self.settings.server.data_dir / "logs" / f"{task_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = TaskRecord(
            task_id=task_id,
            parent_task_id=req.parent_task_id,
            escalated_from=None,
            root_agent=root,
            source_agent=req.source_agent,
            hop=req.hop,
            request=req,
            status=TaskStatus.queued,
            decision=None,
            result=None,
            log_path=str(log_path),
            created_at=self._now(),
            started_at=None,
            finished_at=None,
        )
        await self.storage.insert_task(record)
        event = asyncio.Event()
        self._events[task_id] = event
        self._tasks[task_id] = asyncio.create_task(self._run(task_id))
        self._tasks[task_id].add_done_callback(
            lambda worker, tid=task_id: self._track_cleanup(self._on_worker_done(tid, worker))
        )
        return record

    def _track_cleanup(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)

    async def get(self, task_id: str) -> TaskRecord | None:
        return await self.storage.get_task(task_id)

    async def wait(self, task_id: str, seconds: float) -> TaskRecord:
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.status not in _FINAL and seconds > 0:
            event = self._events.get(task_id)
            if event is None:
                return record
            try:
                await asyncio.wait_for(event.wait(), seconds)
            except TimeoutError:
                pass
        return await self.get(task_id)  # type: ignore[return-value]

    async def cancel(self, task_id: str, wait_seconds: float = 5.0) -> TaskRecord:
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.status in _FINAL:
            raise ValueError(f"task already finished: {task_id}")
        worker = self._tasks.get(task_id)
        if worker:
            worker.cancel()
        else:
            record.status = TaskStatus.cancelled
            record.finished_at = self._now()
            await self.storage.update_task(record)
            event = self._events.get(task_id)
            if event:
                event.set()
        return await self.wait(task_id, wait_seconds)

    async def recover_stale(self) -> int:
        count = 0
        for record in await self.storage.list_tasks(limit=2**31 - 1):
            if (
                record.status in {TaskStatus.queued, TaskStatus.routing, TaskStatus.running}
                and record.task_id not in self._tasks
            ):
                record.status = TaskStatus.failed
                record.finished_at = self._now()
                record.result = ExecutionResult(
                    status="failed",
                    executor=record.decision.executor if record.decision else "",
                    model=None,
                    summary="",
                    error="daemon restarted",
                )
                await self.storage.add_event(record.task_id, "exit", {"status": "failed"})
                await self.storage.update_task(record)
                count += 1
        return count

    async def route_only(self, req: DispatchRequest) -> tuple[RouteDecision, list[GuardEvent]]:
        decision, event, events, names = await self._decide(req)
        if event:
            events = [event]
        await self.storage.add_decision(None, decision, names)
        return decision, events

    async def _decide(
        self, req: DispatchRequest
    ) -> tuple[RouteDecision, GuardEvent | None, list[GuardEvent], list[str]]:
        await self.availability.check_all()
        parent_exists = (
            await self.storage.task_exists(req.parent_task_id) if req.parent_task_id else False
        )
        siblings = (
            await self.storage.count_children(req.parent_task_id) if req.parent_task_id else 0
        )
        verdict = pre_guards(
            req, self.settings, self.availability.unavailable(), parent_exists, siblings
        )
        names = candidates(
            self.settings, self.availability.unavailable(), req.source_agent, req.hop
        )
        events: list[GuardEvent] = []
        guard: GuardEvent | None = None
        if isinstance(verdict, GuardEvent):
            guard = verdict
            decision = RouteDecision(
                executor=self.settings.routing.fallback_executor,
                confidence=0,
                scores={},
                router=RouterKind.fallback,
                reason=verdict.reason,
            )
        elif isinstance(verdict, RouteDecision):
            decision = verdict
            guard = GuardEvent(
                reason=GuardReason.user_override,
                detail="user executor override",
                executor=decision.executor,
            )
        elif not names:
            event = GuardEvent(reason=GuardReason.unavailable, detail="no available executors")
            decision = RouteDecision(
                executor=self.settings.routing.fallback_executor,
                confidence=0,
                scores={},
                router=RouterKind.fallback,
                reason=event.reason,
            )
            guard = event
        else:
            decision, events = await decide_with_fallback(
                req,
                {n: self.settings.executors[n].description for n in names},
                self.settings,
                self.routers,
            )
        return decision, guard, events, names

    async def shutdown(self) -> None:
        workers = list(self._tasks.values())
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        # Колбэки уже поставлены в очередь loop; дать им создать cleanup-таски.
        await asyncio.sleep(0)
        if self._cleanup:
            await asyncio.gather(*self._cleanup, return_exceptions=True)

    async def _on_worker_done(self, task_id: str, worker: asyncio.Task[None]) -> None:
        self._tasks.pop(task_id, None)
        event = self._events.get(task_id)
        try:
            record = await self.storage.get_task(task_id)
            if record and record.status not in _FINAL:
                record.status = TaskStatus.cancelled if worker.cancelled() else TaskStatus.failed
                record.finished_at = self._now()
                record.result = ExecutionResult(
                    status=record.status.value,
                    executor=record.decision.executor if record.decision else "",
                    model=None,
                    summary="",
                    error="worker stopped unexpectedly"
                    if record.status == TaskStatus.failed
                    else None,
                )
                await self.storage.update_task(record)
                await self.storage.add_event(task_id, "exit", {"status": record.status.value})
        finally:
            if event:
                event.set()
            self._events.pop(task_id, None)

    async def _guard_failure(self, record: TaskRecord, event: GuardEvent) -> None:
        result = ExecutionResult(
            status="failed",
            executor="",
            model=None,
            summary="",
            error=event.detail,
            meta={"guard": event.reason.value},
        )
        record.status, record.result, record.finished_at = TaskStatus.failed, result, self._now()
        await self.storage.add_event(record.task_id, "guard", event.model_dump(mode="json"))
        await self.storage.update_task(record)
        event = self._events.get(record.task_id)
        if event:
            event.set()

    async def _run(self, task_id: str) -> None:
        record = await self.storage.get_task(task_id)
        if record is None:
            return
        try:
            record.status = TaskStatus.routing
            await self.storage.update_task(record)
            req = record.request
            decision, guard, events, names = await self._decide(req)
            if guard and guard.reason != GuardReason.user_override:
                await self._guard_failure(record, guard)
                return
            if guard is not None:
                events.append(guard)
            record.decision = decision
            await self.storage.add_decision(task_id, decision, names)
            for event in events:
                await self.storage.add_event(task_id, "guard", event.model_dump(mode="json"))
            adapter = self.adapters.get(decision.executor)
            if adapter is None:
                await self._guard_failure(
                    record,
                    GuardEvent(
                        reason=GuardReason.unavailable,
                        detail=f"executor unavailable: {decision.executor}",
                        executor=decision.executor,
                    ),
                )
                return
            lock_cm = _NullAsyncContext()
            sem_cm = _NullAsyncContext()
            if req.hop == 0:
                sem_cm = self._semaphore
                key = str(Path(req.cwd).resolve())  # noqa: ASYNC240
                lock_cm = self._cwd_locks.setdefault(key, asyncio.Lock())
            async with sem_cm:
                async with lock_cm:
                    package = await asyncio.to_thread(build_task_package, req, self.settings)
                    prompt = await asyncio.to_thread(
                        render_prompt,
                        package,
                        self.settings.executors[decision.executor].adapter,
                        self.settings,
                    )
                    record.status, record.started_at = TaskStatus.running, self._now()
                    await self.storage.update_task(record)
                    await self.storage.add_event(
                        task_id,
                        "spawn",
                        {"executor": decision.executor, "hop": req.hop, "cwd": req.cwd},
                    )
                    ctx = RunContext(
                        cwd=req.cwd,
                        timeout_seconds=req.timeout_seconds
                        or self.settings.routing.default_timeout_seconds,
                        env=child_env(
                            self.settings,
                            {
                                "AGENT_DISPATCH_TASK_ID": task_id,
                                "AGENT_DISPATCH_ROOT_AGENT": record.root_agent.value,
                                "AGENT_DISPATCH_HOP": str(req.hop + 1),
                            },
                        ),
                        log_path=Path(record.log_path),
                        task_id=task_id,
                        prompt=prompt,
                    )
                    try:
                        result = await adapter.execute(ctx)
                    except asyncio.CancelledError:
                        record.status, record.finished_at = TaskStatus.cancelled, self._now()
                        await self.storage.add_event(task_id, "cancel", {})
                        await self.storage.update_task(record)
                        event = self._events.get(task_id)
                        if event:
                            event.set()
                        raise
                    except Exception as exc:
                        result = ExecutionResult(
                            status="failed",
                            executor=decision.executor,
                            model=None,
                            summary="",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    record.result = result
                    record.status, record.finished_at = TaskStatus(result.status), self._now()
                    await self.storage.add_event(
                        task_id,
                        "exit",
                        {"status": result.status, "changed_files": len(result.changed_files)},
                    )
                    await self.storage.update_task(record)
                    event = self._events.get(task_id)
                    if event:
                        event.set()
        except asyncio.CancelledError:
            if record.status not in _FINAL:
                record.status, record.finished_at = TaskStatus.cancelled, self._now()
                await self.storage.add_event(task_id, "cancel", {})
                await self.storage.update_task(record)
                event = self._events.get(task_id)
                if event:
                    event.set()
            raise
        except Exception as exc:
            result = ExecutionResult(
                status="failed",
                executor=record.decision.executor if record.decision else "",
                model=None,
                summary="",
                error=f"{type(exc).__name__}: {exc}",
            )
            record.result, record.status, record.finished_at = (
                result,
                TaskStatus.failed,
                self._now(),
            )
            await self.storage.add_event(task_id, "exit", {"status": "failed"})
            await self.storage.update_task(record)
            event = self._events.get(task_id)
            if event:
                event.set()
        finally:
            self._release_cwd_lock(record.request)

    def _release_cwd_lock(self, req: DispatchRequest) -> None:
        """Убрать lock cwd из словаря, если он свободен и его никто не ждёт."""
        if req.hop != 0:
            return
        key = str(Path(req.cwd).resolve())  # noqa: ASYNC240
        lock = self._cwd_locks.get(key)
        if lock and not lock.locked() and not lock._waiters:  # type: ignore[attr-defined]
            self._cwd_locks.pop(key, None)


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False
