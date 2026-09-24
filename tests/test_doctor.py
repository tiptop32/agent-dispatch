import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from agent_dispatch import doctor as doctor_module
from agent_dispatch.cli import app
from agent_dispatch.config import load_settings
from agent_dispatch.doctor import (
    Check,
    _install_check,
    format_checks,
    install_check,
    run_checks,
    stale_files,
)
from agent_dispatch.serve_state import ServeState, write_state

runner = CliRunner()
FAKE = Path(__file__).parent / "fakes" / "version_only.sh"
JEV_RESPONSE = Path(__file__).parent / "fixtures" / "jev" / "response_ok.json"


def _package_tree(root: Path, files: dict[str, str]) -> Path:
    package = root / "agent_dispatch"
    for relative, content in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package


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


@pytest.fixture(autouse=True)
def _daemon_port():
    """Порт фальшивого демона занят по-настоящему: `is_running` смотрит не только на pid."""
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 17434))
        holder.listen(1)
        yield


def _mock_health(settings, state: ServeState):
    write_state(settings.server.data_dir, state)
    return respx.get(f"http://127.0.0.1:{state.port}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )


def test_stale_files_returns_empty_for_identical_trees(tmp_path):
    installed = _package_tree(tmp_path / "installed", {"main.py": "same", "config.yaml": "x: 1"})
    source = _package_tree(tmp_path / "source", {"main.py": "same", "config.yaml": "x: 1"})

    assert stale_files(installed, source) == []


def test_stale_files_reports_changed_and_one_sided_files(tmp_path):
    installed = _package_tree(tmp_path / "installed", {"changed.py": "old", "installed.json": "{}"})
    source = _package_tree(
        tmp_path / "source", {"changed.py": "new", "source.yml": "enabled: true"}
    )

    assert stale_files(installed, source) == ["changed.py", "installed.json", "source.yml"]


def test_stale_files_ignores_pycache_and_pyc(tmp_path):
    installed = _package_tree(
        tmp_path / "installed", {"__pycache__/main.py": "old", "main.pyc": "old"}
    )
    source = _package_tree(tmp_path / "source", {"__pycache__/main.py": "new", "main.pyc": "new"})

    assert stale_files(installed, source) == []


def test_install_check_skips_non_local_install(tmp_path):
    assert install_check(None, tmp_path) == Check(
        name="install", ok=True, detail="not a local directory install, skipped"
    )


def test_install_check_accepts_editable_install(tmp_path):
    source = tmp_path / "source"

    check = install_check(
        {"url": source.as_uri(), "dir_info": {"editable": True}}, tmp_path / "installed"
    )

    assert check == Check(name="install", ok=True, detail=f"editable {source}")


def test_install_check_skips_missing_source(tmp_path):
    source = tmp_path / "missing"

    check = install_check({"url": source.as_uri(), "dir_info": {}}, tmp_path / "installed")

    assert check == Check(name="install", ok=True, detail=f"source {source} not found, skipped")


def test_install_check_accepts_matching_copy(tmp_path):
    installed = _package_tree(tmp_path / "installed", {"main.py": "same"})
    source = tmp_path / "source"
    _package_tree(source, {"main.py": "same"})

    check = install_check({"url": source.as_uri(), "dir_info": {}}, installed)

    assert check == Check(name="install", ok=True, detail=f"copy matches {source}")


def test_install_check_reports_stale_copy_and_reinstall_command(tmp_path):
    installed = _package_tree(tmp_path / "installed", {"main.py": "old"})
    source = tmp_path / "source"
    _package_tree(source, {"main.py": "new"})

    check = install_check({"url": source.as_uri(), "dir_info": {}}, installed)

    assert not check.ok
    assert "in 1 files (main.py)" in check.detail
    assert f"uv tool install --reinstall {source}" in check.detail


def test_install_check_limits_stale_file_details_to_three(tmp_path):
    installed = _package_tree(tmp_path / "installed", {})
    source = tmp_path / "source"
    _package_tree(source, {f"file_{number}.py": "new" for number in range(4)})

    check = install_check({"url": source.as_uri(), "dir_info": {}}, installed)

    assert "in 4 files (file_0.py, file_1.py, file_2.py, ...)" in check.detail


def test_install_check_decodes_percent_encoded_source_path(tmp_path):
    source = tmp_path / "source with space"
    installed = _package_tree(tmp_path / "installed", {"main.py": "same"})
    _package_tree(source, {"main.py": "same"})

    check = install_check({"url": source.as_uri(), "dir_info": {}}, installed)

    assert check == Check(name="install", ok=True, detail=f"copy matches {source}")


def test_install_check_decodes_the_source_path_exactly_once(tmp_path):
    # Буквальный `%20` в имени каталога кодируется в URL как `%2520`: двойное
    # раскодирование превратило бы его в пробел и увело проверку в чужой путь.
    source = tmp_path / "literal%20percent"
    installed = _package_tree(tmp_path / "installed", {"main.py": "same"})
    _package_tree(source, {"main.py": "same"})

    check = install_check({"url": source.as_uri(), "dir_info": {}}, installed)

    assert check == Check(name="install", ok=True, detail=f"copy matches {source}")


def test_install_check_wrapper_never_raises(monkeypatch):
    def fail(_name):
        raise RuntimeError("broken metadata")

    monkeypatch.setattr(doctor_module.importlib.metadata, "distribution", fail)

    assert _install_check() == Check(
        name="install", ok=True, detail="check failed: broken metadata"
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
async def test_run_checks_contains_install_check(tmp_config_dir):
    settings = _configure(tmp_config_dir)

    checks = await run_checks(settings)

    assert any(check.name == "install" for check in checks)


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
    assert checks[2] == Check(name="env", ok=True, detail="not required for claude_local")
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
