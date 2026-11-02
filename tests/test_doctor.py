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
    omp_mcp_env_check,
    omp_version_check,
    omp_x5_profile_check,
    run_checks,
    stale_files,
    worktrees_check,
)
from agent_dispatch.serve_state import ServeState, write_state

runner = CliRunner()
FAKE = Path(__file__).parent / "fakes" / "version_only.sh"
FAKE_OMP = Path(__file__).parent / "fakes" / "omp.sh"
JEV_RESPONSE = Path(__file__).parent / "fixtures" / "jev" / "response_ok.json"
OMP_HOP_VARS = ("AGENT_DISPATCH_HOP", "AGENT_DISPATCH_TASK_ID", "AGENT_DISPATCH_ROOT_AGENT")
# Все omp-исполнители дефолтного конфига: run_checks проверяет каждый, включая выключенных.
DEFAULT_OMP_EXECUTORS = (
    "omp/sol",
    "omp/terra",
    "omp/luna",
    "omp/x5-code-large",
    "omp/x5-small-coder",
    "omp/copilot-code-flash",
    "omp/copilot-code-flash-thinking",
)
GREEN_MCP_JSON = {
    "mcpServers": {
        "agent-dispatch": {
            "command": "agent-dispatch",
            "args": ["mcp"],
            "env": {
                "AGENT_DISPATCH_SOURCE_AGENT": "omp",
                **{var: var for var in OMP_HOP_VARS},
            },
        }
    }
}
GREEN_X5_PROFILE = """\
enabledModels:
  - copilot-x5/*
modelRoles:
  default: copilot-x5/x5-airun-code-large
  smol: copilot-x5/copilot-code-flash
  slow: copilot-x5/x5-airun-small-coder
"""


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
    omp_wrapper: str | None = None,
):
    lines = []
    for name in ("claude", *DEFAULT_OMP_EXECUTORS):
        if name == broken:
            command = "/nonexistent"
        elif name == "omp/sol" and omp_wrapper is not None:
            command = omp_wrapper
        else:
            command = str(FAKE)
        lines.append(f"  {name}:")
        # Адаптер и модель приходят из дефолтного конфига (merge по имени),
        # пользовательская запись подменяет только команду CLI.
        lines.append(f'    command: "{command}"')
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
def _omp_home(tmp_path_factory, monkeypatch):
    """Проверки omp смотрят во временный HOME с зелёным конфигом, а не в настоящий ~/.omp."""
    home = tmp_path_factory.mktemp("omp-home")
    omp = home / ".omp"
    agent = omp / "agent"
    agent.mkdir(parents=True)
    (agent / "mcp.json").write_text(json.dumps(GREEN_MCP_JSON))
    profile = omp / "profiles" / "x5" / "agent"
    profile.mkdir(parents=True)
    (profile / "config.yml").write_text(GREEN_X5_PROFILE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor_module, "OMP_COMMAND", str(FAKE_OMP))
    return omp


def _mcp_env(**env: str) -> dict:
    return {"mcpServers": {"agent-dispatch": {"command": "x", "env": env}}}


def test_omp_mcp_env_check_ok_when_hop_vars_forwarded(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(GREEN_MCP_JSON))

    check = omp_mcp_env_check(path)

    assert check.ok and "forwarded" in check.detail


def test_omp_mcp_env_check_lists_each_missing_var(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            _mcp_env(
                AGENT_DISPATCH_HOP="AGENT_DISPATCH_HOP",
                AGENT_DISPATCH_SOURCE_AGENT="codex",
            )
        )
    )

    check = omp_mcp_env_check(path)

    assert not check.ok
    assert "does not forward AGENT_DISPATCH_TASK_ID, AGENT_DISPATCH_ROOT_AGENT" in check.detail
    assert "AGENT_DISPATCH_SOURCE_AGENT=omp" in check.detail


def test_omp_mcp_env_check_skips_without_omp_config(tmp_path):
    check = omp_mcp_env_check(tmp_path / "mcp.json")
    assert check.ok and "skipped" in check.detail


