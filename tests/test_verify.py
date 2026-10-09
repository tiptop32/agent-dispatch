"""Проверка `verify`: демон сам запускает команды и судит по коду выхода."""

from __future__ import annotations

import subprocess
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
from agent_dispatch.dispatch.escalation import should_escalate
from agent_dispatch.dispatch.task_package import build_task_package, render_prompt
from agent_dispatch.dispatch.verify import OUTPUT_TAIL, _output_tail, is_test_path
from agent_dispatch.executors import worktree
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    TaskStatus,
    VerificationInfo,
)
from agent_dispatch.models import TestsInfo as ResultTestsInfo
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 10.0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


async def _make(tmp_path: Path, *, results: dict[str, ExecutionResult] | None = None, **execution):
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
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name)) for name in settings.executors
    }
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    return storage, adapters, dispatcher


def _req(repo: Path, verify: list[str], **kwargs) -> DispatchRequest:
    return DispatchRequest(
        task="fix tests", cwd=str(repo), executor="codex", verify=verify, **kwargs
    )


async def _finish(dispatcher: Dispatcher, task_id: str):
    return await dispatcher.wait(task_id, WAIT)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", True),
        ("src/pkg/test_util.py", True),
        ("pkg/util_test.go", True),
        ("web/app.spec.ts", True),
        ("web/__tests__/app.js", True),
        ("src/FooTest.java", True),
        ("src/contest.py", False),
        ("src/latest.py", False),
        ("docs/testing.md", False),
    ],
)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


def test_failed_verification_escalates_even_if_the_executor_reported_green_tests():
    result = ExecutionResult(
        status="partial",
        executor="codex",
        model=None,
        summary="done",
        changed_files=["a.py"],
        tests=ResultTestsInfo(command="pytest", result="passed"),
        verification=VerificationInfo(result="failed"),
    )
    assert should_escalate(result) == "verification_failed"


def test_passed_verification_does_not_escalate():
    result = ExecutionResult(
        status="completed",
        executor="codex",
        model=None,
        summary="done",
        verification=VerificationInfo(result="passed"),
    )
    assert should_escalate(result) is None


@pytest.mark.asyncio
async def test_passing_verification_keeps_the_result_completed(tmp_path, git_repo):
    storage, _, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(_req(git_repo, ["true", "echo checked"]))
    done = await _finish(dispatcher, record.task_id)

    assert done.status == TaskStatus.completed
    info = done.result.verification
    assert info.result == "passed"
    assert [c.command for c in info.commands] == ["true", "echo checked"]
    assert info.commands[1].exit_code == 0 and "checked" in info.commands[1].output_tail
    events = await storage.list_events(record.task_id)
    assert any(e["kind"] == "verify" and e["payload"]["result"] == "passed" for e in events)
    assert "[verify] $ echo checked" in Path(done.log_path).read_text()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_failed_verification_escalates_to_the_next_executor(tmp_path, git_repo):
    claimed = ExecutionResult(
        status="completed",
        executor="codex",
        model=None,
        summary="all green",
        tests=ResultTestsInfo(command="pytest", result="passed"),
    )
    storage, adapters, dispatcher = await _make(tmp_path, results={"codex": claimed})
    # Первый прогон падает, второй (после эскалации) проходит.
    marker = tmp_path / "second"
    command = f"test -f {marker} || {{ touch {marker}; echo boom; exit 3; }}"
    record = await dispatcher.submit(_req(git_repo, [command, "echo never"]))
    final = await _finish(dispatcher, record.task_id)
    first = await storage.get_task(record.task_id)

    assert first.status == TaskStatus.partial
    info = first.result.verification
    assert info.result == "failed" and info.contradicts_report
    # После первой упавшей команды остальные не запускаются.
    assert len(info.commands) == 1 and info.commands[0].exit_code == 3
    assert "boom" in info.commands[0].output_tail
    assert first.result.error.startswith("verification failed (exit 3)")
    assert first.result.meta["escalated_to"] == final.task_id
    assert final.decision.executor == "claude" and final.status == TaskStatus.completed
    assert final.result.verification.result == "passed"
    assert adapters["claude"].calls
    events = await storage.list_events(record.task_id)
    assert any(
        e["kind"] == "escalate" and e["payload"]["reason"] == "verification_failed" for e in events
    )


