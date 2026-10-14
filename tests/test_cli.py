import asyncio
import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from agent_dispatch.cli import app
from agent_dispatch.config import load_settings
from agent_dispatch.mcp import autostart
from agent_dispatch.models import DispatchRequest, SourceAgent, TaskRecord, TaskStatus
from agent_dispatch.serve_state import ServeState, write_state
from agent_dispatch.telemetry.storage import Storage

runner = CliRunner()


def _state(port: int = 17433, token: str = "secret") -> ServeState:
    return ServeState(pid=os.getpid(), port=port, token=token, started_at=datetime.now(UTC))


@pytest.fixture(autouse=True)
def _daemon_port():
    """Порт фальшивого демона занят по-настоящему: `is_running` смотрит не только на pid."""
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 17433))
        holder.listen(1)
        yield


def _route_payload() -> dict:
    return {
        "executor": "codex",
        "confidence": 0.84,
        "scores": {"codex": 0.84},
        "router": "jev",
    }


def _view(status: str = "completed", *, task_id: str = "t1", meta: dict | None = None) -> dict:
    result = None
    if status in {"completed", "partial", "failed", "needs_context", "needs_escalation"}:
        result = {
            "status": status,
            "executor": "codex",
            "model": None,
            "summary": "done",
            "changed_files": ["a.py"],
            "meta": meta or {},
        }
    return {
        "task_id": task_id,
        "parent_task_id": None,
        "escalated_from": None,
        "root_agent": "cli",
        "source_agent": "cli",
        "hop": 0,
        "request": {"task": "x", "cwd": ".", "source_agent": "cli"},
        "status": status,
        "decision": None,
        "result": result,
        "log_path": "",
        "created_at": datetime.now(UTC).isoformat(),
        "started_at": None,
        "finished_at": None,
        "log_tail": "",
    }


def _write_running_state(tmp_config_dir, state: ServeState) -> None:
    write_state(tmp_config_dir.parent / "data", state)


def _patch_autostart(monkeypatch, state: ServeState) -> None:
    async def fake_ensure(_settings):
        return state

    monkeypatch.setattr(autostart, "ensure_daemon", fake_ensure)


@respx.mock
def test_route_prints_decision_and_sends_cli_source(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    request = respx.post(f"http://127.0.0.1:{state.port}/route").mock(
        return_value=httpx.Response(200, json=_route_payload())
    )

    result = runner.invoke(app, ["route", "fix tests"])

    assert result.exit_code == 0
    assert "executor: codex" in result.stdout
    assert "confidence: 0.84" in result.stdout
    assert json.loads(request.calls[0].request.content)["source_agent"] == "cli"


@respx.mock
def test_dispatch_sends_executor(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    request = respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["dispatch", "--executor", "opencode/kimi", "x"])
    assert result.exit_code == 0
    assert json.loads(request.calls[0].request.content)["executor"] == "opencode/kimi"


@respx.mock
def test_dispatch_wait_zero_prints_running_hint(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(202, json=_view("running"))
    )
    status = respx.get(f"http://127.0.0.1:{state.port}/tasks/t1")
    result = runner.invoke(app, ["dispatch", "--wait", "0", "x"])
    assert result.exit_code == 0
    assert "task_id: t1" in result.stdout
    assert "still running: agent-dispatch status t1" in result.stdout
    assert status.call_count == 0


@respx.mock
def test_dispatch_polls_until_completed(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(202, json=_view("running"))
    )
    status = respx.get(f"http://127.0.0.1:{state.port}/tasks/t1").mock(
        return_value=httpx.Response(200, json=_view("completed"))
    )
    result = runner.invoke(app, ["dispatch", "--poll", "0", "x"])
    assert result.exit_code == 0
    assert "status: completed" in result.stdout
    assert "still running" not in result.stdout
    assert status.call_count == 1


@respx.mock
def test_dispatch_polls_escalated_task(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(202, json=_view("running"))
    )
    respx.get(f"http://127.0.0.1:{state.port}/tasks/t1").mock(
        return_value=httpx.Response(200, json=_view("failed", meta={"escalated_to": "child"}))
    )
    respx.get(f"http://127.0.0.1:{state.port}/tasks/child").mock(
        return_value=httpx.Response(200, json=_view("completed", task_id="child"))
    )
    result = runner.invoke(app, ["dispatch", "--poll", "0", "x"])
    assert result.exit_code == 0
    assert "escalated to child" in result.stdout
    assert "status: completed" in result.stdout


def test_dispatch_timeout_zero_is_rejected(tmp_config_dir):
    result = runner.invoke(app, ["dispatch", "--timeout", "0", "x"])
    assert result.exit_code != 0


@respx.mock
def test_dispatch_completed_prints_summary_without_hint(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["dispatch", "x"])
    assert result.exit_code == 0
    assert "status: completed" in result.stdout and "summary: done" in result.stdout
    assert "still running" not in result.stdout


@respx.mock
def test_dispatch_disables_escalation(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    request = respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["dispatch", "--no-escalation", "x"])
    assert result.exit_code == 0
    assert json.loads(request.calls[0].request.content)["allow_escalation"] is False


@respx.mock
def test_status_prints_task(tmp_config_dir):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    respx.get(f"http://127.0.0.1:{state.port}/tasks/t1").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["status", "t1"])
    assert result.exit_code == 0 and "status: completed" in result.stdout