def test_omp_mcp_env_check_fails_without_agent_dispatch_server(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    check = omp_mcp_env_check(path)

    # omp установлен, но сервер agent-dispatch не настроен: hop-переменные
    # проверить нечем, проверка падает, а не молча проходит.
    assert not check.ok and "no agent-dispatch" in check.detail


def test_omp_mcp_env_check_reports_broken_json(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{not json")

    check = omp_mcp_env_check(path)

    assert not check.ok and "cannot parse" in check.detail


def test_omp_mcp_env_check_uses_home_env(monkeypatch, tmp_path):
    agent = tmp_path / ".omp" / "agent"
    agent.mkdir(parents=True)
    (agent / "mcp.json").write_text(json.dumps(GREEN_MCP_JSON))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert doctor_module._omp_mcp_env_check().ok


def test_omp_version_check_ok_with_recent_version():
    check = omp_version_check(str(FAKE_OMP))
    assert check.ok and "18.16.0" in check.detail


def test_omp_version_check_fails_below_minimum(tmp_path, monkeypatch):
    old = tmp_path / "omp-old"
    old.write_text("#!/bin/sh\necho omp 18.14.2\n")
    old.chmod(0o755)

    check = omp_version_check(str(old))

    assert not check.ok
    assert "older than required 18.15.0" in check.detail


def test_omp_version_check_rejects_unparseable_output(tmp_path):
    broken = tmp_path / "omp-broken"
    broken.write_text("#!/bin/sh\necho omp\n")
    broken.chmod(0o755)

    check = omp_version_check(str(broken))

    assert not check.ok and "cannot parse" in check.detail


def test_omp_version_check_reports_missing_binary(tmp_path):
    check = omp_version_check(str(tmp_path / "no-such-omp"))
    assert not check.ok and "cannot run" in check.detail


def test_omp_version_check_uses_module_command(monkeypatch):
    # run_checks вызывает проверку без аргумента: команда читается из глобала,
    # чтобы тесты подменяли её одной строкой.
    monkeypatch.setattr(doctor_module, "OMP_COMMAND", str(FAKE_OMP))
    assert omp_version_check().ok


def test_omp_x5_profile_check_accepts_green_profile(_omp_home):
    check = omp_x5_profile_check(_omp_home / "profiles" / "x5" / "agent" / "config.yml")
    assert check.ok and "inside copilot-x5" in check.detail


def test_omp_x5_profile_check_fails_without_profile(_omp_home):
    (_omp_home / "profiles" / "x5" / "agent" / "config.yml").unlink()

    check = omp_x5_profile_check(_omp_home / "profiles" / "x5" / "agent" / "config.yml")

    assert not check.ok and "wide open" in check.detail


def test_omp_x5_profile_check_fails_with_role_outside_provider(_omp_home):
    path = _omp_home / "profiles" / "x5" / "agent" / "config.yml"
    path.write_text(
        "enabledModels:\n  - copilot-x5/*\nmodelRoles:\n  default: openrouter/some-model\n"
    )

    check = omp_x5_profile_check(path)

    assert not check.ok
    assert "периметр дырявый" in check.detail
    assert "modelRoles outside copilot-x5" in check.detail


def test_omp_x5_profile_check_fails_with_enabled_model_outside_provider(_omp_home):
    path = _omp_home / "profiles" / "x5" / "agent" / "config.yml"
    path.write_text(
        "enabledModels:\n  - copilot-x5/*\n  - openrouter/*\n"
        "modelRoles:\n  default: copilot-x5/x5-airun-code-large\n"
    )

    check = omp_x5_profile_check(path)

    assert not check.ok and "enabledModels outside copilot-x5" in check.detail


def test_omp_x5_profile_check_fails_without_enabled_models(_omp_home):
    path = _omp_home / "profiles" / "x5" / "agent" / "config.yml"
    path.write_text("modelRoles:\n  default: copilot-x5/x5-airun-code-large\n")

    check = omp_x5_profile_check(path)

    assert not check.ok and "enabledModels do not restrict" in check.detail


@pytest.mark.asyncio
async def test_run_checks_includes_omp_mcp_env_check(tmp_config_dir, _omp_home):
    path = _omp_home / "agent" / "mcp.json"
    path.write_text(json.dumps(_mcp_env(AGENT_DISPATCH_HOP="AGENT_DISPATCH_HOP")))
    settings = _configure(tmp_config_dir)

    checks = await run_checks(settings)

    check = next(c for c in checks if c.name == "omp-mcp-env")
    assert not check.ok and "AGENT_DISPATCH_TASK_ID" in check.detail


@pytest.mark.asyncio
async def test_executor_check_names_a_key_trapped_in_the_dispatch_env_file(
    tmp_config_dir, tmp_path
):
    # Wrapper agent-lb падает до `exec`, если ключа нет в его env. Ключ
    # в env-файле agent-dispatch туда не попадёт: child_env вырезает секреты.
    wrapper = tmp_path / "omp-agent-lb"
    wrapper.write_text(
        "#!/bin/sh\n"
        "echo 'omp-agent-lb: Missing environment variable: AGENT_LB_API_KEY' >&2\n"
        "exit 78\n"
    )
    wrapper.chmod(0o755)
    value = "lb-secret-value"  # pragma: allowlist secret
    settings = _configure(tmp_config_dir, omp_wrapper=str(wrapper))
    (tmp_config_dir / "env").write_text(f"AGENT_LB_API_KEY={value}\n")
    settings = load_settings()

    checks = await run_checks(settings)

    check = next(c for c in checks if c.name == "executor:omp/sol")
    assert not check.ok
    assert "AGENT_LB_API_KEY is in the agent-dispatch env file" in check.detail
    assert value not in check.detail


@pytest.mark.asyncio
async def test_executor_check_gives_no_env_file_hint_for_a_process_env_key(
    tmp_config_dir, tmp_path, monkeypatch
):
    # Ключ роутера только в окружении демона: совет «переложить из env-файла» был бы ложью.
    wrapper = tmp_path / "omp-agent-lb"
    wrapper.write_text(
        "#!/bin/sh\necho 'Missing environment variable: OPENROUTER_API_KEY' >&2\nexit 78\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")
    settings = _configure(tmp_config_dir, omp_wrapper=str(wrapper))

    checks = await run_checks(settings)

    check = next(c for c in checks if c.name == "executor:omp/sol")
    assert not check.ok and "agent-dispatch env file" not in check.detail


def test_worktrees_check_warns_about_old_trees_and_reports_total_size(tmp_config_dir, git_repo):
    from agent_dispatch.executors import worktree

    settings = _configure(tmp_config_dir)
    tree = worktree.create(
        git_repo,
        settings.server.data_dir / "worktrees" / "old-task",
        "agent-dispatch/old-task",
    )
    old = datetime.now().timestamp() - 8 * 86400
    os.utime(tree.path / ".git", (old, old))

    check = worktrees_check(settings)

    assert check.ok and check.warning
    assert "1 trees" in check.detail and "total" in check.detail
    assert "1 older than 7 days" in check.detail
    assert format_checks([check]).startswith("WARN  worktrees:")


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
    assert any(c.name == "executor:omp/sol" and c.ok and c.detail == "1.2.3" for c in checks)


@pytest.mark.asyncio
async def test_executor_check_reports_error(tmp_config_dir):
    settings = _configure(tmp_config_dir, broken="omp/sol")
    checks = await run_checks(settings)
    assert any(c.name == "executor:omp/sol" and not c.ok for c in checks)


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
    # Фейковый роутер возвращает omp/sol: старый исполнитель codex/sol из дефолтов удалён.
    claude = Path(__file__).parent / "fakes" / "claude_router_ok_omp.sh"
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
    _configure(tmp_config_dir, broken="omp/sol")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL  executor:omp/sol" in result.stdout


@respx.mock
def test_doctor_exits_zero_when_all_checks_are_green(tmp_config_dir):
    settings = _configure(tmp_config_dir, key="test-key")
    _mock_health(settings, _state())
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