@pytest.mark.asyncio
async def test_failed_verification_without_escalation_is_partial(tmp_path, git_repo):
    _, _, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(_req(git_repo, ["exit 1"], allow_escalation=False))
    done = await _finish(dispatcher, record.task_id)

    assert done.status == TaskStatus.partial
    assert done.result.verification.result == "failed"
    assert "escalated_to" not in done.result.meta


@pytest.mark.asyncio
async def test_exhausted_chain_turns_failed_verification_into_failed(tmp_path, git_repo):
    _, _, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(_req(git_repo, ["exit 1"]))
    done = await _finish(dispatcher, record.task_id)

    assert done.decision.executor == "claude"
    assert done.status == TaskStatus.failed
    assert done.result.error == "escalation exhausted: verification_failed"


@pytest.mark.asyncio
async def test_verification_timeout_counts_as_failure(tmp_path, git_repo):
    _, _, dispatcher = await _make(tmp_path, verify_timeout_seconds=1)
    record = await dispatcher.submit(_req(git_repo, ["sleep 30"], allow_escalation=False))
    done = await _finish(dispatcher, record.task_id)

    command = done.result.verification.commands[0]
    assert command.timed_out
    assert done.result.error.startswith("verification failed (timed out)")


@pytest.mark.asyncio
async def test_rate_limit_text_in_verification_output_does_not_cool_the_executor(
    tmp_path, git_repo
):
    _, _, dispatcher = await _make(tmp_path)
    record = await dispatcher.submit(
        _req(git_repo, ["echo 'rate limit exceeded'; exit 1"], allow_escalation=False)
    )
    done = await _finish(dispatcher, record.task_id)

    assert "executor_failure" not in done.result.meta
    assert dispatcher.availability.unavailable() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "needs_context"])
async def test_verification_is_skipped_when_the_executor_did_not_finish(tmp_path, git_repo, status):
    result = ExecutionResult(status=status, executor="codex", model=None, summary="")
    _, _, dispatcher = await _make(tmp_path, results={"codex": result})
    marker = tmp_path / "ran"
    record = await dispatcher.submit(_req(git_repo, [f"touch {marker}"], allow_escalation=False))
    done = await _finish(dispatcher, record.task_id)

    assert done.result.verification is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_review_is_not_verified(tmp_path, git_repo):
    _, _, dispatcher = await _make(tmp_path)
    marker = tmp_path / "ran"
    record = await dispatcher.submit(_req(git_repo, [f"touch {marker}"], kind="review"))
    done = await _finish(dispatcher, record.task_id)

    assert done.result.verification is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_changed_test_files_are_reported(tmp_path, git_repo):
    result = ExecutionResult(
        status="completed",
        executor="codex",
        model=None,
        summary="done",
        changed_files=["a.py", "tests/test_a.py"],
    )
    _, _, dispatcher = await _make(tmp_path, results={"codex": result})
    record = await dispatcher.submit(_req(git_repo, ["true"]))
    done = await _finish(dispatcher, record.task_id)

    assert done.result.verification.tests_changed == ["tests/test_a.py"]
    assert not done.result.verification.contradicts_report


@pytest.mark.asyncio
async def test_worktree_verification_runs_in_the_tree_and_leaves_no_artifacts(tmp_path, git_repo):
    (git_repo / "sub").mkdir()
    (git_repo / "sub" / "keep.txt").write_text("keep\n")
    _git(git_repo, "add", "sub")
    _git(git_repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "sub")

    async def edit(ctx):
        (Path(ctx.cwd) / "sub" / "new.py").write_text("y = 1\n")

    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["codex"].on_execute = edit
    # Команда идёт в подкаталоге дерева (cwd запроса — sub), видит правку
    # исполнителя и оставляет артефакт, которого не должно быть в патче.
    record = await dispatcher.submit(
        _req(git_repo / "sub", ["test -f new.py && touch artifact.txt && mkdir -p .cache"])
    )
    done = await _finish(dispatcher, record.task_id)

    assert done.status == TaskStatus.completed, done.result
    assert done.result.verification.result == "passed"
    assert done.result.meta["integrated"] is True
    assert (git_repo / "sub" / "new.py").read_text() == "y = 1\n"
    assert not (git_repo / "sub" / "artifact.txt").exists()
    patch = Path(done.result.meta["patch"]).read_text()  # noqa: ASYNC240
    assert "new.py" in patch and "artifact.txt" not in patch