@respx.mock
def test_cancel_prints_cancelled_task(tmp_config_dir):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    respx.delete(f"http://127.0.0.1:{state.port}/tasks/t1").mock(
        return_value=httpx.Response(200, json=_view("cancelled"))
    )
    result = runner.invoke(app, ["cancel", "t1"])
    assert result.exit_code == 0 and "status: cancelled" in result.stdout


@respx.mock
def test_executors_prints_names_and_availability(tmp_config_dir):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    respx.get(f"http://127.0.0.1:{state.port}/executors").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "codex",
                    "adapter": "codex",
                    "model": None,
                    "enabled": True,
                    "available": True,
                    "version": "1.2.3",
                    "error": None,
                }
            ],
        )
    )
    result = runner.invoke(app, ["executors"])
    assert result.exit_code == 0
    assert "codex" in result.stdout and "available" in result.stdout


@respx.mock
def test_feedback_posts_outcome(tmp_config_dir):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    request = respx.post(f"http://127.0.0.1:{state.port}/tasks/t1/feedback").mock(
        return_value=httpx.Response(200, json={"ok": True, "event_id": 1})
    )
    result = runner.invoke(app, ["feedback", "t1", "--outcome", "user_accepted"])
    assert result.exit_code == 0
    assert json.loads(request.calls[0].request.content)["outcome"] == "user_accepted"


@respx.mock
def test_export_writes_jsonl_unchanged(tmp_config_dir):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    request = respx.get(f"http://127.0.0.1:{state.port}/export").mock(
        return_value=httpx.Response(200, text='{"task_id":"t1"}\n')
    )
    result = runner.invoke(app, ["export", "--since", "7d"])
    assert result.exit_code == 0
    assert result.stdout == '{"task_id":"t1"}\n'
    assert request.calls[0].request.url.params["since"] == "7d"


def test_missing_serve_state_exits_two_with_hint(tmp_config_dir):
    result = runner.invoke(app, ["status", "t1"])
    assert result.exit_code == 2
    assert "agent-dispatch serve" in result.stderr


def test_autostart_failure_exits_two_with_detail(tmp_config_dir, monkeypatch):
    async def fail(_settings):
        from agent_dispatch.mcp.client import DaemonUnavailable

        raise DaemonUnavailable("boom")

    monkeypatch.setattr(autostart, "ensure_daemon", fail)
    result = runner.invoke(app, ["route", "x"])
    assert result.exit_code == 2
    assert "boom" in result.stderr and "agent-dispatch serve" in result.stderr


@respx.mock
def test_serve_state_token_is_sent_as_bearer(tmp_config_dir):
    state = _state(token="from-state")
    _write_running_state(tmp_config_dir, state)
    request = respx.get(f"http://127.0.0.1:{state.port}/tasks/t1").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["status", "t1"])
    assert result.exit_code == 0
    assert request.calls[0].request.headers["authorization"] == "Bearer from-state"


def test_worktrees_lists_trees_and_branches_left_by_branch_mode(tmp_config_dir, git_repo, tmp_path):
    from agent_dispatch.executors import worktree

    attached = worktree.create(git_repo, tmp_path / "wt" / "kept", "agent-dispatch/kept")
    orphan = worktree.create(git_repo, tmp_path / "wt" / "gone", "agent-dispatch/gone")
    (orphan.path / "a.py").write_text("x = 2\n")
    worktree.commit(orphan, "wip")
    worktree.remove(orphan, keep_branch=True)

    result = runner.invoke(app, ["worktrees", "--cwd", str(git_repo)])

    assert result.exit_code == 0
    assert str(attached.path) in result.stdout
    assert "agent-dispatch/gone" in result.stdout
    assert "(no worktree)" in result.stdout


