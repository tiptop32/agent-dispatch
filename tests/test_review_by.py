"""`review_by`: демон сам отдаёт законченную правку на ревью другому исполнителю."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch import dispatcher as dispatcher_module
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors import worktree as worktree_module
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    RouteDecision,
    RouterKind,
    TaskStatus,
)
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 10.0
FINDINGS = "a.py:1 the fix returns a string instead of an int\nsecond line"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _done(executor: str = "omp", files: list[str] | None = None, **kwargs) -> ExecutionResult:
    values: dict = {"status": "completed", "summary": "done", "changed_files": ["a.py"]}
    if files is not None:
        values["changed_files"] = files
    values.update(kwargs)
    return ExecutionResult(executor=executor, model=None, **values)


def _review(verdict: str | None, summary: str = FINDINGS, **kwargs) -> ExecutionResult:
    return ExecutionResult(
        status="completed", executor="rev", model=None, summary=summary, verdict=verdict, **kwargs
    )


async def _make(
    tmp_path: Path,
    *,
    results: dict[str, ExecutionResult] | None = None,
    review_by: str | None = "rev",
    escalation: dict[str, list[str]] | None = None,
    **execution,
):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path / "data"),
        routing=RoutingSettings(fallback_executor="omp"),
        execution=ExecutionSettings(**execution),
        executors={
            "omp": ExecutorSettings(
                adapter="omp", model="agent-lb/luna", description="omp", review_by=review_by
            ),
            "rev": ExecutorSettings(adapter="claude", description="reviewer"),
            "claude": ExecutorSettings(adapter="claude", description="claude"),
        },
        escalation={"omp": ["claude"]} if escalation is None else escalation,
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    results = {"omp": _done(), "rev": _review("pass"), **(results or {})}
    adapters = {name: FakeAdapter(name, result=results.get(name)) for name in settings.executors}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return storage, adapters, dispatcher


def _req(repo: Path, **kwargs) -> DispatchRequest:
    values: dict = {"task": "fix the parser", "cwd": str(repo), "executor": "omp"}
    values.update(kwargs)
    return DispatchRequest(**values)


@pytest.mark.asyncio
async def test_completed_work_is_reviewed_read_only_by_review_by(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(_req(git_repo, context="secret notes of the caller"))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["by"] == "rev" and review["verdict"] == "pass"
    assert review["summary"] == FINDINGS
    assert "escalated_to" not in done.result.meta
    (ctx,) = adapters["rev"].calls
    assert ctx.read_only
    # Чистый контекст: задача и диф, без заметок вызывающего.
    assert "fix the parser" in ctx.prompt and "secret notes" not in ctx.prompt
    assert "$ git diff HEAD" in ctx.prompt
    child = await storage.get_task(review["task_id"])
    assert child.parent_task_id == record.task_id
    assert child.request.kind == "review" and child.request.executor == "rev"
    assert child.hop == 1 and not child.request.allow_escalation
    events = [e for e in await storage.list_events(record.task_id) if e["kind"] == "review_by"]
    assert [e["payload"] for e in events] == [
        {"child": child.task_id, "by": "rev"},
        {"child": child.task_id, "verdict": "pass"},
    ]


@pytest.mark.asyncio
async def test_failed_review_escalates_with_the_findings_in_context(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path, results={"rev": _review("fail")})
    record = await dispatcher.submit(_req(git_repo, context="caller context"))
    final = await dispatcher.wait(record.task_id, WAIT)
    first = await storage.get_task(record.task_id)

    assert first.status == TaskStatus.partial
    assert first.result.error == "review failed (rev)"
    assert first.result.meta["review"]["verdict"] == "fail"
    assert first.result.meta["escalated_to"] == final.task_id
    assert final.decision.executor == "claude" and final.status == TaskStatus.completed
    events = await storage.list_events(record.task_id)
    assert any(
        e["kind"] == "escalate" and e["payload"]["reason"] == "review_failed" for e in events
    )
    context = final.request.context
    assert context.startswith("caller context")
    assert f"Review of the previous attempt by rev: {FINDINGS}" in context
    (ctx,) = adapters["claude"].calls
    assert "the fix returns a string" in ctx.prompt


@pytest.mark.asyncio
async def test_findings_reach_the_next_link_in_prompt_only_mode(tmp_path, git_repo):
    # В режиме `prompt` раздел Context не рендерится: замечания идут в текст задачи.
    _, adapters, dispatcher = await _make(tmp_path, results={"rev": _review("fail")})
    record = await dispatcher.submit(_req(git_repo, context_mode="prompt"))
    await dispatcher.wait(record.task_id, WAIT)

    (ctx,) = adapters["claude"].calls
    assert "Review of the previous attempt by rev" in ctx.prompt


@pytest.mark.asyncio
async def test_worktree_review_sees_the_committed_work_and_failure_holds_it(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 'broken'\n")
        (Path(ctx.cwd) / "new.py").write_text("y = 1\n")

    storage, adapters, dispatcher = await _make(
        tmp_path,
        results={"omp": _done(files=["a.py", "new.py"]), "rev": _review("fail")},
        workspace_mode="worktree",
    )
    adapters["omp"].on_execute = edit
    record = await dispatcher.submit(_req(git_repo))
    await dispatcher.wait(record.task_id, WAIT)
    first = await storage.get_task(record.task_id)

    assert first.result.meta["integration_held"] == "escalated"
    assert (git_repo / "a.py").read_text() == "x = 1\n"
    assert not (git_repo / "new.py").exists()
    (ctx,) = adapters["rev"].calls
    tree = dispatcher.settings.worktree_base / record.task_id
    assert Path(ctx.cwd).resolve() == tree.resolve()  # noqa: ASYNC240
    child = await storage.get_task(first.result.meta["review"]["task_id"])
    base = child.request.review_base
    assert base and f"$ git diff {base}" in ctx.prompt
    assert "broken" in ctx.prompt and "new.py" in ctx.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("no changes", {"results": {"omp": _done(files=[])}}),
        ("failed verify", {}),
        ("no review_by", {"review_by": None}),
        ("review task", {}),
    ],
)
async def test_review_does_not_run(tmp_path, git_repo, case, kwargs):
    _, adapters, dispatcher = await _make(tmp_path, **kwargs)
    extra: dict = {"allow_escalation": False}
    if case == "failed verify":
        extra["verify"] = ["exit 1"]
    if case == "review task":
        extra["kind"] = "review"
        adapters["omp"].result = _done(files=[])
    record = await dispatcher.submit(_req(git_repo, **extra))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert not adapters["rev"].calls
    assert "review" not in done.result.meta


@pytest.mark.asyncio
async def test_review_without_verdict_is_unknown_and_does_not_block(tmp_path, git_repo):
    _, _, dispatcher = await _make(tmp_path, results={"rev": _review(None)})
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    assert done.result.meta["review"]["verdict"] == "unknown"
    assert done.result.error is None


@pytest.mark.asyncio
async def test_review_refused_by_max_hops_is_skipped(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(_req(git_repo, hop=1))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped" and review["reason"].startswith("max_hops:")
    assert not adapters["rev"].calls


@pytest.mark.asyncio
async def test_crashed_reviewer_is_skipped(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["rev"].raise_exc = RuntimeError("cli exploded")
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped"
    assert "cli exploded" in review["reason"]


@pytest.mark.asyncio
async def test_unavailable_reviewer_is_skipped(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    dispatcher.availability.trip("rev", "offline", "connection refused", 600)
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.status == TaskStatus.completed
    assert done.result.meta["review"]["verdict"] == "skipped"
    assert done.result.meta["review"]["reason"].startswith("unavailable")
    assert not adapters["rev"].calls


@pytest.mark.asyncio
async def test_exhausted_chain_after_failed_review_integrates_the_work(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    _, adapters, dispatcher = await _make(
        tmp_path, results={"rev": _review("fail")}, escalation={}, workspace_mode="worktree"
    )
    adapters["omp"].on_execute = edit
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id
    assert done.status == TaskStatus.failed
    assert done.result.error == "escalation exhausted: review_failed"
    assert done.result.meta["integrated"] is True
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert done.result.meta["review"]["verdict"] == "fail"


@pytest.mark.asyncio
async def test_perimeter_is_stronger_than_the_review(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    dispatcher.settings.executors["omp"].corporate = True
    # Роутер не спросил о корп. данных: решение помечено `perimeter: unknown`,
    # и внешний ревьюер не должен увидеть диф.
    dispatcher.routers = [
        FakeRouter(
            RouteDecision(
                router=RouterKind.jev, executor="omp", confidence=0.99, scores={"omp": 0.99}
            )
        )
    ]
    record = await dispatcher.submit(_req(git_repo, executor=None))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.decision.meta["perimeter"] == "unknown"
    assert done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped" and review["reason"].startswith("perimeter:")
    assert not adapters["rev"].calls


@pytest.mark.asyncio
async def test_perimeter_task_is_reviewed_by_the_corporate_reviewer(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    dispatcher.settings.executors["omp"].corporate = True
    dispatcher.settings.executors["claude"].corporate = True
    dispatcher.settings.executors["omp"].perimeter_review_by = "claude"
    dispatcher.adapters["claude"].result = _review("pass")
    dispatcher.routers = [
        FakeRouter(
            RouteDecision(
                router=RouterKind.jev, executor="omp", confidence=0.99, scores={"omp": 0.99}
            )
        )
    ]
    record = await dispatcher.submit(_req(git_repo, executor=None))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.decision.meta["perimeter"] == "unknown"
    review = done.result.meta["review"]
    assert review["by"] == "claude" and review["verdict"] == "pass"
    assert not adapters["rev"].calls
    (ctx,) = adapters["claude"].calls
    assert ctx.read_only and "A refused tool call is not an obstacle" in ctx.prompt


async def _review_child(storage: Storage, parent_id: str):
    tasks = await storage.list_tasks(limit=100)
    (child,) = [task for task in tasks if task.parent_task_id == parent_id]
    return child


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["submit", "event", "wait"])
async def test_cancelled_parent_cancels_its_review(tmp_path, git_repo, stage):
    # Отмена родителя на любом шаге от постановки ревью до ожидания не должна
    # оставить ревьюера работать без хозяина: его CLI платный.
    storage, adapters, dispatcher = await _make(tmp_path)
    adapters["rev"].gate = asyncio.Event()  # ревьюер не закончит сам
    reached, hold = asyncio.Event(), asyncio.Event()
    add_event, git_events = storage.add_event, []

    async def blocking(task_id, kind, payload):
        if kind == "git":
            git_events.append(task_id)
        # Второе событие `git` пишет `submit` ревью уже после создания воркера.
        if (stage == "submit" and kind == "git" and len(git_events) == 2) or (
            stage == "event" and kind == "review_by"
        ):
            reached.set()
            await hold.wait()
        return await add_event(task_id, kind, payload)

    storage.add_event = blocking
    record = await dispatcher.submit(_req(git_repo))
    await asyncio.wait_for((adapters["rev"].started if stage == "wait" else reached).wait(), WAIT)
    dispatcher._tasks[record.task_id].cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    hold.set()
    parent = await dispatcher.wait(record.task_id, WAIT)
    child = await _review_child(storage, record.task_id)
    child = await dispatcher.wait(child.task_id, 3.0)

    assert parent.status == TaskStatus.cancelled
    assert child.status == TaskStatus.cancelled
    if stage == "wait":
        assert adapters["rev"].cancelled


@pytest.mark.asyncio
async def test_review_timeout_cancels_the_reviewer_and_is_skipped(tmp_path, git_repo, monkeypatch):
    monkeypatch.setattr(dispatcher_module, "REVIEW_WAIT_GRACE_SECONDS", 0)
    storage, adapters, dispatcher = await _make(tmp_path)
    dispatcher.settings.executors["rev"].timeout_seconds = 1
    adapters["rev"].gate = asyncio.Event()
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped" and review["reason"] == "timeout: no review within 1 s"
    assert adapters["rev"].cancelled
    assert (await storage.get_task(review["task_id"])).status == TaskStatus.cancelled


@pytest.mark.asyncio
async def test_review_submit_failure_is_skipped(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    submit = dispatcher.submit

    async def failing(req, escalated_from=None):
        if req.kind == "review":
            raise RuntimeError("database is locked")
        return await submit(req, escalated_from)

    dispatcher.submit = failing
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped" and review["task_id"] is None
    assert review["reason"] == "submit: RuntimeError: database is locked"
    assert not adapters["rev"].calls


@pytest.mark.asyncio
async def test_review_commit_failure_is_skipped(tmp_path, git_repo, monkeypatch):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["omp"].on_execute = edit
    commit, calls = worktree_module.commit, []

    def failing(tree, message):
        calls.append(message)
        if len(calls) == 1:
            raise worktree_module.WorktreeError("index.lock exists")
        return commit(tree, message)

    monkeypatch.setattr(worktree_module, "commit", failing)
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped" and review["reason"] == "commit: index.lock exists"
    assert not adapters["rev"].calls
    assert (git_repo / "a.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_worktree_review_task_points_at_the_tree_not_the_caller_copy(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    repo = git_repo.resolve()  # noqa: ASYNC240
    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["omp"].on_execute = edit
    record = await dispatcher.submit(_req(git_repo, task=f"Fix {repo}/a.py and {repo}-old/b.py"))
    await dispatcher.wait(record.task_id, WAIT)

    (ctx,) = adapters["rev"].calls
    tree = Path(ctx.cwd)
    assert f"Fix {tree}/a.py" in ctx.prompt
    assert f"{repo}/a.py" not in ctx.prompt
    # Граница пути строгая: соседний каталог не переписывается.
    assert f"{repo}-old/b.py" in ctx.prompt


@pytest.mark.asyncio
async def test_reviewer_writes_in_the_worktree_do_not_reach_the_caller(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    async def scribble(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 'reviewer'\n")
        (Path(ctx.cwd) / "notes.md").write_text("review notes\n")

    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["omp"].on_execute = edit
    adapters["rev"].on_execute = scribble
    record = await dispatcher.submit(_req(git_repo))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert done.task_id == record.task_id and done.status == TaskStatus.completed
    assert done.result.meta["integrated"] is True
    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert not (git_repo / "notes.md").exists()
    assert "review_artifacts_kept" not in done.result.meta


@pytest.mark.asyncio
async def test_in_place_review_names_the_files_of_the_work(tmp_path, git_repo):
    # Диф HEAD рабочей копии вызывающего содержит и его собственные правки:
    # ревьюер должен знать, какие файлы относятся к работе исполнителя.
    _, adapters, dispatcher = await _make(tmp_path, results={"omp": _done(files=["a.py", "b.py"])})
    record = await dispatcher.submit(_req(git_repo))
    await dispatcher.wait(record.task_id, WAIT)

    (ctx,) = adapters["rev"].calls
    assert "Review the changes in these files: a.py, b.py" in ctx.prompt
    assert "other uncommitted changes in the working copy are not part of this review" in (
        ctx.prompt
    )


def _settings(**omp) -> Settings:
    return Settings(
        routing=RoutingSettings(fallback_executor="omp"),
        executors={
            "omp": ExecutorSettings(adapter="omp", model="agent-lb/luna", **omp),
            "rev": ExecutorSettings(adapter="claude"),
        },
    )


def test_config_accepts_a_known_reviewer():
    assert _settings(review_by="rev").executors["omp"].review_by == "rev"


@pytest.mark.parametrize(
    ("review_by", "message"),
    [("ghost", "executors.omp.review_by: unknown executor 'ghost'"), ("omp", "itself")],
)
def test_config_rejects_unknown_or_self_reviewer(review_by, message):
    with pytest.raises(ValidationError, match=message):
        _settings(review_by=review_by)
