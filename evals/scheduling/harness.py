"""Детерминированный eval планирования корневых задач и ревью.

Живых CLI, сети и демона нет: Dispatcher поднимается in-process на
гейт-адаптерах, которые стартуют и висят, пока гейт не отпустят. Проверяются
поведенческие инварианты ёмкости, а не статические флаги конфига:

- ревью стартует, пока все задачные слоты заняты (нет голодания ревью);
- лишние ревью ждут в собственной ёмкости и не занимают слоты задач;
- отмена заблокированной задачи/ревью освобождает свой слот;
- вложенная работа (hop > 0) идёт мимо обоих пулов;
- после дренирования нет утечек: значения семафоров восстановлены, cwd-локи
  и события задач убраны, все записи в конечном статусе.

Гонка/стресс держится на гейтах и событиях, а не на замерах по sleep:
негативная проверка («не стартовал») тоже защищена гейтом, короткий таймаут
там лишь ограничивает длительность — стартовать задача не может, пока пул
занят, при любой скорости машины.
"""

from __future__ import annotations

import asyncio
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    FINAL_STATUSES,
    Availability,
    DispatchRequest,
    ExecutionResult,
    RouteDecision,
    RouterKind,
    SourceAgent,
)
from agent_dispatch.telemetry.storage import Storage

WAIT = 10.0
NEGATIVE_WAIT = 0.1
CODEX = "codex/sol"
CLAUDE = "claude/opus"
KIMI = "opencode/kimi"


@dataclass
class GatedAdapter:
    """Адаптер-заглушка: стартует, затем ждёт своего гейта.

    Считает пик одновременных исполнений: по нему проверяется, что пул
    удерживает предел, а не «примерно держит».
    """

    name: str
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    inflight: int = 0
    peak: int = 0
    calls: int = 0

    async def execute(self, ctx: Any) -> ExecutionResult:
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.inflight -= 1
            raise
        self.inflight -= 1
        self.finished.set()
        return ExecutionResult(status="completed", executor=self.name, model=None, summary="ok")

    async def check(self) -> Availability:
        return Availability(available=True, version="gated", checked_at=datetime.now(UTC))


class RoleRouter:
    """Детерминизм без решения модели: задачи — codex, ревью — claude."""

    name = "role"

    async def decide(self, req: Any, candidates: list[str]) -> RouteDecision:
        wanted = CLAUDE if req.kind == "review" else CODEX
        name = wanted if wanted in candidates else sorted(candidates)[0]
        return RouteDecision(
            executor=name, confidence=1.0, scores={name: 1.0}, router=RouterKind.jev
        )


@dataclass(frozen=True)
class Verdict:
    scenario: str
    ok: bool
    reasons: list[str]