def test_worktrees_clean_with_delete_branches_removes_orphan_branches(
    tmp_config_dir, git_repo, tmp_path
):
    from agent_dispatch.executors import worktree

    orphan = worktree.create(git_repo, tmp_path / "wt" / "gone", "agent-dispatch/gone")
    (orphan.path / "a.py").write_text("x = 2\n")
    worktree.commit(orphan, "wip")
    worktree.remove(orphan, keep_branch=True)

    result = runner.invoke(
        app, ["worktrees", "--cwd", str(git_repo), "--clean", "--delete-branches"]
    )

    assert result.exit_code == 0
    assert "deleted" in result.stdout
    assert worktree.list_branches(git_repo, "agent-dispatch") == []


def test_worktrees_reports_an_empty_repository(tmp_config_dir, git_repo):
    result = runner.invoke(app, ["worktrees", "--cwd", str(git_repo)])

    assert result.exit_code == 0
    assert "no agent-dispatch worktrees or branches" in result.stdout


def _clone_repo(source: Path, target: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", str(source), str(target)],
        check=True,
        capture_output=True,
    )
    return target


def _record_task(task_id: str, status: TaskStatus, cwd: Path) -> None:
    async def save() -> None:
        settings = load_settings()
        storage = Storage(settings.server.data_dir / "dispatch.db")
        await storage.open()
        try:
            now = datetime.now(UTC)
            await storage.insert_task(
                TaskRecord(
                    task_id=task_id,
                    parent_task_id=None,
                    escalated_from=None,
                    root_agent=SourceAgent.cli,
                    source_agent=SourceAgent.cli,
                    hop=0,
                    request=DispatchRequest(task="test", cwd=str(cwd), source_agent="cli"),
                    status=status,
                    decision=None,
                    result=None,
                    log_path="",
                    created_at=now,
                    started_at=None,
                    finished_at=(
                        now if status in {TaskStatus.completed, TaskStatus.failed} else None
                    ),
                )
            )
        finally:
            await storage.close()

    asyncio.run(save())


def test_worktrees_all_lists_multiple_repositories_with_age_and_task_status(
    tmp_config_dir, git_repo, tmp_path
):
    from agent_dispatch.executors import worktree

    settings = load_settings()
    base = settings.server.data_dir / "worktrees"
    second_repo = _clone_repo(git_repo, tmp_path / "second-repo")
    first = worktree.create(git_repo, base / "task-finished", "agent-dispatch/finished")
    second = worktree.create(second_repo, base / "task-running", "agent-dispatch/running")
    old = datetime.now().timestamp() - 8 * 86400
    os.utime(first.path / ".git", (old, old))
    _record_task("task-finished", TaskStatus.completed, first.path)
    _record_task("task-running", TaskStatus.running, second.path)

    result = runner.invoke(app, ["worktrees", "--all"])

    assert result.exit_code == 0
    assert f"repository\t{git_repo}" in result.stdout
    assert f"repository\t{second_repo}" in result.stdout
    assert f"{first.path}\tagent-dispatch/finished\tage=8d\tstatus=completed" in result.stdout
    assert f"{second.path}\tagent-dispatch/running" in result.stdout
    assert "status=running" in result.stdout


def test_worktrees_all_clean_removes_only_safe_trees_and_missing_repositories(
    tmp_config_dir, git_repo, tmp_path
):
    from agent_dispatch.executors import worktree

    settings = load_settings()
    base = settings.server.data_dir / "worktrees"
    missing_repo = _clone_repo(git_repo, tmp_path / "missing-repo")
    finished = worktree.create(git_repo, base / "finished", "agent-dispatch/finished")
    running = worktree.create(git_repo, base / "running", "agent-dispatch/running")
    guarded = worktree.create(git_repo, base / "guarded", "agent-dispatch/guarded")
    missing = worktree.create(missing_repo, base / "missing", "agent-dispatch/missing")
    missing_running = worktree.create(
        missing_repo, base / "missing-running", "agent-dispatch/missing-running"
    )
    active_child_cwd = guarded.path / "child"
    active_child_cwd.mkdir()
    _record_task("finished", TaskStatus.completed, finished.path)
    _record_task("running", TaskStatus.running, running.path)
    _record_task("guarded", TaskStatus.completed, guarded.path)
    _record_task("active-child", TaskStatus.running, active_child_cwd)
    _record_task("missing", TaskStatus.completed, missing.path)
    _record_task("missing-running", TaskStatus.running, missing_running.path)
    missing_repo.rename(tmp_path / "moved-repo")

    result = runner.invoke(app, ["worktrees", "--all", "--clean"])

    assert result.exit_code == 0
    assert not finished.path.exists()
    assert running.path.exists()
    assert guarded.path.exists()
    assert not missing.path.exists()
    # Исчезнувший репозиторий не даёт права удалить дерево работающей задачи.
    assert missing_running.path.exists()
    assert f"{running.path}\tkept\tunfinished task (running)" in result.stdout
    assert f"{guarded.path}\tkept\tactive task cwd" in result.stdout
    assert f"{missing.path}\tremoved\trepository missing" in result.stdout
    assert f"{missing_running.path}\tkept\tunfinished task (running)" in result.stdout


