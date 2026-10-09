from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_dispatch.config import Settings
from agent_dispatch.dispatch import followup, verify
from agent_dispatch.dispatch.escalation import next_executor, should_escalate
from agent_dispatch.dispatch.task_package import (
    Followup,
    build_task_package,
    render_prompt,
    rewrite_repo_prefix,
)
from agent_dispatch.executors import workspace, worktree
from agent_dispatch.executors.base import ExecutorAdapter, RunContext
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.health import classify_failure, cooldown_seconds, failure_scope
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    FINAL_STATUSES,
    DispatchRequest,
    ExecutionResult,
    FollowupRequest,
    GuardEvent,
    GuardReason,
    RouteDecision,
    RouterKind,
    TaskRecord,
    TaskStatus,
)
from agent_dispatch.routing.base import Router
from agent_dispatch.routing.decision import decide_with_fallback
from agent_dispatch.routing.guards import candidates, pre_guards, review_only_executors
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


@dataclass
class EscalationPlan:
    """Что делать с результатом, который не закрывает задачу.

    `executor` это следующее звено цепочки. Пустой `executor` при `reroute`
    отдаёт задачу роутеру заново: исполнитель упал на сбое, который к задаче
    не относится (лимит, сеть), и цепочка за ним кончилась. Пустой `executor`
    без `reroute` значит, что эскалация исчерпана.
    """

    reason: str
    tried: list[str]
    executor: str | None = None
    reroute: bool = False

    @property
    def hands_off(self) -> bool:
        return self.executor is not None or self.reroute


def _failed_result(
    record: TaskRecord, error: str | None, meta: dict[str, object] | None = None
) -> ExecutionResult:
    """Отказ на месте прежнего результата задачи.

    meta прежнего результата переносится, если не задана явно: в ней путь
    оставшегося worktree, и терять его вместе с результатом нельзя. Статус
    всегда `failed`: `cancelled` нет в TerminalStatus, статус задачи живёт в
    `record.status`, у результата остаётся только причина.
    """
    previous = record.result
    return ExecutionResult(
        status="failed",
        executor=record.decision.executor if record.decision else "",
        model=previous.model if previous else None,
        summary=previous.summary if previous else "",
        error=error,
        meta=dict(previous.meta if previous else {}) if meta is None else meta,
    )


