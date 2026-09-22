from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agent_dispatch.config import Settings
from agent_dispatch.dispatch.escalation import next_executor, should_escalate
from agent_dispatch.dispatch.task_package import build_task_package, render_prompt
from agent_dispatch.executors import worktree
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

    async def submit(self, req: DispatchRequest, escalated_from: str | None = None) -> TaskRecord:
        task_id = uuid.uuid4().hex
        root = req.root_agent or req.source_agent
        log_path = self.settings.server.data_dir / "logs" / f"{task_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = TaskRecord(
            task_id=task_id,
            parent_task_id=req.parent_task_id,
            escalated_from=escalated_from,
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
        deadline = time.monotonic() + max(0.0, seconds)
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if seconds <= 0:
            # Нулевое ожидание: вернуть саму задачу, не следуя за эскалацией.
            return record
        visited = {record.task_id}
        while True:
            remaining = deadline - time.monotonic()
            if record.status not in _FINAL and remaining > 0:
                event = self._events.get(record.task_id)
                if event is not None:
                    try:
                        await asyncio.wait_for(event.wait(), remaining)
                    except TimeoutError:
                        pass
                record = await self.get(record.task_id) or record
            if record.status not in _FINAL:
                return record
            child_id = record.result.meta.get("escalated_to") if record.result else None
            if not child_id:
                return record
            child = await self.get(child_id)
            if child is None or child.task_id in visited:
                return record
            visited.add(child.task_id)
            record = child

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
        self, req: DispatchRequest, *, is_escalation: bool = False
    ) -> tuple[RouteDecision, GuardEvent | None, list[GuardEvent], list[str]]:
        await self.availability.check_all()
        parent_exists = (
            await self.storage.task_exists(req.parent_task_id) if req.parent_task_id else False
        )
        siblings = 0
        if req.parent_task_id and not is_escalation:
            # Ретраи эскалации не съедают квоту fan-out родителя, cancelled тоже.
            siblings = await self.storage.count_children(req.parent_task_id, exclude_escalated=True)
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
                {n: self.settings.executors[n] for n in names},
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
            decision, guard, events, names = await self._decide(
                req, is_escalation=record.escalated_from is not None
            )
            if guard and guard.reason != GuardReason.user_override:
                await self._guard_failure(record, guard)
                return
            if guard is not None:
                events.append(guard)
            if record.escalated_from is not None:
                decision = decision.model_copy(
                    update={"router": RouterKind.fallback, "reason": GuardReason.escalated}
                )
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
            mode = req.workspace_mode or self.settings.execution.workspace_mode
            lock_cm = _NullAsyncContext()
            sem_cm = _NullAsyncContext()
            if req.hop == 0:
                sem_cm = self._semaphore
            # В режиме worktree исполнители не делят дерево, поэтому lock нужен
            # только на время переноса результата обратно в рабочую копию.
            if req.hop == 0 and mode == "in_place":
                lock_cm = self._cwd_lock(req.cwd)
            async with sem_cm:
                async with lock_cm:
                    tree = None
                    if mode == "worktree":
                        tree = await asyncio.to_thread(self._create_worktree, req, task_id)
                        await self.storage.add_event(
                            task_id,
                            "worktree",
                            {"path": str(tree.path), "branch": tree.branch},
                        )
                    work_dir = str(tree.path) if tree else req.cwd
                    package = await asyncio.to_thread(
                        build_task_package,
                        req,
                        self.settings,
                        str(tree.path) if tree else None,
                        tree.branch if tree else None,
                    )
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
                        cwd=work_dir,
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
                    if tree is not None:
                        await self._finish_worktree(req, tree, result, task_id)
                    record.result = result
                    record.status, record.finished_at = TaskStatus(result.status), self._now()
                    reason = should_escalate(result) if req.allow_escalation else None
                    if reason is not None:
                        tried = await self._escalation_chain(record)
                        nxt = next_executor(decision.executor, self.settings, tried)
                        if nxt is None:
                            result.meta["escalation_chain"] = tried
                            if result.status != "failed":
                                result.status = "failed"
                                result.error = f"escalation exhausted: {reason}"
                                record.status = TaskStatus.failed
                        else:
                            await self.storage.add_event(
                                task_id,
                                "escalate",
                                {"from": decision.executor, "to": nxt, "reason": reason},
                            )
                            child = await self.submit(
                                req.model_copy(update={"executor": nxt}),
                                escalated_from=task_id,
                            )
                            result.meta["escalated_to"] = child.task_id
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

    def _cwd_lock(self, cwd: str) -> asyncio.Lock:
        key = str(Path(cwd).resolve())
        return self._cwd_locks.setdefault(key, asyncio.Lock())

    def _create_worktree(self, req: DispatchRequest, task_id: str) -> worktree.Worktree:
        base = self.settings.execution.worktree_dir or (self.settings.server.data_dir / "worktrees")
        branch = f"{self.settings.execution.branch_prefix}/{task_id[:8]}"
        return worktree.create(req.cwd, Path(base).expanduser() / task_id, branch)

    async def _finish_worktree(
        self,
        req: DispatchRequest,
        tree: worktree.Worktree,
        result: ExecutionResult,
        task_id: str,
    ) -> None:
        """Перенести результат из worktree в рабочую копию и прибрать за собой.

        Патч применяется под тем же lock, что и обычный запуск in_place, иначе две
        параллельные интеграции наложатся друг на друга. Если патч не лёг, worktree
        и ветка остаются: их видно в `agent-dispatch worktrees`.
        """
        result.meta["worktree"] = str(tree.path)
        result.meta["branch"] = tree.branch
        execution = self.settings.execution
        try:
            patch = await asyncio.to_thread(worktree.build_patch, tree)
        except worktree.WorktreeError as exc:
            result.meta["integrated"] = False
            result.meta["integration_error"] = str(exc)
            return
        if not patch.strip():
            result.meta["integrated"] = True
            if not execution.keep_worktrees:
                await self._drop_worktree(tree, keep_branch=False, result=result)
            return
        if execution.integrate != "apply":
            result.meta["integrated"] = False
            return
        patch_path = self.settings.server.data_dir / "patches" / f"{task_id}.patch"
        await asyncio.to_thread(patch_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(patch_path.write_text, patch)
        result.meta["patch"] = str(patch_path)
        try:
            async with self._cwd_lock(req.cwd):
                await asyncio.to_thread(worktree.apply_patch, req.cwd, patch_path)
        except worktree.WorktreeError as exc:
            result.meta["integrated"] = False
            result.meta["integration_error"] = str(exc)
            await self.storage.add_event(
                task_id, "integrate", {"ok": False, "error": str(exc)[:500]}
            )
            return
        result.meta["integrated"] = True
        await self.storage.add_event(
            task_id, "integrate", {"ok": True, "files": len(result.changed_files)}
        )
        if not execution.keep_worktrees:
            await self._drop_worktree(tree, keep_branch=False, result=result)

    async def _drop_worktree(
        self, tree: worktree.Worktree, *, keep_branch: bool, result: ExecutionResult
    ) -> None:
        try:
            await asyncio.to_thread(worktree.remove, tree, keep_branch=keep_branch)
        except worktree.WorktreeError as exc:
            result.meta["worktree_cleanup_error"] = str(exc)
            return
        result.meta.pop("worktree", None)
        if not keep_branch:
            result.meta.pop("branch", None)

    async def _escalation_chain(self, record: TaskRecord) -> list[str]:
        chain: list[str] = []
        current: TaskRecord | None = record
        seen = {record.task_id}
        for _ in range(10):
            if current.decision is not None:
                chain.append(current.decision.executor)
            previous_id = current.escalated_from
            if previous_id is None or previous_id in seen:
                break
            seen.add(previous_id)
            current = await self.storage.get_task(previous_id)
            if current is None:
                break
        chain.reverse()
        return chain

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
