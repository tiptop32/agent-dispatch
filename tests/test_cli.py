import json
import os
from datetime import UTC, datetime

import httpx
import respx
from typer.testing import CliRunner

from agent_dispatch.cli import app
from agent_dispatch.mcp import autostart
from agent_dispatch.serve_state import ServeState, write_state

runner = CliRunner()


def _state(port: int = 17433, token: str = "secret") -> ServeState:
    return ServeState(pid=os.getpid(), port=port, token=token, started_at=datetime.now(UTC))


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