def _not_integrated(result: ExecutionResult, error: str) -> None:
    result.meta["integrated"] = False
    result.meta["integration_error"] = error


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
        # Ёмкость ревью отдельная: долгая корневая задача не должна оставлять
        # ревью без слота, и лишние ревью не должны вытеснять задачи. Максимум
        # корневых CLI-процессов демона из-за этого равен сумме двух пределов.
        self._review_semaphore = asyncio.Semaphore(settings.server.max_concurrent_reviews)
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
        if req.parent_task_id is None:
            req = await self._inherit_worktree_parent(req)
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

    async def _inherit_worktree_parent(self, req: DispatchRequest) -> DispatchRequest:
        """Восстановить родителя вложенного вызова по cwd его worktree."""
        cwd = Path(req.cwd).expanduser().resolve()  # noqa: ASYNC240
        base = self.settings.execution.worktree_dir or (self.settings.server.data_dir / "worktrees")
        base = Path(base).expanduser().resolve()  # noqa: ASYNC240
        try:
            relative = cwd.relative_to(base)
        except ValueError:
            return req
        if not relative.parts:
            return req
        parent = await self.storage.get_task(relative.parts[0])
        if parent is None:
            return req
        parent_path, _ = self._worktree_target(parent.task_id)
        try:
            cwd.relative_to(parent_path.expanduser().resolve())
        except ValueError:
            return req
        return req.model_copy(
            update={
                "parent_task_id": parent.task_id,
                "hop": parent.hop + 1,
                "root_agent": parent.root_agent,
            }
        )

    async def followup(self, task_id: str, body: FollowupRequest) -> TaskRecord:
        """Продолжить готовую задачу тем же исполнителем (`dispatch/followup.py`)."""
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        link = followup.continued_link(await self._chain_links(record))
        executor: str | None = link.decision.executor if link.decision else None
        if executor in self.availability.unavailable():
            # Исполнитель остывает: роутер выберет другого, итог прошлой
            # попытки уйдёт ему в промпт.
            executor = None
        return await self.submit(followup.followup_request(link, body, executor))

    async def _chain_links(self, record: TaskRecord) -> list[TaskRecord]:
        """Звенья цепочки эскалации от `record` до последнего, как их видит `wait`."""
        chain = [record]
        seen = {record.task_id}
        while record.result is not None:
            child_id = record.result.meta.get("escalated_to")
            if not child_id or child_id in seen:
                break
            child = await self.get(child_id)
            if child is None:
                break
            seen.add(child.task_id)
            record = child
            chain.append(record)
        return chain

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
                # Демон умер вместе со знанием о worktree задачи. Путь
                # восстанавливается из события, иначе дерево осталось бы
                # на диске, не упомянутое ни в одной записи.
                meta = await self._worktree_meta(record.task_id)
                record.result = _failed_result(record, "daemon restarted", meta)
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
        meta: dict[str, object] = {"branch": payload.get("branch", ""), "integrated": False}
        # Дерево могли убрать до записи результата (`integrate: branch` или
        # удержание перед эскалацией): работа на ветке, а путь вёл бы в пустоту.
        if Path(payload["path"]).exists():  # noqa: ASYNC240
            meta["worktree"] = payload["path"]
        return meta

    async def route_only(self, req: DispatchRequest) -> tuple[RouteDecision, list[GuardEvent]]:
        routing = await self._route(req)
        await self.storage.add_decision(None, routing.decision, routing.candidates)
        # Сработавший guard объясняет решение целиком: события роутеров к нему не относятся.
        return routing.decision, [routing.guard] if routing.guard else routing.events

    async def _route(
        self,
        req: DispatchRequest,
        *,
        is_escalation: bool = False,
        exclude: set[str] | frozenset[str] = frozenset(),
        current_task_id: str | None = None,
    ) -> Routing:
        await self.availability.check_all()
        review_only = review_only_executors(req, self.settings)
        unavailable = self.availability.unavailable() | set(exclude)
        parent_exists = (
            await self.storage.task_exists(req.parent_task_id) if req.parent_task_id else False
        )
        siblings = 0
        if req.parent_task_id and not is_escalation:
            # Ретраи эскалации не съедают квоту fan-out родителя, cancelled тоже.
            siblings = await self.storage.count_children(
                req.parent_task_id,
                exclude_escalated=True,
                exclude_task_id=current_task_id,
            )
        verdict = pre_guards(req, self.settings, unavailable, parent_exists, siblings, review_only)
        names = candidates(self.settings, unavailable | review_only, req.source_agent, req.hop)
        if isinstance(verdict, GuardEvent):
            cooldown = self.availability.cooldown(verdict.executor) if verdict.executor else None
            if verdict.reason == GuardReason.unavailable and cooldown is not None:
                # Явно выбранный исполнитель остывает: вызывающему нужно знать
                # почему и до какого времени, а не только «недоступен».
                verdict.detail += (
                    f" (cooldown until {cooldown.until_utc.isoformat(timespec='seconds')}"
                    f" after {cooldown.kind}: {cooldown.detail[:160]})"
                )
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
            executor=self.settings.routing.fallback_chain[0],
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
                # Отмену до первого шага `_run` (shutdown сразу после submit)
                # или посреди `_mark_cancelled` записывает только этот колбэк.
                cancelled = worker.cancelled()
                record.status = TaskStatus.cancelled if cancelled else TaskStatus.failed
                record.finished_at = self._now()
                record.result = _failed_result(
                    record, "cancelled" if cancelled else "worker stopped unexpectedly"
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
        budget = await self._daily_budget_guard()
        if budget is not None:
            # До роутера: исчерпанный бюджет не тратят и на решение Jev.
            await self._guard_failure(record, budget)
            return None
        exclude: set[str] = set()
        if record.escalated_from is not None and record.request.executor is None:
            # Повторная маршрутизация после сбоя исполнителя: те, кто уже брал
            # задачу, в выбор не входят, иначе роутер вернул бы её им же.
            exclude = set(await self._escalation_chain(record))
        routing = await self._route(
            record.request,
            is_escalation=record.escalated_from is not None,
            exclude=exclude,
            current_task_id=record.task_id,
        )
        if routing.guard and routing.guard.reason != GuardReason.user_override:
            await self._guard_failure(record, routing.guard)
            return None
        decision = routing.decision
        if record.escalated_from is not None:
            decision = decision.model_copy(
                update={"router": RouterKind.fallback, "reason": GuardReason.escalated}
            )
        record.decision = decision
        # Решение должно быть видно в status, пока задача ждёт semaphore.
        await self.storage.update_task(record)
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

    async def _daily_budget_guard(self, extra: float = 0.0) -> GuardEvent | None:
        """Отказ `budget`, если расходы с местной полуночи (плюс `extra`) у лимита."""
        limit = self.settings.routing.daily_cost_limit_usd
        if limit is None:
            return None
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        spent = extra + await self.storage.cost_since(midnight)
        if spent < limit:
            return None
        return GuardEvent(
            reason=GuardReason.budget,
            detail=(
                f"daily cost limit reached: ${spent:.2f} of daily_cost_limit_usd "
                f"${limit:.2f} since {midnight.isoformat(timespec='minutes')}"
            ),
        )

    async def _execute(
        self, record: TaskRecord, decision: RouteDecision, adapter: ExecutorAdapter
    ) -> None:
        req, task_id = record.request, record.task_id
        review = req.kind == "review"
        # Ревью читает рабочую копию как есть: worktree от HEAD не видел бы
        # незакоммиченного диффа, который и просят проверить. Ничего не правит,
        # поэтому и lock на рабочую копию ему не нужен.
        mode = req.workspace_mode or self.settings.execution.workspace_mode
        if review:
            mode = "in_place"
        # Корневая задача и корневое ревью ходят в разные пулы; вложенная
        # работа (hop > 0) мимо обоих, иначе исполнитель в подзадаче ждал бы
        # слот своего же родителя — взаимная блокировка.
        if req.hop > 0:
            semaphore = contextlib.nullcontext()
        elif review:
            semaphore = self._review_semaphore
        else:
            semaphore = self._semaphore
        # В режиме worktree исполнители не делят дерево, поэтому lock нужен
        # только на время переноса результата обратно в рабочую копию.
        lock = (
            self._cwd_lock(req.cwd)
            if req.hop == 0 and mode == "in_place" and not review
            else contextlib.nullcontext()
        )
        async with semaphore, lock:
            # Путь и ветка известны до создания: отмена посреди `worktree.create`
            # бросает ожидание, но поток доводит каталог до конца, и корутина
            # никогда не узнает о дереве, которое уже есть на диске.
            target = self._worktree_target(task_id) if mode == "worktree" else None
            result: ExecutionResult
            plan: EscalationPlan | None
            try:
                if decision.executor in self.availability.unavailable():
                    result = ExecutionResult(
                        status="failed",
                        executor=decision.executor,
                        model=None,
                        summary="",
                        error=(
                            "executor became unavailable while waiting for a slot: "
                            f"{decision.executor}"
                        ),
                    )
                    plan = await self._plan_escalation(record, decision, result)
                elif not await asyncio.to_thread(Path(req.cwd).is_dir):
                    result = ExecutionResult(
                        status="failed",
                        executor=decision.executor,
                        model=None,
                        summary="",
                        error=f"cwd does not exist: {req.cwd} (parent worktree removed?)",
                    )
                    plan = await self._plan_escalation(record, decision, result)
                else:
                    result, plan = await self._run_executor(
                        record, decision, adapter, target, review
                    )
            except asyncio.CancelledError as exc:
                # Отмена задачи это BaseException, её не ловит обычный путь
                # ошибок. Дерево остаётся намеренно: в нём может лежать
                # незакоммиченная работа исполнителя.
                if target is not None:
                    await self._note_kept_worktree(record, *target, exc)
                raise
            except Exception as exc:
                # Сбой создания, подготовки или интеграции worktree не должен
                # обходить эскалацию и финальную запись результата.
                if target is not None:
                    await self._note_kept_worktree(record, *target, exc)
                result = record.result or _failed_result(record, f"{type(exc).__name__}: {exc}")
                plan = await self._plan_escalation(record, decision, result)
            try:
                await self._finalize(record, decision, result, plan)
            except asyncio.CancelledError as exc:
                # `_finalize` уже присвоил финальный результат, но мог быть
                # отменён до записи. Сохраняем его вместе с оставшимся деревом.
                if target is not None:
                    await self._note_kept_worktree(record, *target, exc)
                raise

    async def _run_executor(
        self,
        record: TaskRecord,
        decision: RouteDecision,
        adapter: ExecutorAdapter,
        target: tuple[Path, str] | None,
        review: bool,
    ) -> tuple[ExecutionResult, EscalationPlan | None]:
        """Создать workspace, запустить executor и завершить worktree."""
        req, task_id = record.request, record.task_id
        tree = None
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
        before = await self._review_fingerprint(req) if review else None
        result = await self._call_adapter(adapter, ctx, decision)
        if ctx.resume_session and self._resume_failed(result):
            # Сессию не нашли или CLI другой версии не понял флаги: один раз
            # с нуля, итог прошлой попытки идёт в промпт.
            await self.storage.add_event(
                task_id,
                "resume_failed",
                {"session": ctx.resume_session, "error": (result.error or "")[:500]},
            )
            ctx = await self._build_context(record, decision, tree, resume=False)
            result = await self._call_adapter(adapter, ctx, decision)
            result.meta["resume_failed"] = True
        if review:
            await self._check_review_wrote_nothing(req, before, result)
        await self._note_executor_failure(record, decision, result)
        if not review and req.verify and result.status in ("completed", "partial"):
            await self._verify(record, result, tree)
        # Ревью, после которого изменилась рабочая копия, не эскалируется:
        # иначе вызывающий получил бы ответ следующего ревьюера без
        # предупреждения о правках первого.
        plan = (
            None
            if review and result.meta.get("warning")
            else await self._plan_escalation(record, decision, result)
        )
        if tree is not None:
            # Работу, которую переделает следующий исполнитель, в рабочую
            # копию не переносим: он стартует с HEAD, и его патч лёг бы
            # поверх чужой недоделки с конфликтом. Она остаётся коммитом
            # на ветке задачи.
            hold = plan is not None and plan.hands_off
            await self._finish_worktree(req, tree, result, task_id, hold=hold)
        return result, plan

    async def _verify(
        self, record: TaskRecord, result: ExecutionResult, tree: worktree.Worktree | None
    ) -> None:
        """Запустить команды `verify` там, где работал исполнитель, и судить по ним.

        В worktree работа сначала коммитится на ветку задачи, а после проверки
        дерево возвращается к этому коммиту: кэши и отчёты, которые оставила
        проверка, не должны попасть в патч вызывающему.
        """
        req = record.request
        cwd, commands = Path(req.cwd), list(req.verify)
        committed = False
        if tree is not None:
            try:
                await asyncio.to_thread(
                    worktree.commit, tree, f"agent-dispatch: task {record.task_id[:8]}"
                )
                committed = True
            except worktree.WorktreeError as exc:
                # Работа цела в дереве; без коммита откат после проверки стёр
                # бы её, поэтому артефакты проверки останутся в патче.
                result.meta["verify_artifacts_kept"] = f"commit failed: {exc}"
            # Команды написаны для cwd вызывающего, а исполнитель работал в
            # корне дерева: проверка идёт в том же подкаталоге дерева, и
            # абсолютные пути рабочей копии переписываются на дерево.
            try:
                root = await asyncio.to_thread(worktree.repo_root, req.cwd)
                relative = Path(req.cwd).resolve().relative_to(root.resolve())  # noqa: ASYNC240
            except (worktree.WorktreeError, ValueError):
                root, relative = None, Path()
            cwd = tree.path / relative
            if root is not None:
                commands = [
                    rewrite_repo_prefix(command, str(root), str(tree.path)) or command
                    for command in commands
                ]
        info = await verify.run_verification(
            commands,
            cwd=cwd,
            env=child_env(self.settings),
            timeout_seconds=self.settings.execution.verify_timeout_seconds,
            log_path=Path(record.log_path),
            result=result,
        )
        verify.apply_verification(result, info)
        await self.storage.add_event(
            record.task_id,
            "verify",
            {
                "result": info.result,
                "commands": len(info.commands),
                "tests_changed": len(info.tests_changed),
                "contradicts_report": info.contradicts_report,
            },
        )
        if committed and tree is not None:
            if await self._tree_in_use(tree):
                # Внутри дерева ещё работает подзадача: откат стёр бы её правки.
                result.meta["verify_artifacts_kept"] = "active children"
                return
            try:
                await asyncio.to_thread(worktree.discard_changes, tree)
            except worktree.WorktreeError as exc:
                # Работа уже в коммите: несостоявшийся откат оставляет в патче
                # только артефакты проверки, а не отменяет результат исполнителя.
                result.meta["verify_artifacts_kept"] = f"discard failed: {exc}"

    @staticmethod
    async def _call_adapter(
        adapter: ExecutorAdapter, ctx: RunContext, decision: RouteDecision
    ) -> ExecutionResult:
        try:
            return await adapter.execute(ctx)
        except Exception as exc:
            return ExecutionResult(
                status="failed",
                executor=decision.executor,
                model=None,
                summary="",
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _resume_failed(result: ExecutionResult) -> bool:
        """Продолженная сессия упала, не начав работу.

        Сбой исполнителя (лимит, сеть) повторился бы и с нуля, а таймаут и
        молчание значат, что агент работал: их не повторяем.
        """
        if result.status != "failed" or result.changed_files:
            return False
        error = result.error or ""
        if error == "timeout" or error.startswith("stalled:"):
            return False
        return classify_failure(result) is None

    @staticmethod
    async def _review_fingerprint(req: DispatchRequest) -> str | None:
        try:
            return await asyncio.to_thread(workspace.fingerprint, req.cwd)
        except subprocess.CalledProcessError:
            return None

    async def _check_review_wrote_nothing(
        self, req: DispatchRequest, before: str | None, result: ExecutionResult
    ) -> None:
        """Предупредить, если рабочая копия изменилась за время ревью.

        Режим только чтения задаёт CLI, и он не гарантия. Правки не откатываются:
        это рабочая копия вызывающего, и решать о ней ему. Ревью идёт без lock,
        поэтому изменение мог сделать и параллельный запуск; предупреждение
        говорит о факте, а не о виновнике.
        """
        after = await self._review_fingerprint(req)
        if result.changed_files:
            result.meta["warning"] = "review changed files: " + ", ".join(result.changed_files)
        elif before is not None and after is not None and before != after:
            result.meta["warning"] = "working copy changed during the review"

    async def _build_context(
        self,
        record: TaskRecord,
        decision: RouteDecision,
        tree: worktree.Worktree | None,
        *,
        resume: bool = True,
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
        session = None
        if req.followup_of:
            attempt, session = await self._followup(req, decision, tree, resume=resume)
            package = package.model_copy(update={"followup": attempt})
        prompt = await asyncio.to_thread(
            render_prompt,
            package,
            self.settings.executors[decision.executor].adapter,
            self.settings,
        )
        return RunContext(
            cwd=str(tree.path) if tree else req.cwd,
            timeout_seconds=req.timeout_seconds or self.settings.routing.default_timeout_seconds,
            idle_timeout_seconds=self.settings.execution.idle_timeout_seconds or None,
            env=child_env(
                self.settings,
                {
                    "AGENT_DISPATCH_TASK_ID": record.task_id,
                    "AGENT_DISPATCH_ROOT_AGENT": record.root_agent.value,
                    "AGENT_DISPATCH_HOP": str(req.hop + 1),
                    # Источник вложенного вызова это сам исполнитель. Без этого
                    # MCP-прокси внутри codex унаследовал бы источник демона
                    # (того, кто его поднял), и правило review_only не сработало бы.
                    "AGENT_DISPATCH_SOURCE_AGENT": self.settings.executors[
                        decision.executor
                    ].adapter,
                },
            ),
            log_path=Path(record.log_path),
            task_id=record.task_id,
            prompt=prompt,
            read_only=req.kind == "review",
            resume_session=session,
        )

    async def _followup(
        self,
        req: DispatchRequest,
        decision: RouteDecision,
        tree: worktree.Worktree | None,
        *,
        resume: bool,
    ) -> tuple[Followup | None, str | None]:
        """Итог продолжаемой задачи и сессия CLI, если её можно продолжить.

        Сессию продолжает только тот же исполнитель в том же каталоге: claude
        ищет сессии по cwd, а у прошлого запуска в worktree дерева уже нет.
        """
        previous = await self.get(req.followup_of) if req.followup_of else None
        if previous is None or previous.result is None or previous.decision is None:
            return None, None
        session = previous.result.meta.get("session_id")
        same_place = (
            tree is None
            and not await self._worktree_meta(previous.task_id)
            and Path(previous.request.cwd).resolve() == Path(req.cwd).resolve()  # noqa: ASYNC240
        )
        resumed = bool(
            resume
            and isinstance(session, str)
            and session
            and same_place
            and previous.decision.executor == decision.executor
        )
        original = previous
        seen = {previous.task_id}
        while original.request.followup_of and original.request.followup_of not in seen:
            seen.add(original.request.followup_of)
            earlier = await self.get(original.request.followup_of)
            if earlier is None:
                break
            original = earlier
        described = followup.describe(previous, original, resumed=resumed)
        return described, session if resumed else None

    async def _note_executor_failure(
        self, record: TaskRecord, decision: RouteDecision, result: ExecutionResult
    ) -> None:
        """Вывести исполнителя из ротации, если он упал на сбое, не связанном с задачей."""
        kind = classify_failure(result)
        if kind is None:
            return
        result.meta["executor_failure"] = kind
        error = result.error or ""
        routing = self.settings.routing
        configured = self.settings.executors.get(decision.executor)
        seconds = cooldown_seconds(
            kind,
            error,
            self._now(),
            failure_seconds=routing.failure_cooldown_seconds,
            quota_seconds=routing.quota_cooldown_seconds,
            recheck=configured is not None and configured.limit_reset == "recheck",
        )
        members = self._limit_group(decision.executor, failure_scope(kind, error))
        tripped: dict[str, datetime] = {}
        for name in members:
            cooldown = self.availability.trip(name, kind, error, seconds, origin=decision.executor)
            if cooldown is None:
                continue
            tripped[name] = cooldown.until_utc
            await self.storage.save_cooldown(
                name,
                kind=cooldown.kind,
                detail=cooldown.detail,
                origin=cooldown.origin,
                until=cooldown.until_utc,
                task_id=record.task_id,
            )
        if decision.executor not in tripped:
            return
        until = tripped[decision.executor].isoformat(timespec="seconds")
        result.meta["cooldown_until"] = until
        await self.storage.add_event(
            record.task_id,
            "cooldown",
            {
                "executor": decision.executor,
                "kind": kind,
                "until": until,
                "executors": sorted(tripped),
            },
        )

    def _limit_group(self, executor: str, scope: str) -> list[str]:
        """Исполнитель и все, кто делит с ним лимит (scope `group`)."""
        configured = self.settings.executors.get(executor)
        if scope != "group" or configured is None:
            return [executor]
        group = configured.resolved_limit_group(executor)
        return [executor] + [
            name
            for name, item in self.settings.executors.items()
            if name != executor and item.enabled and item.resolved_limit_group(name) == group
        ]

    async def restore_cooldowns(self) -> int:
        """Вернуть остывания из базы после перезапуска демона."""
        count = 0
        for row in await self.storage.active_cooldowns(self._now()):
            if row["executor"] not in self.settings.executors:
                continue
            restored = self.availability.restore(
                row["executor"], row["kind"], row["detail"], row["until"], row["origin"]
            )
            count += restored is not None
        return count

    async def clear_cooldowns(self, names: list[str] | None = None) -> list[str]:
        """Снять остывание вручную: лимит подняли или сбросили раньше срока."""
        cleared = self.availability.clear(set(names) if names is not None else None)
        await self.storage.delete_cooldowns(names)
        return cleared

    async def _plan_escalation(
        self, record: TaskRecord, decision: RouteDecision, result: ExecutionResult
    ) -> EscalationPlan | None:
        if not record.request.allow_escalation:
            return None
        reason = should_escalate(result)
        if reason is None:
            return None
        failure = result.meta.get("executor_failure")
        if failure:
            reason = f"executor_{failure}"
        links = await self._escalation_links(record)
        tried = [executor for _, executor in links]
        # Цепочка эскалации подчиняется тому же правилу ревью, что и роутер:
        # иначе задача Codex дошла бы до Claude через `codex/sol: [claude/opus]`.
        unavailable = self.availability.unavailable() | review_only_executors(
            record.request, self.settings
        )
        nxt = next_executor(decision.executor, self.settings, tried, unavailable)
        plan = EscalationPlan(
            reason=reason, tried=tried, executor=nxt, reroute=nxt is None and bool(failure)
        )
        if not plan.hands_off:
            return plan
        stop = await self._budget_stop(links, result)
        if stop is None:
            return plan
        # Звено, которое бюджет всё равно не пустит, не должно удерживать
        # работу (`integration_held`): цепочка кончается здесь, работа
        # интегрируется, как у любой исчерпанной цепочки.
        return EscalationPlan(reason=f"{reason}; {stop}", tried=tried)

    async def _budget_stop(
        self, links: list[tuple[str, str]], result: ExecutionResult
    ) -> str | None:
        """Причина не эскалировать по деньгам или None; цифры идут в `meta.budget`.

        Текущий результат ещё не записан в базу, его цена прибавляется
        отдельно; подзадачи звеньев и завершённые за сутки задачи уже там.
        """
        current = result.usage.cost_usd if result.usage and result.usage.cost_usd else 0.0
        routing = self.settings.routing
        if routing.max_chain_cost_usd is not None:
            spent = current + await self.storage.tree_cost([task_id for task_id, _ in links])
            if spent >= routing.max_chain_cost_usd:
                result.meta["budget"] = {
                    "chain_cost_usd": round(spent, 4),
                    "limit_usd": routing.max_chain_cost_usd,
                }
                return f"chain cost ${spent:.2f} reached max_chain_cost_usd"
        guard = await self._daily_budget_guard(extra=current)
        if guard is not None:
            result.meta["budget"] = {"daily_limit_usd": routing.daily_cost_limit_usd}
            return guard.detail
        return None

    async def _finalize(
        self,
        record: TaskRecord,
        decision: RouteDecision,
        result: ExecutionResult,
        plan: EscalationPlan | None = None,
    ) -> None:
        """Записать результат, при необходимости эскалировать, разбудить ожидающих."""
        record.result = result
        record.status, record.finished_at = TaskStatus(result.status), self._now()
        if plan is not None:
            await self._escalate(record, decision, result, plan)
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
        plan: EscalationPlan,
    ) -> None:
        """Отдать задачу следующему исполнителю цепочки, либо закрыть её отказом."""
        if not plan.hands_off:
            result.meta["escalation_chain"] = plan.tried
            if result.status != "failed":
                result.status = "failed"
                result.error = f"escalation exhausted: {plan.reason}"
                record.status = TaskStatus.failed
            return
        await self.storage.add_event(
            record.task_id,
            "escalate",
            {"from": decision.executor, "to": plan.executor or "router", "reason": plan.reason},
        )
        # `executor: None` отдаёт задачу роутеру; уже пробовавшие исключает `_prepare`.
        update: dict[str, object] = {"executor": plan.executor}
        if record.request.followup_of:
            # Звено follow-up продолжает упавшую попытку, а не задачу до неё:
            # иначе следующий исполнитель видел бы устаревший отчёт.
            update["followup_of"] = record.task_id
        child = await self.submit(
            record.request.model_copy(update=update),
            escalated_from=record.task_id,
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
        result = _failed_result(
            record, "cancelled" if cancelled else f"{type(exc).__name__}: {exc}"
        )
        result.meta["worktree"], result.meta["branch"] = str(path), branch
        # Отмена может прийти и после удачной интеграции, когда дерево оставлено
        # по `keep_worktrees`. Готовый вердикт об интеграции не перебивается:
        # рабочая копия уже изменена, и `integrated: false` был бы ложью.
        result.meta.setdefault("integrated", False)
        record.result = result
        await self.storage.update_task(record)

    async def _mark_cancelled(self, record: TaskRecord) -> None:
        if record.status in FINAL_STATUSES:
            return
        record.status, record.finished_at = TaskStatus.cancelled, self._now()
        await self.storage.add_event(record.task_id, "cancel", {})
        await self.storage.update_task(record)
        self._notify(record.task_id)

    async def _mark_failed(self, record: TaskRecord, exc: BaseException) -> None:
        record.result = _failed_result(record, f"{type(exc).__name__}: {exc}")
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
        *,
        hold: bool = False,
    ) -> None:
        """Закоммитить результат на ветке worktree и перенести его в рабочую копию.

        `hold` оставляет результат на ветке, как `integrate: branch`: задачу
        переделывает следующий исполнитель, и его патч должен лечь на чистую копию.

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
            _not_integrated(result, str(exc))
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
            _not_integrated(result, str(exc))
            return
        patch_path = self.settings.server.data_dir / "patches" / f"{task_id}.patch"
        try:
            await asyncio.to_thread(patch_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(patch_path.write_text, patch)
        except OSError as exc:
            # Патч не лёг на диск: применять нечего, но работа цела на ветке,
            # и дерево остаётся вместе с ней.
            _not_integrated(result, f"patch not written: {exc}")
            return
        result.meta["patch"] = str(patch_path)
        if hold and execution.integrate == "apply":
            result.meta["integration_held"] = "escalated"
        if execution.integrate == "branch" or (hold and execution.integrate == "apply"):
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
            _not_integrated(result, str(exc))
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
        if await self._tree_in_use(tree):
            result.meta["worktree"] = str(tree.path)
            result.meta["branch"] = tree.branch
            result.meta["worktree_kept"] = "active children"
            return
        try:
            await asyncio.to_thread(worktree.remove, tree, keep_branch=keep_branch)
        except worktree.WorktreeError as exc:
            result.meta["worktree_cleanup_error"] = str(exc)
            return
        result.meta.pop("worktree", None)
        if not keep_branch:
            result.meta.pop("branch", None)

    async def _tree_in_use(self, tree: worktree.Worktree) -> bool:
        """В дереве cwd незавершённой задачи: убирать или откатывать его нельзя."""
        tree_path = tree.path.expanduser().resolve()
        for active in await self.storage.list_active_tasks():
            active_cwd = Path(active.request.cwd).expanduser().resolve()  # noqa: ASYNC240
            if active_cwd == tree_path or tree_path in active_cwd.parents:
                return True
        return False

    async def _escalation_chain(self, record: TaskRecord) -> list[str]:
        return [executor for _, executor in await self._escalation_links(record)]

    async def _escalation_links(self, record: TaskRecord) -> list[tuple[str, str]]:
        """Звенья цепочки эскалации от первого к текущему: (task_id, executor)."""
        chain: list[tuple[str, str]] = []
        current: TaskRecord | None = record
        seen = {record.task_id}
        # Без предела глубины: от циклов защищает `seen`, а усечённая цепочка
        # вернула бы ранних исполнителей в выбор при повторной маршрутизации,
        # и она перестала бы быть конечной.
        while current is not None:
            if current.decision is not None:
                chain.append((current.task_id, current.decision.executor))
            previous_id = current.escalated_from
            if previous_id is None or previous_id in seen:
                break
            seen.add(previous_id)
            current = await self.storage.get_task(previous_id)
            if current is None:
                break
        chain.reverse()
        return chain
