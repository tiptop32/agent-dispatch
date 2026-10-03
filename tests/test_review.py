"""Codex не отдаёт Claude работу, только ревью; ревью идёт в режиме только чтения.

Телеметрия 22 сентября - 2 октября: Codex отдал claude/opus 50 задач, 21 упала,
в основном на лимитах. Цель правила `routing.review_only` в экономии токенов Claude.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.dispatch.task_package import build_task_package, render_prompt
from agent_dispatch.executors.base import RunContext, strip_flags
from agent_dispatch.executors.claude import ClaudeAdapter
from agent_dispatch.executors.codex import CodexAdapter
from agent_dispatch.executors.opencode import OpenCodeAdapter
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult, RouteDecision, RouterKind
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

ROOT = Path(__file__).parent
WAIT = 2.0


class CapturingRouter(FakeRouter):
    """Роутер, который запоминает кандидатов и выбирает первого из них."""

    def __init__(self):
        super().__init__()
        self.candidates: list[list[str]] = []

    async def decide(self, req, candidates):
        self.calls += 1
        self.candidates.append(sorted(candidates))
        name = sorted(candidates)[0]
        return RouteDecision(
            executor=name, confidence=1.0, scores={name: 1.0}, router=RouterKind.jev
        )


def _settings(tmp_path: Path, **routing) -> Settings:
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="codex/sol", **routing),
        execution=ExecutionSettings(workspace_mode="worktree"),
        executors={
            "claude/opus": ExecutorSettings(adapter="claude", model="opus"),
            "codex/sol": ExecutorSettings(adapter="codex", model="sol"),
            "opencode/a": ExecutorSettings(adapter="opencode", model="x5/a"),
            "opencode/b": ExecutorSettings(adapter="opencode", model="x5/b"),
        },
        escalation={"opencode/a": ["claude/opus", "opencode/b"]},
    )


async def _make(tmp_path: Path, results: dict[str, ExecutionResult] | None = None, **routing):
    settings = _settings(tmp_path, **routing)
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name)) for name in settings.executors
    }
    router = CapturingRouter()
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    return storage, adapters, router, dispatcher


def _req(repo: Path, source: str = "codex", **kwargs) -> DispatchRequest:
    return DispatchRequest(task="fix bug", cwd=str(repo), source_agent=source, **kwargs)


async def _run(dispatcher: Dispatcher, req: DispatchRequest):
    return await dispatcher.wait((await dispatcher.submit(req)).task_id, WAIT)


def test_default_config_keeps_codex_work_away_from_claude():
    assert RoutingSettings().review_only == {"codex": ["claude"]}


@pytest.mark.asyncio
async def test_router_never_offers_claude_for_codex_work(tmp_path, git_repo):
    _, adapters, router, dispatcher = await _make(tmp_path)
    done = await _run(dispatcher, _req(git_repo))
    # codex/sol вне выбора по exclude_source_agent, claude/opus по review_only.
    assert router.candidates == [["opencode/a", "opencode/b"]]
    assert done.status == "completed" and adapters["claude/opus"].calls == []


@pytest.mark.asyncio
async def test_explicit_claude_for_codex_work_is_refused_without_a_run(tmp_path, git_repo):
    _, adapters, _, dispatcher = await _make(tmp_path)
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus"))
    assert done.status == "failed" and done.result.meta["guard"] == "review_only"
    assert "kind='review'" in done.result.error
    assert adapters["claude/opus"].calls == []


@pytest.mark.asyncio
async def test_codex_may_ask_claude_for_a_read_only_review_of_the_working_copy(tmp_path, git_repo):
    storage, adapters, _, dispatcher = await _make(tmp_path)
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus", kind="review"))

    assert done.status == "completed"
    ctx = adapters["claude/opus"].calls[0]
    assert ctx.read_only is True
    # Ревью видит незакоммиченный дифф: рабочая копия, а не worktree от HEAD.
    assert ctx.cwd == str(git_repo)
    events = await storage.list_events(done.task_id)
    assert not [event for event in events if event["kind"] == "worktree"]
    assert "# Review only" in ctx.prompt


@pytest.mark.asyncio
async def test_review_through_the_router_may_pick_claude(tmp_path, git_repo):
    _, _, router, dispatcher = await _make(tmp_path)
    await _run(dispatcher, _req(git_repo, kind="review"))
    assert router.candidates == [["claude/opus", "opencode/a", "opencode/b"]]


@pytest.mark.asyncio
async def test_escalation_of_codex_work_skips_claude(tmp_path, git_repo):
    failed = ExecutionResult(status="failed", executor="opencode/a", model=None, summary="")
    storage, adapters, _, dispatcher = await _make(tmp_path, {"opencode/a": failed})
    first = await dispatcher.submit(_req(git_repo, executor="opencode/a"))
    await dispatcher.wait(first.task_id, WAIT)
    events = await storage.list_events(first.task_id)
    escalate = next(event["payload"] for event in events if event["kind"] == "escalate")
    assert escalate["to"] == "opencode/b"
    assert adapters["claude/opus"].calls == []


@pytest.mark.asyncio
async def test_other_sources_still_give_claude_work(tmp_path, git_repo):
    _, adapters, _, dispatcher = await _make(tmp_path)
    done = await _run(dispatcher, _req(git_repo, source="cli", executor="claude/opus"))
    assert done.status == "completed" and adapters["claude/opus"].calls[0].read_only is False


@pytest.mark.asyncio
async def test_empty_rule_lets_codex_give_claude_work(tmp_path, git_repo):
    _, adapters, _, dispatcher = await _make(tmp_path, review_only={})
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus"))
    assert done.status == "completed" and len(adapters["claude/opus"].calls) == 1


@pytest.mark.asyncio
async def test_review_that_edits_files_is_reported(tmp_path, git_repo):
    edited = ExecutionResult(
        status="completed",
        executor="claude/opus",
        model=None,
        summary="ok",
        changed_files=["a.py"],
    )
    _, _, _, dispatcher = await _make(tmp_path, {"claude/opus": edited})
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus", kind="review"))
    assert done.result.meta["warning"] == "review changed files: a.py"


# --- адаптеры в режиме только чтения ------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--sandbox", "workspace-write", "--foo"], ["--foo"]),
        (["--sandbox=workspace-write", "--foo"], ["--foo"]),
        (["-s", "danger-full-access"], []),
        (["--full-auto", "--foo", "bar"], ["--foo", "bar"]),
        ([], []),
    ],
)
def test_strip_flags_drops_flags_with_their_values(args, expected):
    assert strip_flags(args, {"--sandbox", "-s"}, {"--full-auto"}) == expected


def _ctx(repo: Path, tmp_path: Path, cap: Path) -> RunContext:
    return RunContext(
        cwd=str(repo),
        timeout_seconds=5,
        env={"PATH": os.environ.get("PATH", ""), "FAKE_CAPTURE": str(cap)},
        log_path=tmp_path / "run.log",
        task_id="t",
        prompt="review",
        read_only=True,
    )


@pytest.mark.asyncio
async def test_claude_review_runs_with_read_only_tools(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = ClaudeAdapter(
        "claude/opus",
        ExecutorSettings(
            adapter="claude",
            command=str(ROOT / "fakes/claude_ok.sh"),
            extra_args=[
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Bash,Edit,Write,Read,Glob,Grep",
                "--effort",
                "high",
            ],
        ),
    )
    await adapter.execute(_ctx(git_repo, tmp_path, cap))
    args = (tmp_path / "cap.argv").read_text().splitlines()
    # Правила allow из ~/.claude/settings.json складываются с --allowedTools,
    # поэтому набор инструментов задаёт --tools, а не разрешения.
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--strict-mcp-config" in args
    assert args.count("--permission-mode") == 1
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--allowedTools" not in args and "Bash,Edit,Write,Read,Glob,Grep" not in args
    assert args[args.index("--effort") + 1] == "high"


@pytest.mark.asyncio
async def test_codex_review_runs_in_the_read_only_sandbox(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = CodexAdapter(
        "codex/sol",
        ExecutorSettings(
            adapter="codex",
            command=str(ROOT / "fakes/codex_ok.sh"),
            extra_args=["--sandbox", "workspace-write"],
        ),
    )
    await adapter.execute(_ctx(git_repo, tmp_path, cap))
    args = (tmp_path / "cap.argv").read_text().splitlines()
    # Codex отвергает повтор флага, поэтому из конфига он убран, а не перекрыт.
    assert args.count("--sandbox") == 1
    assert args[args.index("--sandbox") + 1] == "read-only"


@pytest.mark.asyncio
async def test_opencode_review_runs_with_the_plan_agent(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode/a",
        ExecutorSettings(
            adapter="opencode",
            command=str(ROOT / "fakes/opencode_ok.sh"),
            model="x5/a",
            extra_args=["--agent", "build"],
        ),
    )
    await adapter.execute(_ctx(git_repo, tmp_path, cap))
    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args.count("--agent") == 1 and args[args.index("--agent") + 1] == "plan"


def test_review_prompt_forbids_edits_and_delegation(tmp_path, git_repo):
    settings = _settings(tmp_path)
    req = _req(git_repo, kind="review")
    prompt = render_prompt(build_task_package(req, settings), "claude", settings)
    assert "# Review only" in prompt and "Do NOT create, modify or delete" in prompt
    assert "Do NOT delegate this review" in prompt and "`dispatch` tool" not in prompt
    task_prompt = render_prompt(build_task_package(_req(git_repo), settings), "claude", settings)
    assert "Review only" not in task_prompt


def test_review_package_carries_the_working_copy_diff(tmp_path, git_repo):
    (git_repo / "tracked.py").write_text("x = 1\n")
    import subprocess

    from agent_dispatch.executors.workspace import git_env

    subprocess.run(["git", "add", "."], cwd=git_repo, env=git_env(), check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base"],
        cwd=git_repo,
        env=git_env(),
        check=True,
    )
    (git_repo / "tracked.py").write_text("x = 2\n")
    (git_repo / "new.py").write_text("y = 1\n")
    settings = _settings(tmp_path)
    prompt = render_prompt(
        build_task_package(_req(git_repo, kind="review"), settings), "claude", settings
    )
    assert "-x = 1\n+x = 2" in prompt
    assert "?? new.py" in prompt


def test_review_diff_is_capped(tmp_path, git_repo, monkeypatch):
    from agent_dispatch.dispatch import task_package

    monkeypatch.setattr(task_package, "REVIEW_DIFF_LIMIT", 10)
    monkeypatch.setattr(task_package, "_git", lambda cwd, args: "d" * 50)
    package = task_package.build_task_package(_req(git_repo, kind="review"), _settings(tmp_path))
    assert package.git_diff == "d" * 10 + "\n... (diff truncated, read the files directly)\n"


def test_low_confidence_scores_cannot_smuggle_in_an_excluded_executor(tmp_path):
    # Ревью Codex: claude_local отдаёт оценки и за пределами кандидатов. При
    # низкой уверенности и fallback вне кандидатов победил бы claude/opus.
    from agent_dispatch.routing.guards import post_guards

    settings = _settings(tmp_path)
    decision = RouteDecision(
        executor="opencode/a",
        confidence=0.2,
        scores={"opencode/a": 0.2, "claude/opus": 0.9},
        router=RouterKind.claude_local,
    )
    final, _ = post_guards(decision, settings, ["opencode/a", "opencode/b"])
    assert final.executor == "opencode/a"

    outside = decision.model_copy(update={"executor": "claude/opus", "confidence": 0.95})
    final, events = post_guards(outside, settings, ["opencode/a", "opencode/b"])
    assert final.executor == "opencode/a"
    assert any("non-candidate: claude/opus" in event.detail for event in events)


@pytest.mark.asyncio
async def test_review_that_rewrites_an_already_dirty_file_is_reported(tmp_path, git_repo):
    # Ревью Codex: файл был грязным до ревью, набор грязных путей не изменился.
    dirty = next(path for path in git_repo.iterdir() if path.is_file() and path.suffix)
    dirty.write_text("caller's uncommitted edit\n")

    async def rewrite(_ctx):
        dirty.write_text("reviewer wrote here\n")

    storage, adapters, _, dispatcher = await _make(tmp_path)
    adapters["claude/opus"].on_execute = rewrite
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus", kind="review"))
    assert done.result.changed_files == []
    assert done.result.meta["warning"] == "working copy changed during the review"


@pytest.mark.asyncio
async def test_clean_review_has_no_warning(tmp_path, git_repo):
    _, _, _, dispatcher = await _make(tmp_path)
    done = await _run(dispatcher, _req(git_repo, executor="claude/opus", kind="review"))
    assert "warning" not in done.result.meta


@pytest.mark.asyncio
async def test_executor_child_reports_itself_as_the_source(tmp_path, git_repo):
    # Ревью Codex: MCP-прокси внутри codex иначе унаследовал бы источник демона.
    _, adapters, _, dispatcher = await _make(tmp_path)
    await _run(dispatcher, _req(git_repo, source="claude", executor="codex/sol"))
    await _run(dispatcher, _req(git_repo, source="cli", executor="claude/opus"))
    assert adapters["codex/sol"].calls[0].env["AGENT_DISPATCH_SOURCE_AGENT"] == "codex"
    assert adapters["claude/opus"].calls[0].env["AGENT_DISPATCH_SOURCE_AGENT"] == "claude"


@pytest.mark.asyncio
async def test_review_that_changed_the_working_copy_is_not_escalated(tmp_path, git_repo):
    wrote = ExecutionResult(
        status="failed", executor="opencode/a", model=None, summary="", changed_files=["a.py"]
    )
    storage, adapters, _, dispatcher = await _make(tmp_path, {"opencode/a": wrote})
    done = await _run(dispatcher, _req(git_repo, executor="opencode/a", kind="review"))
    assert done.result.meta["warning"] == "review changed files: a.py"
    assert "escalated_to" not in done.result.meta
    assert adapters["opencode/b"].calls == [] and len(await storage.list_tasks()) == 1
