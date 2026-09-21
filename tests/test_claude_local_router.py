import pytest

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest
from agent_dispatch.routing.base import RouterError
from agent_dispatch.routing.claude_local import ClaudeLocalRouter


@pytest.mark.asyncio
async def test_claude_local_ok():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {
                "backend": "claude_local",
                "claude_local": {"command": "tests/fakes/claude_router_ok.sh"},
            },
            "executors": {"codex": {"adapter": "codex"}, "claude": {"adapter": "claude"}},
        }
    )
    result = await ClaudeLocalRouter(s).decide(
        DispatchRequest(task="x", cwd="."), {"codex": "d", "claude": "c"}
    )
    assert result.executor == "codex"


@pytest.mark.asyncio
async def test_claude_local_bad():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_bad.sh"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    with pytest.raises(RouterError):
        await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})


@pytest.mark.asyncio
async def test_claude_local_confidence_and_router():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_ok.sh"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    result = await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
    assert result.router == "claude_local" and result.confidence == 0.8


@pytest.mark.asyncio
async def test_claude_local_unknown_executor():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_ok.sh"}},
            "executors": {"claude": {"adapter": "claude"}, "codex": {"adapter": "codex"}},
        }
    )
    with pytest.raises(RouterError, match="unknown executor"):
        await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"claude": "c"})


@pytest.mark.asyncio
async def test_claude_local_invalid_json():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_bad.sh"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    with pytest.raises(RouterError, match="invalid claude response"):
        await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})


@pytest.mark.asyncio
async def test_claude_local_env_keeps_home_and_removes_secret(tmp_path, monkeypatch):
    capture = tmp_path / "env.txt"
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    monkeypatch.setenv("OPENROUTER_API_KEY", "leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    s = Settings.model_validate(
        {
            "server": {"data_dir": str(tmp_path / "data")},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_capture.sh"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
    env = capture.read_text()
    assert "HOME=" in env and "OPENROUTER_API_KEY=" not in env
    # Маркер вложенной сессии Claude Code не должен доходить до claude -p.
    assert "CLAUDECODE=" not in env
    assert (tmp_path / "data/logs/router-claude-local.log").is_file()


async def test_claude_local_unwritable_log_dir_is_router_error(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("not a directory")
    s = Settings.model_validate(
        {
            "server": {"data_dir": str(blocker)},
            "router": {"claude_local": {"command": "tests/fakes/claude_router_ok.sh"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    with pytest.raises(RouterError):
        await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})


@pytest.mark.asyncio
async def test_claude_local_missing_command_is_router_error():
    s = Settings.model_validate(
        {
            "server": {"data_dir": ".test-data"},
            "router": {"claude_local": {"command": "/nonexistent/claude"}},
            "executors": {"codex": {"adapter": "codex"}},
        }
    )
    with pytest.raises(RouterError, match="cannot start"):
        await ClaudeLocalRouter(s).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
