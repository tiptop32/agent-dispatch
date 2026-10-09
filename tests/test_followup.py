"""Follow-up: продолжение готовой задачи тем же исполнителем, по возможности в его сессии."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_dispatch.api.app import create_app
from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.dispatch.followup import FollowupError, continued_link
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.claude import ClaudeAdapter
from agent_dispatch.executors.codex import CodexAdapter
from agent_dispatch.executors.opencode import OpenCodeAdapter
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    FollowupRequest,
    RouteDecision,
    RouterKind,
    SourceAgent,
    TaskRecord,
    TaskStatus,
    VerificationInfo,
    VerifyCommand,
)
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

ROOT = Path(__file__).parent
WAIT = 5.0


# --- адаптеры: id сессии и продолжение ---------------------------------------


def _ctx(repo: Path, tmp_path: Path, **kwargs) -> RunContext:
    return RunContext(
        cwd=str(repo),
        timeout_seconds=3,
        env={"PATH": os.environ.get("PATH", ""), "FAKE_CAPTURE": str(tmp_path / "cap")},
        log_path=tmp_path / "run.log",
        task_id="t",
        prompt="do task",
        **kwargs,
    )


def _argv(tmp_path: Path) -> list[str]:
    return (tmp_path / "cap.argv").read_text().splitlines()


@pytest.mark.asyncio
async def test_claude_resumes_the_given_session(git_repo, tmp_path):
    adapter = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/claude_ok.sh"))
    )
    await adapter.execute(_ctx(git_repo, tmp_path, resume_session="sess-1"))
    argv = _argv(tmp_path)
    assert argv[3:5] == ["--resume", "sess-1"]
    assert "--session-id" not in argv


@pytest.mark.asyncio
async def test_codex_reports_its_thread_and_resumes_after_exec_flags(git_repo, tmp_path):
    adapter = CodexAdapter(
        "codex",
        ExecutorSettings(
            adapter="codex", command=str(ROOT / "fakes/codex_ok.sh"), extra_args=["--foo"]
        ),
    )
    first = await adapter.execute(_ctx(git_repo, tmp_path))
    assert first.meta["session_id"] == "01a0c53c-3863-7162-8e11-6c44760a6e3c"
    assert "resume" not in _argv(tmp_path)

    await adapter.execute(_ctx(git_repo, tmp_path, resume_session="thread-1"))
    argv = _argv(tmp_path)
    # Флаги exec до подкоманды: `--output-schema` и `-o` разбирает сам exec.
    assert argv[:3] == ["exec", "--json", "-C"]
    assert argv[-4:] == ["--foo", "resume", "thread-1", "-"]


@pytest.mark.asyncio
async def test_opencode_reports_its_session_and_continues_it(git_repo, tmp_path):
    adapter = OpenCodeAdapter(
        "opencode/kimi",
        ExecutorSettings(
            adapter="opencode",
            command=str(ROOT / "fakes/opencode_ok.sh"),
            model="kimi",
            extra_args=["--quiet"],
        ),
    )
    first = await adapter.execute(_ctx(git_repo, tmp_path))
    assert first.meta["session_id"] == "ses_f3ac28112ffeW8DLQORWJlNTsT"
    assert "--session" not in _argv(tmp_path)

    await adapter.execute(_ctx(git_repo, tmp_path, resume_session="ses_1"))
    argv = _argv(tmp_path)
    assert argv[7:9] == ["--session", "ses_1"]
    assert argv[-3:] == ["--quiet", "--auto", "do task"]


# --- диспетчер ---------------------------------------------------------------


class ScriptedAdapter(FakeAdapter):
    """Отдаёт результаты по очереди, каждый раз свежей копией."""

    def __init__(self, name: str, results: list[ExecutionResult] | None = None):
        super().__init__(name)
        self.results = list(results or [])

    async def execute(self, ctx):
        self.calls.append(ctx)
        self.started.set()
        if self.on_execute is not None:
            await self.on_execute(ctx)
        if self.results:
            result = self.results.pop(0)
        else:
            result = ExecutionResult(
                status="completed", executor=self.name, model=None, summary="ok"
            )
        return result.model_copy(deep=True)


def _ok(name: str, session: str | None = "s1", summary: str = "did it") -> ExecutionResult:
    meta = {"session_id": session} if session else {}
    return ExecutionResult(
        status="completed",
        executor=name,
        model=None,
        summary=summary,
        changed_files=["a.py"],
        meta=meta,
    )


async def _make(tmp_path: Path, *, router: FakeRouter | None = None, **execution):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path / "data"),
        routing=RoutingSettings(fallback_executor="codex"),
        execution=ExecutionSettings(**execution),
        executors={
            "codex": ExecutorSettings(adapter="codex", description="codex"),
            "claude": ExecutorSettings(adapter="claude", description="claude"),
        },
        escalation={"codex": ["claude"]},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: ScriptedAdapter(name) for name in settings.executors}
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [router or FakeRouter()]
    )
    return storage, adapters, dispatcher


def _req(repo: Path, **kwargs) -> DispatchRequest:
    return DispatchRequest(task="fix tests", cwd=str(repo), **{"executor": "codex", **kwargs})


async def _done(dispatcher: Dispatcher, task_id: str):
    return await dispatcher.wait(task_id, WAIT)


@pytest.mark.asyncio
async def test_followup_resumes_the_executor_session_in_place(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [_ok("codex", "s1"), _ok("codex", "s1")]
    first = await _done(
        dispatcher, (await dispatcher.submit(_req(git_repo, verify=["true"]))).task_id
    )

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="also handle None"))
    done = await _done(dispatcher, record.task_id)

    assert done.status == TaskStatus.completed
    assert done.request.followup_of == first.task_id
    assert done.request.task == "also handle None"
    assert done.request.verify == ["true"]  # проверка наследуется
    assert done.decision.executor == "codex"
    ctx = adapters["codex"].calls[-1]
    assert ctx.resume_session == "s1"
    assert ctx.cwd == str(git_repo)
    assert ctx.prompt.startswith("# Follow-up")
    assert "also handle None" in ctx.prompt
    assert "# Previous attempt" not in ctx.prompt
    assert "- true" in ctx.prompt


@pytest.mark.asyncio
async def test_followup_without_a_session_carries_the_previous_report(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    failed_check = _ok("codex", None, summary="changed the parser")
    failed_check.verification = VerificationInfo(
        result="failed",
        commands=[VerifyCommand(command="pytest -q", exit_code=1, output_tail="E assert 1 == 2")],
    )
    adapters["codex"].results = [failed_check]
    first = await _done(
        dispatcher, (await dispatcher.submit(_req(git_repo, allow_escalation=False))).task_id
    )

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="fix the test"))
    await _done(dispatcher, record.task_id)

    ctx = adapters["codex"].calls[-1]
    assert ctx.resume_session is None
    assert ctx.prompt.startswith("# Task\nfix the test")
    assert f"This continues task {first.task_id}, done by codex" in ctx.prompt
    assert "Its request: fix tests" in ctx.prompt
    assert "Changed files: a.py" in ctx.prompt
    assert "changed the parser" in ctx.prompt
    assert "`pytest -q` failed (exit 1):\nE assert 1 == 2" in ctx.prompt


@pytest.mark.asyncio
async def test_followup_continues_the_last_link_of_the_escalation_chain(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [
        ExecutionResult(status="failed", executor="codex", model=None, summary="")
    ]
    adapters["claude"].results = [_ok("claude", "c1"), _ok("claude", "c1")]
    submitted = await dispatcher.submit(_req(git_repo))
    last = await _done(dispatcher, submitted.task_id)
    assert last.decision.executor == "claude"

    record = await dispatcher.followup(submitted.task_id, FollowupRequest(message="more"))
    done = await _done(dispatcher, record.task_id)

    assert done.request.followup_of == last.task_id
    assert done.decision.executor == "claude"
    assert adapters["claude"].calls[-1].resume_session == "c1"


@pytest.mark.asyncio
async def test_followup_refuses_running_unknown_and_never_run_tasks(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    with pytest.raises(KeyError):
        await dispatcher.followup("nope", FollowupRequest(message="x"))

    refused = await _done(
        dispatcher,
        (await dispatcher.submit(_req(git_repo, executor="missing"))).task_id,
    )
    assert refused.status == TaskStatus.failed
    with pytest.raises(FollowupError, match="never reached an executor"):
        await dispatcher.followup(refused.task_id, FollowupRequest(message="x"))

    gate = asyncio.Event()

    async def wait_gate(ctx):
        await gate.wait()

    adapters["codex"].on_execute = wait_gate
    running = await dispatcher.submit(_req(git_repo))
    await asyncio.wait_for(adapters["codex"].started.wait(), WAIT)
    with pytest.raises(FollowupError, match="is still"):
        await dispatcher.followup(running.task_id, FollowupRequest(message="x"))
    gate.set()
    await _done(dispatcher, running.task_id)


@pytest.mark.asyncio
async def test_followup_refuses_work_that_is_not_in_the_working_copy(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree", integrate="branch")
    adapters["codex"].on_execute = edit
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    assert first.result.meta["integrated"] is False

    with pytest.raises(FollowupError, match="not in the working copy"):
        await dispatcher.followup(first.task_id, FollowupRequest(message="more"))


@pytest.mark.asyncio
async def test_followup_after_a_worktree_run_goes_in_place_without_resuming(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["codex"].on_execute = edit
    adapters["codex"].results = [_ok("codex", "s1")]
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    assert first.result.meta["integrated"] is True
    adapters["codex"].on_execute = None

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="more"))
    done = await _done(dispatcher, record.task_id)

    # Перенесённый, но не закоммиченный результат видно только в рабочей копии,
    # а сессия claude привязана к каталогу дерева, которого больше нет.
    assert done.request.workspace_mode == "in_place"
    ctx = adapters["codex"].calls[-1]
    assert ctx.cwd == str(git_repo) and ctx.resume_session is None
    assert "# Previous attempt" in ctx.prompt


@pytest.mark.asyncio
async def test_failed_resume_is_retried_once_from_scratch(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [
        _ok("codex", "s1"),
        ExecutionResult(
            status="failed",
            executor="codex",
            model=None,
            summary="",
            error="error: unexpected argument 'resume'",
        ),
        _ok("codex", "s2"),
    ]
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    record = await dispatcher.followup(first.task_id, FollowupRequest(message="more"))
    done = await _done(dispatcher, record.task_id)

    assert done.status == TaskStatus.completed
    assert done.result.meta["resume_failed"] is True
    resumed, fresh = adapters["codex"].calls[-2:]
    assert resumed.resume_session == "s1" and fresh.resume_session is None
    assert "# Previous attempt" in fresh.prompt
    events = await storage.list_events(record.task_id)
    assert any(e["kind"] == "resume_failed" and e["payload"]["session"] == "s1" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    ["You've hit your usage limit. Try again at 5:02 PM.", "timeout", "stalled: no output for 9s"],
)
async def test_resume_is_not_retried_after_executor_failures_or_real_work(
    tmp_path, git_repo, error
):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [
        _ok("codex", "s1"),
        ExecutionResult(status="failed", executor="codex", model=None, summary="", error=error),
    ]
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    record = await dispatcher.followup(
        first.task_id, FollowupRequest(message="more", allow_escalation=False)
    )
    await _done(dispatcher, record.task_id)
    assert len(adapters["codex"].calls) == 2


@pytest.mark.asyncio
async def test_followup_of_a_cooling_executor_goes_to_the_router(tmp_path, git_repo):
    router = FakeRouter(
        RouteDecision(executor="claude", confidence=1, scores={"claude": 1}, router=RouterKind.jev)
    )
    _, adapters, dispatcher = await _make(tmp_path, router=router)
    adapters["codex"].results = [_ok("codex", "s1")]
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    dispatcher.availability.trip("codex", "quota", "hit your usage limit", 600, origin="codex")

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="more"))
    done = await _done(dispatcher, record.task_id)

    assert record.request.executor is None
    assert done.decision.executor == "claude"
    ctx = adapters["claude"].calls[-1]
    assert ctx.resume_session is None and "# Previous attempt" in ctx.prompt


@pytest.mark.asyncio
async def test_review_followup_stays_a_review(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    review = _ok("codex", "s1", summary="looks fine")
    review.changed_files = []
    adapters["codex"].results = [review, review]
    first = await _done(
        dispatcher, (await dispatcher.submit(_req(git_repo, kind="review"))).task_id
    )
    record = await dispatcher.followup(first.task_id, FollowupRequest(message="and b.py?"))
    await _done(dispatcher, record.task_id)

    ctx = adapters["codex"].calls[-1]
    assert ctx.read_only and ctx.resume_session == "s1"
    assert "The working tree still has your changes" not in ctx.prompt


# --- HTTP --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_endpoint(tmp_path, git_repo):
    storage, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [_ok("codex", "s1"), _ok("codex", "s1")]
    settings = dispatcher.settings
    app = create_app(settings, dispatcher, storage, dispatcher.availability, "secret")
    headers = {"host": "127.0.0.1:7433", "authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7433") as client:
        first = await client.post(
            "/tasks",
            headers=headers,
            json={"task": "fix", "cwd": str(git_repo), "executor": "codex", "wait_seconds": 5},
        )
        task_id = first.json()["task_id"]
        ok = await client.post(
            f"/tasks/{task_id}/followup",
            headers=headers,
            json={"message": "more", "wait_seconds": 5},
        )
        missing = await client.post(
            "/tasks/nope/followup", headers=headers, json={"message": "more"}
        )
    assert ok.status_code == 200
    assert ok.json()["request"]["followup_of"] == task_id
    assert missing.status_code == 404
    await dispatcher.shutdown()


# --- регрессии ревью ----------------------------------------------------------


def _link(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.completed,
    executor: str | None = "codex",
    meta: dict | None = None,
) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_task_id=None,
        escalated_from=None,
        root_agent=SourceAgent.cli,
        source_agent=SourceAgent.cli,
        hop=0,
        request=DispatchRequest(task="t", cwd="."),
        status=status,
        decision=(
            RouteDecision(executor=executor, confidence=1, scores={}, router=RouterKind.override)
            if executor
            else None
        ),
        result=ExecutionResult(
            status="failed" if not executor else "completed",
            executor=executor or "",
            model=None,
            summary="",
            meta=meta or {},
        ),
        log_path="",
        created_at=now,
        started_at=now,
        finished_at=now,
    )


def test_guard_refused_last_link_is_skipped():
    chain = [_link("a"), _link("b", status=TaskStatus.failed, executor=None)]
    assert continued_link(chain).task_id == "a"


def test_chain_that_never_ran_is_refused():
    with pytest.raises(FollowupError, match="never reached an executor"):
        continued_link([_link("a", status=TaskStatus.failed, executor=None)])


@pytest.mark.parametrize(
    "meta",
    [
        {"integrated": False, "patch": "/p.patch", "commit": "abc"},
        # Сбой коммита, отмена, перезапуск демона: только дерево и ветка.
        {"integrated": False, "worktree": "/wt/x", "branch": "agent-dispatch/x"},
    ],
)
def test_work_outside_the_working_copy_is_refused(meta):
    with pytest.raises(FollowupError, match="not in the working copy"):
        continued_link([_link("a", meta=meta)])


def test_integrated_worktree_run_is_continued():
    meta = {"integrated": True, "patch": "/p.patch", "commit": "abc"}
    assert continued_link([_link("a", meta=meta)]).task_id == "a"


@pytest.mark.asyncio
async def test_followup_is_judged_by_the_current_caller(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["claude"].results = [_ok("claude", "c1")]
    first = await _done(
        dispatcher, (await dispatcher.submit(_req(git_repo, executor="claude"))).task_id
    )

    # Codex вправе отдать Claude только ревью: follow-up от codex на правку
    # должен получить тот же отказ, что и прямой dispatch.
    record = await dispatcher.followup(
        first.task_id, FollowupRequest(message="more", source_agent=SourceAgent.codex)
    )
    done = await _done(dispatcher, record.task_id)

    assert done.request.source_agent == SourceAgent.codex
    assert done.status == TaskStatus.failed
    assert done.result.meta["guard"] == "review_only"
    assert len(adapters["claude"].calls) == 1


@pytest.mark.asyncio
async def test_escalated_followup_link_describes_the_failed_followup(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [
        _ok("codex", "s1", summary="first pass"),
        # Изменения есть: это провал работы, а не продолжения сессии, и он
        # эскалируется, а не повторяется с нуля.
        ExecutionResult(
            status="failed",
            executor="codex",
            model=None,
            summary="followup broke",
            changed_files=["a.py"],
        ),
    ]
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    record = await dispatcher.followup(first.task_id, FollowupRequest(message="also None"))
    final = await _done(dispatcher, record.task_id)

    assert final.decision.executor == "claude"
    assert final.request.followup_of == record.task_id
    prompt = adapters["claude"].calls[-1].prompt
    assert "followup broke" in prompt and "first pass" not in prompt
    assert "Original task: fix tests" in prompt


@pytest.mark.asyncio
async def test_followup_chain_keeps_the_original_task_in_the_prompt(tmp_path, git_repo):
    _, adapters, dispatcher = await _make(tmp_path)
    adapters["codex"].results = [_ok("codex", None)] * 3
    first = await _done(dispatcher, (await dispatcher.submit(_req(git_repo))).task_id)
    second = await _done(
        dispatcher,
        (await dispatcher.followup(first.task_id, FollowupRequest(message="fix the test"))).task_id,
    )
    await _done(
        dispatcher,
        (await dispatcher.followup(second.task_id, FollowupRequest(message="handle None"))).task_id,
    )

    prompt = adapters["codex"].calls[-1].prompt
    assert "Original task: fix tests" in prompt
    assert "Its request: fix the test" in prompt
