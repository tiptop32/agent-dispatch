import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from agent_dispatch.cli import app
from agent_dispatch.config import load_settings
from agent_dispatch.doctor import Check, format_checks, run_checks
from agent_dispatch.serve_state import ServeState, write_state

runner = CliRunner()
FAKE = Path(__file__).parent / "fakes" / "version_only.sh"
JEV_RESPONSE = Path(__file__).parent / "fixtures" / "jev" / "response_ok.json"


def _configure(
    tmp_config_dir: Path,
    *,
    broken: str | None = None,
    key: str | None = None,
    backend: str = "jev",
    claude_command: str = "claude",
):
    lines = []
    for name in ("claude", "codex", "opencode/kimi"):
        command = "/nonexistent" if name == broken else str(FAKE)
        lines.extend([f"  {name}:", f'    command: "{command}"'])
    (tmp_config_dir / "config.yaml").write_text(
        f"router:\n  backend: {backend}\n  claude_local:\n    command: {claude_command!r}\n"
        + "executors:\n"
        + "\n".join(lines)
        + "\n"
    )
    if key is not None:
        (tmp_config_dir / "env").write_text(f"OPENROUTER_API_KEY={key}\n")
    return load_settings()


def _state(port: int = 17434) -> ServeState:
    return ServeState(pid=os.getpid(), port=port, token="secret", started_at=datetime.now(UTC))


def _mock_health(settings, state: ServeState):
    write_state(settings.server.data_dir, state)
    return respx.get(f"http://127.0.0.1:{state.port}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )


@pytest.mark.asyncio
@respx.mock
async def test_config_check_reports_found_path(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    _mock_health(settings, _state())
    checks = await run_checks(settings)
    config = checks[0]
    assert config.ok and "found" in config.detail and "config.yaml" in config.detail


@pytest.mark.asyncio
@respx.mock
async def test_env_check_reports_set_without_secret_value(tmp_config_dir):
    secret = "super-secret-value"  # pragma: allowlist secret
    settings = _configure(tmp_config_dir, key=secret)
    _mock_health(settings, _state())
    output = format_checks(await run_checks(settings))
    assert "env: OPENROUTER_API_KEY: set" in output
    assert "chars" not in output
    assert secret not in output


@pytest.mark.asyncio
@respx.mock
async def test_env_check_reports_not_set(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    _mock_health(settings, _state())
    output = format_checks(await run_checks(settings))
    assert "env: OPENROUTER_API_KEY: not set" in output


@pytest.mark.asyncio
@respx.mock
async def test_daemon_check_reports_running_pid_and_port(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    state = _state()
    _mock_health(settings, state)
    output = format_checks(await run_checks(settings))
    assert f"daemon: running pid {state.pid} port {state.port}" in output


@pytest.mark.asyncio
async def test_daemon_check_reports_not_running(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    output = format_checks(await run_checks(settings))
    assert "FAIL  daemon: not running" in output


@pytest.mark.asyncio
async def test_executor_check_reports_version(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    checks = await run_checks(settings)
    assert any(c.name == "executor:codex" and c.ok and c.detail == "1.2.3" for c in checks)


@pytest.mark.asyncio
async def test_executor_check_reports_error(tmp_config_dir):
    settings = _configure(tmp_config_dir, broken="codex")
    checks = await run_checks(settings)
    assert any(c.name == "executor:codex" and not c.ok for c in checks)


@pytest.mark.asyncio
async def test_jev_check_is_skipped_offline(tmp_config_dir):
    settings = _configure(tmp_config_dir)
    checks = await run_checks(settings)
    assert checks[-1] == Check(name="jev", ok=True, detail="skipped (use --online)")


@pytest.mark.asyncio
@respx.mock
async def test_jev_check_calls_router_online(tmp_config_dir):
    settings = _configure(tmp_config_dir, key="test-key")
    respx.post(settings.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=json.loads(JEV_RESPONSE.read_text()))
    )
    checks = await run_checks(settings, online=True)
    assert checks[-1].ok and checks[-1].detail.startswith("ok executor=")


@pytest.mark.asyncio
@respx.mock
async def test_claude_local_backend_does_not_require_jev_or_call_it(tmp_config_dir):
    claude = Path(__file__).parent / "fakes" / "claude_router_ok.sh"
    settings = _configure(tmp_config_dir, backend="claude_local", claude_command=str(claude))
    _mock_health(settings, _state())
    checks = await run_checks(settings, online=True)
    assert all(check.ok for check in checks)
    assert checks[1] == Check(name="env", ok=True, detail="not required for claude_local")
    assert checks[-1].name == "jev" and checks[-1].ok
    assert "claude_local" in checks[-1].detail


def test_doctor_json_is_parseable(tmp_config_dir):
    _configure(tmp_config_dir)
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and {"name", "ok", "detail"} <= payload[0].keys()


def test_doctor_exits_one_when_any_check_fails(tmp_config_dir):
    _configure(tmp_config_dir, broken="codex")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL  executor:codex" in result.stdout


@respx.mock
def test_doctor_exits_zero_when_all_checks_are_green(tmp_config_dir):
    settings = _configure(tmp_config_dir, key="test-key")
    _mock_health(settings, _state())
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
