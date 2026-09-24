from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
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
    FINAL_STATUSES,
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


@dataclass
class Routing:
    """Итог маршрутизации: кому отдать задачу и что по дороге сказали guard'ы.

    `guard` это причина, по которой решение принято не роутером: отказ (задачу
    надо провалить) либо явный выбор исполнителя пользователем.
    """

    decision: RouteDecision
    guard: GuardEvent | None = None
    events: list[GuardEvent] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


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
        # Lock на рабочую копию вместе со счётчиком тех, кто его держит или ждёт:
        # запись живёт ровно столько, сколько нужна.
        self._cwd_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        # Cleanup-таски из done-колбэка: shutdown их дожидается, иначе они
        # могут обратиться к уже закрытому storage.
        self._cleanup: set[asyncio.Task[None]] = set()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _notify(self, task_id: str) -> None:
        """Разбудить того, кто ждёт эту задачу в `wait`."""
        event = self._events.get(task_id)
        if event is not None:
            event.set()

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
        self._events[task_id] = asyncio.Event()
        self._tasks[task_id] = asyncio.create_task(self._run(task_id))
        self._tasks[task_id].add_done_callback(
            lambda worker, tid=task_id: self._track_cleanup(self._on_worker_done(tid, worker))
        )
        return record

    def _track_cleanup(self, coro: Coroutine[None, None, None]) -> None:
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
            if record.status not in FINAL_STATUSES and remaining > 0:
                event = self._events.get(record.task_id)
                if event is not None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(event.wait(), remaining)
                record = await self.get(record.task_id) or record
            if record.status not in FINAL_STATUSES:
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
        if record.status in FINAL_STATUSES:
            raise ValueError(f"task already finished: {task_id}")
        worker = self._tasks.get(task_id)
        if worker:
            worker.cancel()
        else:
            record.status = TaskStatus.cancelled
            record.finished_at = self._now()
            await self.storage.update_task(record)
            self._notify(task_id)
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
                    # Демон умер вместе со знанием о worktree задачи. Путь
                    # восстанавливается из события, иначе дерево осталось бы
                    # на диске, не упомянутое ни в одной записи.
                    meta=await self._worktree_meta(record.task_id),
                )
                await self.storage.add_event(record.task_id, "exit", {"status": "failed"})
                await self.storage.update_task(record)
                count += 1
        return count

    async def _worktree_meta(self, task_id: str) -> dict[str, object]:
        """Путь и ветка worktree задачи по её событиям; пусто в режиме in_place."""
        events = await self.storage.list_events(task_id)
        payload = next(
            (event["payload"] for event in reversed(events) if event["kind"] == "worktree"), None
        )
        if not payload or not payload.get("path"):
            return {}
        return {
            "worktree": payload["path"],
            "branch": payload.get("branch", ""),
            "integrated": False,
        }

    async def route_only(self, req: DispatchRequest) -> tuple[RouteDecision, list[GuardEvent]]:
        routing = await self._route(req)
        await self.storage.add_decision(None, routing.decision, routing.candidates)
        # Сработавший guard объясняет решение целиком: события роутеров к нему не относятся.
        return routing.decision, [routing.guard] if routing.guard else routing.events

    async def _route(self, req: DispatchRequest, *, is_escalation: bool = False) -> Routing:
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
        if isinstance(verdict, GuardEvent):
            return Routing(self._fallback_decision(verdict.reason), verdict, [], names)
        if isinstance(verdict, RouteDecision):
            guard = GuardEvent(
                reason=GuardReason.user_override,
                detail="user executor override",
                executor=verdict.executor,
            )
            return Routing(verdict, guard, [], names)
        if not names:
            guard = GuardEvent(reason=GuardReason.unavailable, detail="no available executors")
            return Routing(self._fallback_decision(guard.reason), guard, [], names)
        decision, events = await decide_with_fallback(
            req, {n: self.settings.executors[n] for n in names}, self.settings, self.routers
        )
        return Routing(decision, None, events, names)

    def _fallback_decision(self, reason: GuardReason) -> RouteDecision:
        return RouteDecision(
            executor=self.settings.routing.fallback_executor,
            confidence=0,
            scores={},
            router=RouterKind.fallback,
            reason=reason,
        )

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
        try:
            record = await self.storage.get_task(task_id)
            if record and record.status not in FINAL_STATUSES:
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
                    # Тот же перенос meta, что и в `_mark_failed`: путь
                    # оставшегося worktree переживает подмену результата.
                    meta=dict(record.result.meta) if record.result else {},
                )
                await self.storage.update_task(record)
                await self.storage.add_event(task_id, "exit", {"status": record.status.value})
        finally:
            self._notify(task_id)
            self._events.pop(task_id, None)

    async def _guard_failure(self, record: TaskRecord, guard: GuardEvent) -> None:
        result = ExecutionResult(
            status="failed",
            executor="",
            model=None,
            summary="",
            error=guard.detail,
            meta={"guard": guard.reason.value},
        )
        record.status, record.result, record.finished_at = TaskStatus.failed, result, self._now()
        await self.storage.add_event(record.task_id, "guard", guard.model_dump(mode="json"))
        await self.storage.update_task(record)
        self._notify(record.task_id)

    async def _run(self, task_id: str) -> None:
        record = await self.storage.get_task(task_id)
        if record is None:
            return
        try:
            prepared = await self._prepare(record)
            if prepared is None:
                return
            decision, adapter = prepared
            await self._execute(record, decision, adapter)
        except asyncio.CancelledError:
            await self._mark_cancelled(record)
            raise
        except Exception as exc:
            await self._mark_failed(record, exc)

    async def _prepare(self, record: TaskRecord) -> tuple[RouteDecision, ExecutorAdapter] | None:
        """Маршрутизация и guard'ы. None значит, что задача уже закрыта отказом."""
        record.status = TaskStatus.routing
        await self.storage.update_task(record)
        routing = await self._route(record.request, is_escalation=record.escalated_from is not None)
        if routing.guard and routing.guard.reason != GuardReason.user_override:
            await self._guard_failure(record, routing.guard)
            return None
        decision = routing.decision
        if record.escalated_from is not None:
            decision = decision.model_copy(
                update={"router": RouterKind.fallback, "reason": GuardReason.escalated}
            )
        record.decision = decision
        await self.storage.add_decision(record.task_id, decision, routing.candidates)
        for event in routing.events + ([routing.guard] if routing.guard else []):
            await self.storage.add_event(record.task_id, "guard", event.model_dump(mode="json"))
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
            return None
        return decision, adapter

    async def _execute(
        self, record: TaskRecord, decision: RouteDecision, adapter: ExecutorAdapter
    ) -> None:
        req, task_id = record.request, record.task_id
        mode = req.workspace_mode or self.settings.execution.workspace_mode
        semaphore = self._semaphore if req.hop == 0 else contextlib.nullcontext()
        # В режиме worktree исполнители не делят дерево, поэтому lock нужен
        # только на время переноса результата обратно в рабочую копию.
        lock = (
            self._cwd_lock(req.cwd)
            if req.hop == 0 and mode == "in_place"
            else contextlib.nullcontext()
        )
        async with semaphore, lock:
            tree = None
            # Путь и ветка известны до создания: отмена посреди `worktree.create`
            # бросает ожидание, но поток доводит каталог до конца, и корутина
            # никогда не узнает о дереве, которое уже есть на диске.
            target = self._worktree_target(task_id) if mode == "worktree" else None
            try:
                if target is not None:
                    tree = await asyncio.to_thread(self._create_worktree, req, task_id)
                    await self.storage.add_event(
                        task_id, "worktree", {"path": str(tree.path), "branch": tree.branch}
                    )
                ctx = await self._build_context(record, decision, tree)
                record.status, record.started_at = TaskStatus.running, self._now()
                await self.storage.update_task(record)
                await self.storage.add_event(
                    task_id,
                    "spawn",
                    {"executor": decision.executor, "hop": req.hop, "cwd": req.cwd},
                )
                try:
                    result = await adapter.execute(ctx)
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
                await self._finalize(record, decision, result)
            except BaseException as exc:
                # Отмена задачи это BaseException, её не ловит `except Exception`
                # выше, и обычный путь уборки не отрабатывает. Дерево остаётся
                # намеренно: в нём лежит незакоммиченная работа исполнителя.
                # Но без записи в результате его не видно ни в `status`, ни
                # вызывающему, поэтому путь и ветка уходят в meta.
                if target is not None:
                    await self._note_kept_worktree(record, *target, exc)
                raise

    async def _build_context(
        self, record: TaskRecord, decision: RouteDecision, tree: worktree.Worktree | None
    ) -> RunContext:
        """Task Package и prompt под адаптер выбранного исполнителя."""
        req = record.request
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
        return RunContext(
            cwd=str(tree.path) if tree else req.cwd,
            timeout_seconds=req.timeout_seconds or self.settings.routing.default_timeout_seconds,
            env=child_env(
                self.settings,
                {
                    "AGENT_DISPATCH_TASK_ID": record.task_id,
                    "AGENT_DISPATCH_ROOT_AGENT": record.root_agent.value,
                    "AGENT_DISPATCH_HOP": str(req.hop + 1),
                },
            ),
            log_path=Path(record.log_path),
            task_id=record.task_id,
            prompt=prompt,
        )

    async def _finalize(
        self, record: TaskRecord, decision: RouteDecision, result: ExecutionResult
    ) -> None:
        """Записать результат, при необходимости эскалировать, разбудить ожидающих."""
        record.result = result
        record.status, record.finished_at = TaskStatus(result.status), self._now()
        reason = should_escalate(result) if record.request.allow_escalation else None
        if reason is not None:
            await self._escalate(record, decision, result, reason)
        await self.storage.add_event(
            record.task_id,
            "exit",
            {"status": result.status, "changed_files": len(result.changed_files)},
        )
        await self.storage.update_task(record)
        self._notify(record.task_id)

    async def _escalate(
        self,
        record: TaskRecord,
        decision: RouteDecision,
        result: ExecutionResult,
        reason: str,
    ) -> None:
        """Отдать задачу следующему исполнителю цепочки, либо закрыть её отказом."""
        tried = await self._escalation_chain(record)
        nxt = next_executor(decision.executor, self.settings, tried)
        if nxt is None:
            result.meta["escalation_chain"] = tried
            if result.status != "failed":
                result.status = "failed"
                result.error = f"escalation exhausted: {reason}"
                record.status = TaskStatus.failed
            return
        await self.storage.add_event(
            record.task_id,
            "escalate",
            {"from": decision.executor, "to": nxt, "reason": reason},
        )
        child = await self.submit(
            record.request.model_copy(update={"executor": nxt}), escalated_from=record.task_id
        )
        result.meta["escalated_to"] = child.task_id

    async def _note_kept_worktree(
        self, record: TaskRecord, path: Path, branch: str, exc: BaseException
    ) -> None:
        """Записать в результат worktree, который остался после срыва задачи.

        Каталога может уже не быть: срыв бывает и после удачной интеграции,
        когда дерево убрано. Тогда путь не пишется, иначе в результате оказался
        бы указатель в пустоту.
        """
        # Проверка синхронная нарочно: задачу уже отменяют, и лишняя точка
        # ожидания здесь может снять запись результата вместе с путём.
        if not path.exists():  # noqa: ASYNC240
            return
        cancelled = isinstance(exc, asyncio.CancelledError)
        meta = dict(record.result.meta) if record.result else {}
        meta["worktree"], meta["branch"] = str(path), branch
        # Отмена может прийти и после удачной интеграции, когда дерево оставлено
        # по `keep_worktrees`. Готовый вердикт об интеграции не перебивается:
        # рабочая копия уже изменена, и `integrated: false` был бы ложью.
        meta.setdefault("integrated", False)
        record.result = ExecutionResult(
            status="failed",
            executor=record.decision.executor if record.decision else "",
            model=record.result.model if record.result else None,
            summary=record.result.summary if record.result else "",
            # `cancelled` нет в TerminalStatus: статус задачи живёт в record.status,
            # у результата остаётся только причина.
            error="cancelled" if cancelled else f"{type(exc).__name__}: {exc}",
            meta=meta,
        )
        await self.storage.update_task(record)

    async def _mark_cancelled(self, record: TaskRecord) -> None:
        if record.status in FINAL_STATUSES:
            return
        record.status, record.finished_at = TaskStatus.cancelled, self._now()
        await self.storage.add_event(record.task_id, "cancel", {})
        await self.storage.update_task(record)
        self._notify(record.task_id)

    async def _mark_failed(self, record: TaskRecord, exc: BaseException) -> None:
        # meta уже собранного результата переносится: в ней путь оставшегося
        # worktree, и терять его вместе с прежним результатом нельзя.
        record.result = ExecutionResult(
            status="failed",
            executor=record.decision.executor if record.decision else "",
            model=None,
            summary="",
            error=f"{type(exc).__name__}: {exc}",
            meta=dict(record.result.meta) if record.result else {},
        )
        record.status, record.finished_at = TaskStatus.failed, self._now()
        await self.storage.add_event(record.task_id, "exit", {"status": "failed"})
        await self.storage.update_task(record)
        self._notify(record.task_id)

    @contextlib.asynccontextmanager
    async def _cwd_lock(self, cwd: str) -> AsyncIterator[None]:
        """Lock на рабочую копию; запись снимается, когда её больше никто не ждёт."""
        key = str(Path(cwd).resolve())  # noqa: ASYNC240
        lock, waiters = self._cwd_locks.get(key, (asyncio.Lock(), 0))
        self._cwd_locks[key] = (lock, waiters + 1)
        try:
            async with lock:
                yield
        finally:
            lock, waiters = self._cwd_locks[key]
            if waiters <= 1:
                self._cwd_locks.pop(key, None)
            else:
                self._cwd_locks[key] = (lock, waiters - 1)

    def _worktree_target(self, task_id: str) -> tuple[Path, str]:
        """Каталог и ветка будущего дерева: они известны ещё до его создания."""
        base = self.settings.execution.worktree_dir or (self.settings.server.data_dir / "worktrees")
        return (
            Path(base).expanduser() / task_id,
            f"{self.settings.execution.branch_prefix}/{task_id[:8]}",
        )

    def _create_worktree(self, req: DispatchRequest, task_id: str) -> worktree.Worktree:
        path, branch = self._worktree_target(task_id)
        return worktree.create(req.cwd, path, branch)

    async def _finish_worktree(
        self,
        req: DispatchRequest,
        tree: worktree.Worktree,
        result: ExecutionResult,
        task_id: str,
    ) -> None:
        """Закоммитить результат на ветке worktree и перенести его в рабочую копию.

        Промежуточный коммит делает работу исполнителя долговечной: она переживает
        и неудачную интеграцию, и уборку каталога. Патч применяется под тем же lock,
        что и обычный запуск in_place, иначе две параллельные интеграции наложатся
        друг на друга. Если патч не лёг, worktree и ветка остаются: их видно в
        `agent-dispatch worktrees`.
        """
        result.meta["worktree"] = str(tree.path)
        result.meta["branch"] = tree.branch
        execution = self.settings.execution
        try:
            sha = await asyncio.to_thread(
                worktree.commit, tree, f"agent-dispatch: task {task_id[:8]}"
            )
        except worktree.WorktreeError as exc:
            # Работа цела в worktree, поэтому дерево остаётся вместе с ней.
            result.meta["integrated"] = False
            result.meta["integration_error"] = str(exc)
            return
        if sha is None:
            result.meta["integrated"] = True
            if not execution.keep_worktrees:
                await self._drop_worktree(tree, keep_branch=False, result=result)
            return
        result.meta["commit"] = sha
        try:
            patch = await asyncio.to_thread(worktree.build_patch, tree)
        except worktree.WorktreeError as exc:
            result.meta["integrated"] = False
            result.meta["integration_error"] = str(exc)
            return
        patch_path = self.settings.server.data_dir / "patches" / f"{task_id}.patch"
        try:
            await asyncio.to_thread(patch_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(patch_path.write_text, patch)
        except OSError as exc:
            # Патч не лёг на диск: применять нечего, но работа цела на ветке,
            # и дерево остаётся вместе с ней.
            result.meta["integrated"] = False
            result.meta["integration_error"] = f"patch not written: {exc}"
            return
        result.meta["patch"] = str(patch_path)
        if execution.integrate == "branch":
            # Ветка с коммитом и есть результат: рабочую копию не трогаем,
            # каталог можно убрать, работа останется на ветке.
            result.meta["integrated"] = False
            if not execution.keep_worktrees:
                await self._drop_worktree(tree, keep_branch=True, result=result)
            return
        if execution.integrate != "apply":
            result.meta["integrated"] = False
            return
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