def test_worktrees_all_clean_never_follows_a_symlink_out_of_the_base_dir(
    tmp_config_dir, git_repo, tmp_path
):
    from agent_dispatch.executors import worktree

    settings = load_settings()
    base = settings.server.data_dir / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    outside = worktree.create(git_repo, tmp_path / "elsewhere" / "tree", "feature/outside")
    (base / "evil").symlink_to(outside.path, target_is_directory=True)
    _record_task("evil", TaskStatus.completed, base / "evil")

    result = runner.invoke(app, ["worktrees", "--all", "--clean"])

    assert result.exit_code == 0
    assert outside.path.exists()
    assert "evil" not in result.stdout


def test_worktrees_all_clean_rereads_tasks_right_before_removal(
    tmp_config_dir, git_repo, monkeypatch
):
    # Между первым снимком базы и удалением демон принял задачу с cwd в дереве.
    from agent_dispatch import cli
    from agent_dispatch.executors import worktree

    settings = load_settings()
    base = settings.server.data_dir / "worktrees"
    tree = worktree.create(git_repo, base / "done", "agent-dispatch/done")
    snapshots = iter(
        [
            ({"done": "completed"}, []),
            ({"done": "completed", "late": "running"}, [str(tree.path / "sub")]),
        ]
    )

    async def index(_settings):
        return next(snapshots)

    monkeypatch.setattr(cli, "_worktree_task_index", index)

    result = runner.invoke(app, ["worktrees", "--all", "--clean"])

    assert result.exit_code == 0
    assert tree.path.exists()
    assert f"{tree.path}\tkept\tactive task cwd" in result.stdout


def test_worktrees_all_clean_revalidates_the_path_at_removal(
    tmp_config_dir, git_repo, tmp_path, monkeypatch
):
    # После инвентаризации каталог подменили симлинком на чужой worktree.
    import shutil

    from agent_dispatch import cli
    from agent_dispatch.executors import worktree

    settings = load_settings()
    base = settings.server.data_dir / "worktrees"
    tree = worktree.create(git_repo, base / "done", "agent-dispatch/done")
    outside = worktree.create(git_repo, tmp_path / "elsewhere" / "tree", "feature/outside")
    calls = 0

    async def index(_settings):
        nonlocal calls
        calls += 1
        if calls == 2:
            shutil.rmtree(tree.path)
            tree.path.symlink_to(outside.path, target_is_directory=True)
        return {"done": "completed"}, []

    monkeypatch.setattr(cli, "_worktree_task_index", index)

    result = runner.invoke(app, ["worktrees", "--all", "--clean"])

    assert result.exit_code == 0
    assert outside.path.exists()
    assert f"{tree.path}\tkept\tnot a plain directory" in result.stdout


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "serve",
        "mcp",
        "route",
        "dispatch",
        "status",
        "cancel",
        "executors",
        "feedback",
        "export",
        "doctor",
    ):
        assert command in result.stdout


@respx.mock
def test_dispatch_sends_review_kind(tmp_config_dir, monkeypatch):
    state = _state()
    _patch_autostart(monkeypatch, state)
    request = respx.post(f"http://127.0.0.1:{state.port}/tasks").mock(
        return_value=httpx.Response(200, json=_view())
    )
    result = runner.invoke(app, ["dispatch", "--kind", "review", "review the diff"])
    assert result.exit_code == 0
    assert json.loads(request.calls[0].request.content)["kind"] == "review"


@respx.mock
def test_cooldown_clear_sends_names(tmp_config_dir, monkeypatch):
    state = _state()
    _write_running_state(tmp_config_dir, state)
    request = respx.delete(f"http://127.0.0.1:{state.port}/executors/cooldowns").mock(
        return_value=httpx.Response(200, json={"cleared": ["claude/opus"]})
    )
    result = runner.invoke(app, ["cooldown-clear", "claude/opus", "claude/haiku"])
    assert result.exit_code == 0 and "cleared: claude/opus" in result.stdout
    assert request.calls[0].request.url.params.get_list("name") == ["claude/opus", "claude/haiku"]

    result = runner.invoke(app, ["cooldown-clear"])
    assert "name" not in request.calls[1].request.url.params