class SchedulingLab:
    """Диспетчер на гейт-адаптерах в изолированном каталоге."""

    def __init__(self, root: Path, max_tasks: int = 2, max_reviews: int = 1):
        self.root = root
        self.max_tasks = max_tasks
        self.max_reviews = max_reviews
        self.repos: list[Path] = []
        self.adapters: dict[str, GatedAdapter] = {
            name: GatedAdapter(name) for name in (CODEX, CLAUDE, KIMI)
        }
        self.settings = Settings(
            server=ServerSettings(
                data_dir=root / "data",
                max_concurrent_tasks=max_tasks,
                max_concurrent_reviews=max_reviews,
            ),
            routing=RoutingSettings(fallback_executor=CODEX),
            execution=ExecutionSettings(workspace_mode="in_place"),
            executors={
                CODEX: ExecutorSettings(adapter="codex"),
                CLAUDE: ExecutorSettings(adapter="claude"),
                KIMI: ExecutorSettings(adapter="opencode", model="x5/kimi"),
            },
        )

    async def __aenter__(self) -> SchedulingLab:
        self.storage = Storage(self.settings.server.data_dir / "db.sqlite")
        await self.storage.open()
        self.dispatcher = Dispatcher(
            self.settings,
            self.storage,
            self.adapters,  # type: ignore[arg-type]
            AvailabilityCache(self.adapters, 60),  # type: ignore[arg-type]
            [RoleRouter()],
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.dispatcher.shutdown()
        await self.storage.close()

    def repo(self) -> Path:
        repo = self.root / f"repo-{len(self.repos)}"
        repo.mkdir(parents=True)
        # Тот же env без GIT_* для init, add и commit: унаследованный GIT_DIR
        # или хук-контекст из вызывающей репы иначе мутирует эту.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        )
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "add", "a.py"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=eval@example.com",
                "-c",
                "user.name=eval",
                "commit",
                "-qm",
                "init",
            ],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        )
        self.repos.append(repo)
        return repo

    def req(
        self,
        repo: Path,
        *,
        kind: str = "task",
        executor: str | None = None,
        hop: int = 0,
        parent: str | None = None,
    ) -> DispatchRequest:
        return DispatchRequest(
            task="look around" if kind == "review" else "fix bug",
            cwd=str(repo),
            kind=kind,  # type: ignore[arg-type]
            source_agent=SourceAgent.cli,
            executor=executor,
            hop=hop,
            parent_task_id=parent,
        )

    async def submit(self, req: DispatchRequest) -> str:
        return (await self.dispatcher.submit(req)).task_id

    async def wait_started(self, name: str) -> None:
        await asyncio.wait_for(self.adapters[name].started.wait(), WAIT)

    async def never_started(self, name: str) -> bool:
        """True, если адаптер не стартовал: пока гейт чужой задачи держится,
        стартовать ему нечем, поэтому короткий предел здесь не флакает."""
        try:
            await asyncio.wait_for(self.adapters[name].started.wait(), NEGATIVE_WAIT)
        except TimeoutError:
            return True
        return False

    async def cancel_if_active(self, task_id: str) -> None:
        try:
            await self.dispatcher.cancel(task_id, wait_seconds=1.0)
        except ValueError:
            pass

    async def drain(self) -> None:
        """Отпустить все гейты и дождаться конечного статуса каждой задачи."""
        for adapter in self.adapters.values():
            adapter.gate.set()
        for record in await self.storage.list_tasks(limit=2**31 - 1):
            await self.dispatcher.wait(record.task_id, WAIT)

    async def leaks(self) -> list[str]:
        problems: list[str] = []
        tasks_value = self.dispatcher._semaphore._value
        reviews_value = self.dispatcher._review_semaphore._value
        if tasks_value != self.max_tasks:
            problems.append(f"task semaphore leaked: {self.max_tasks - tasks_value} slots lost")
        if reviews_value != self.max_reviews:
            problems.append(
                f"review semaphore leaked: {self.max_reviews - reviews_value} slots lost"
            )
        if self.dispatcher._cwd_locks:
            problems.append(f"cwd locks left behind: {sorted(self.dispatcher._cwd_locks)}")
        if self.dispatcher._events:
            problems.append(f"task events left behind: {sorted(self.dispatcher._events)}")
        for record in await self.storage.list_tasks(limit=2**31 - 1):
            if record.status not in FINAL_STATUSES:
                problems.append(f"task {record.task_id[:8]} stuck in {record.status}")
            elif (
                record.status != "cancelled"
                and record.result
                and record.result.status != "completed"
            ):
                problems.append(f"task {record.task_id[:8]} starved: {record.result.status}")
        return problems


async def scenario_review_unblocked(root: Path) -> Verdict:
    """Ревью стартует, пока оба задачных слота заняты долгими задачами."""
    reasons: list[str] = []
    async with SchedulingLab(root, max_tasks=2, max_reviews=1) as lab:
        repo = lab.repo()
        first = await lab.submit(lab.req(repo, executor=CODEX))
        second = await lab.submit(lab.req(repo, executor=CODEX))
        review = await lab.submit(lab.req(repo, kind="review", executor=CLAUDE))
        await lab.wait_started(CODEX)
        await asyncio.wait_for(lab.adapters[CLAUDE].started.wait(), WAIT)
        if lab.adapters[CLAUDE].inflight != 1:
            reasons.append("root review did not run while coding slots were saturated")
        if lab.adapters[CODEX].peak > 2:
            reasons.append(f"coding peak {lab.adapters[CODEX].peak} exceeded limit 2")
        await lab.drain()
        reasons.extend(await lab.leaks())
        del first, second, review
    return Verdict("review_unblocked_by_saturated_coding", not reasons, reasons)


async def scenario_excess_reviews_queue(root: Path) -> Verdict:
    """Второе ревью ждёт в своей ёмкости и не занимает слот задачи."""
    reasons: list[str] = []
    async with SchedulingLab(root, max_tasks=2, max_reviews=1) as lab:
        repo = lab.repo()
        first = await lab.submit(lab.req(repo, kind="review", executor=CLAUDE))
        second = await lab.submit(lab.req(repo, kind="review", executor=KIMI))
        await lab.wait_started(CLAUDE)
        if not await lab.never_started(KIMI):
            reasons.append("excess review started while review capacity was busy")
        await lab.drain()
        record = await lab.storage.get_task(second)
        if not record or record.status != "completed":
            reasons.append("queued review never completed after capacity freed")
        reasons.extend(await lab.leaks())
        del first
    return Verdict("excess_reviews_queue_in_own_capacity", not reasons, reasons)