@pytest.mark.asyncio
async def test_worktree_failed_verification_holds_the_work_on_the_branch(tmp_path, git_repo):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 'broken'\n")

    storage, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["codex"].on_execute = edit
    record = await dispatcher.submit(_req(git_repo, ["exit 1"]))
    await _finish(dispatcher, record.task_id)
    first = await storage.get_task(record.task_id)

    assert first.result.meta["integration_held"] == "escalated"
    assert (git_repo / "a.py").read_text() == "x = 1\n"
    branch = first.result.meta["branch"]
    assert "broken" in _git(git_repo, "show", f"{branch}:a.py")


def test_commit_returns_the_existing_commit_when_the_index_is_clean(tmp_path, git_repo):
    tree = worktree.create(git_repo, tmp_path / "tree", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    sha = worktree.commit(tree, "work")
    assert sha is not None
    (tree.path / "junk.txt").write_text("junk\n")
    worktree.discard_changes(tree)
    assert not (tree.path / "junk.txt").exists()
    assert worktree.commit(tree, "again") == sha


def test_commit_without_work_still_returns_none(tmp_path, git_repo):
    tree = worktree.create(git_repo, tmp_path / "tree", "agent-dispatch/t")
    assert worktree.commit(tree, "nothing") is None


def test_prompt_lists_verification_commands(tmp_path, git_repo):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path),
        executors={"codex": ExecutorSettings(adapter="codex")},
    )
    req = _req(git_repo, [f"uv run pytest {git_repo}/tests -q"])
    plain = render_prompt(build_task_package(req, settings), "codex", settings)
    assert "# Verification" in plain
    assert f"- uv run pytest {git_repo}/tests -q" in plain

    tree = str(tmp_path / "tree")
    package = build_task_package(req, settings, tree, "agent-dispatch/t")
    in_tree = render_prompt(package, "codex", settings)
    assert f"- uv run pytest {tree}/tests -q" in in_tree

    without = render_prompt(build_task_package(_req(git_repo, []), settings), "codex", settings)
    assert "# Verification" not in without


def test_output_tail_keeps_both_streams():
    stdout = "x" * 5000 + "\nFAILED tests/test_a.py::test_a"
    stderr = "DeprecationWarning\n" * 500
    tail = _output_tail(stdout, stderr)
    assert "FAILED tests/test_a.py::test_a" in tail and "[stderr]" in tail
    assert len(tail) <= OUTPUT_TAIL + len("\n[stderr]\n")
    assert _output_tail("only out", "") == "only out"
    assert _output_tail("", "only err") == "only err"


@pytest.mark.asyncio
async def test_failed_rollback_after_verification_keeps_the_result(tmp_path, git_repo, monkeypatch):
    async def edit(ctx):
        (Path(ctx.cwd) / "a.py").write_text("x = 2\n")

    def broken_discard(tree):
        raise worktree.WorktreeError("git reset failed")

    monkeypatch.setattr(worktree, "discard_changes", broken_discard)
    _, adapters, dispatcher = await _make(tmp_path, workspace_mode="worktree")
    adapters["codex"].on_execute = edit
    record = await dispatcher.submit(_req(git_repo, ["true"]))
    done = await _finish(dispatcher, record.task_id)

    assert done.status == TaskStatus.completed
    assert done.result.verification.result == "passed"
    assert done.result.meta["verify_artifacts_kept"] == "discard failed: git reset failed"
    assert (git_repo / "a.py").read_text() == "x = 2\n"
