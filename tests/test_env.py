from agent_dispatch.config import ExecutorSettings, RoutingSettings, Settings
from agent_dispatch.executors.env import child_env


def settings() -> Settings:
    return Settings(
        executors={"codex": ExecutorSettings(adapter="codex")},
        routing=RoutingSettings(fallback_executor="codex"),
    )


def test_child_env_strips_claude_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("HOME", "/home/test")
    env = child_env(settings())
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["HOME"] == "/home/test"


def test_child_env_strips_configured_secrets(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = child_env(settings())
    assert "OPENROUTER_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_child_env_applies_extra_last(monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_HOP", "0")
    env = child_env(settings(), {"AGENT_DISPATCH_HOP": "1"})
    assert env["AGENT_DISPATCH_HOP"] == "1"