async def scenario_cancellation_releases_capacity(root: Path) -> Verdict:
    """Отмена заблокированной задачи и ревью освобождает ровно свой слот."""
    reasons: list[str] = []
    async with SchedulingLab(root, max_tasks=2, max_reviews=1) as lab:
        repo = lab.repo()
        a = await lab.submit(lab.req(repo, executor=CODEX))
        b = await lab.submit(lab.req(repo, executor=CODEX))
        review = await lab.submit(lab.req(repo, kind="review", executor=CLAUDE))
        await lab.wait_started(CODEX)
        await lab.wait_started(CLAUDE)
        queued_review = await lab.submit(lab.req(repo, kind="review", executor=KIMI))
        if not await lab.never_started(KIMI):
            reasons.append("queued review started beyond the review limit")
        await lab.dispatcher.cancel(a)
        # Отменённая задача освободила задачный слот, не ревью-ёмкость:
        # ожидавшее ревью всё ещё ждёт.
        if not await lab.never_started(KIMI):
            reasons.append("cancelled coding task wrongly released review capacity")
        await lab.dispatcher.cancel(review)
        await asyncio.wait_for(lab.adapters[KIMI].started.wait(), WAIT)
        lab.adapters[CODEX].gate.set()
        third = await lab.submit(lab.req(repo, executor=CODEX))
        await lab.dispatcher.wait(third, WAIT)
        record = await lab.storage.get_task(third)
        if not record or record.status != "completed":
            reasons.append("coding task did not take the slot freed by cancelled task")
        await lab.cancel_if_active(b)
        await lab.cancel_if_active(queued_review)
        await lab.drain()
        reasons.extend(await lab.leaks())
    return Verdict("cancellation_releases_capacity", not reasons, reasons)


async def scenario_nested_bypass(root: Path) -> Verdict:
    """Вложенная работа не ждёт ни один пул, когда насыщены оба."""
    reasons: list[str] = []
    async with SchedulingLab(root, max_tasks=2, max_reviews=1) as lab:
        repo = lab.repo()
        parent = await lab.submit(lab.req(repo, executor=CODEX))
        await lab.submit(lab.req(repo, executor=CODEX))
        await lab.submit(lab.req(repo, kind="review", executor=CLAUDE))
        await lab.wait_started(CODEX)
        await lab.wait_started(CLAUDE)
        nested = await lab.submit(lab.req(repo, hop=1, parent=parent))
        deadline = WAIT
        while lab.adapters[CODEX].calls < 2 and deadline > 0:
            await asyncio.sleep(0.05)
            deadline -= 0.05
        if lab.adapters[CODEX].calls < 2:
            reasons.append("nested work was blocked by root pools")
        await lab.drain()
        reasons.extend(await lab.leaks())
        del nested
    return Verdict("nested_work_bypasses_pools", not reasons, reasons)


async def scenario_stress(root: Path, seed: int, rounds: int = 12) -> Verdict:
    """Гонка: перемешанные задачи/ревью с частичными отменами, без голодания."""
    reasons: list[str] = []
    rng = random.Random(seed)
    async with SchedulingLab(root, max_tasks=2, max_reviews=1) as lab:
        repo = lab.repo()
        submitted: list[str] = []
        cancelled = 0
        for index in range(rounds):
            kind = "review" if index % 3 == 2 else "task"
            submitted.append(await lab.submit(lab.req(repo, kind=kind)))
            # Часть стартовавших задач отменяют не дождавшись: ёмкость должна
            # вернуться в оборот, а остальные — доделаться.
            if index in (2, 5, 8):
                victim = submitted[rng.randrange(len(submitted))]
                record = await lab.storage.get_task(victim)
                if record and record.status not in FINAL_STATUSES:
                    await lab.dispatcher.cancel(victim)
                    cancelled += 1
        await lab.drain()
        reasons.extend(await lab.leaks())
        completed = sum(
            1
            for record in await lab.storage.list_tasks(limit=2**31 - 1)
            if record.status == "completed"
        )
        if completed != len(submitted) - cancelled:
            reasons.append(
                f"starvation: {completed} completed, expected {len(submitted) - cancelled}"
            )
        if lab.adapters[CODEX].peak > lab.max_tasks:
            reasons.append(f"coding peak {lab.adapters[CODEX].peak} exceeded {lab.max_tasks}")
        if lab.adapters[CLAUDE].peak > lab.max_reviews:
            reasons.append(f"review peak {lab.adapters[CLAUDE].peak} exceeded {lab.max_reviews}")
    return Verdict(f"stress_race_seed_{seed}", not reasons, reasons)


SCENARIOS = (
    scenario_review_unblocked,
    scenario_excess_reviews_queue,
    scenario_cancellation_releases_capacity,
    scenario_nested_bypass,
)


async def run(seed: int, stress_rounds: int) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="agent-dispatch-scheduling-") as raw:
            verdicts.append(await scenario(Path(raw)))
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-scheduling-") as raw:
        verdicts.append(await scenario_stress(Path(raw), seed, stress_rounds))
    return verdicts
