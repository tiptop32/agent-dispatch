from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
import yaml
from pydantic import BaseModel

import agent_dispatch
from agent_dispatch.config import Settings
from agent_dispatch.executors import worktree
from agent_dispatch.executors.base import check_cli_version
from agent_dispatch.executors.env import child_env
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import DispatchRequest, SourceAgent
from agent_dispatch.routing.claude_local import ClaudeLocalRouter
from agent_dispatch.routing.jev import JevRouter
from agent_dispatch.serve_state import is_running, read_state


class Check(BaseModel):
    name: str
    ok: bool
    detail: str
    warning: bool = False


def stale_files(installed: Path, source: Path) -> list[str]:
    suffixes = {".py", ".yaml", ".yml", ".json"}

    def files(root: Path) -> dict[str, Path]:
        return {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in suffixes
            and "__pycache__" not in path.relative_to(root).parts
        }

    installed_files = files(installed)
    source_files = files(source)
    different = installed_files.keys() ^ source_files.keys()
    for relative in installed_files.keys() & source_files.keys():
        installed_hash = hashlib.sha256(installed_files[relative].read_bytes()).digest()
        source_hash = hashlib.sha256(source_files[relative].read_bytes()).digest()
        if installed_hash != source_hash:
            different.add(relative)
    return sorted(different)


def install_check(direct_url: dict | None, installed_pkg: Path) -> Check:
    if direct_url is None:
        return Check(name="install", ok=True, detail="not a local directory install, skipped")

    parsed = urlparse(direct_url["url"])
    source = Path(url2pathname(parsed.path))
    if direct_url["dir_info"].get("editable"):
        return Check(name="install", ok=True, detail=f"editable {source}")

    source_pkg = source / "agent_dispatch"
    if not source_pkg.is_dir():
        return Check(name="install", ok=True, detail=f"source {source} not found, skipped")

    stale = stale_files(installed_pkg, source_pkg)
    if not stale:
        return Check(name="install", ok=True, detail=f"copy matches {source}")

    shown = ", ".join(stale[:3]) + (", ..." if len(stale) > 3 else "")
    detail = (
        f"installed copy differs from {source} in {len(stale)} files ({shown}): "
        f"run uv tool install --reinstall {source}"
    )
    return Check(name="install", ok=False, detail=detail)


def _install_check() -> Check:
    try:
        installed_pkg = Path(agent_dispatch.__file__).parent
        try:
            distribution = importlib.metadata.distribution("agent-dispatch")
        except importlib.metadata.PackageNotFoundError:
            return install_check(None, installed_pkg)
        try:
            raw_direct_url = distribution.read_text("direct_url.json")
        except FileNotFoundError:
            raw_direct_url = None
        direct_url = json.loads(raw_direct_url) if raw_direct_url is not None else None
        return install_check(direct_url, installed_pkg)
    except Exception as exc:  # doctor must never crash while inspecting its own install
        return Check(name="install", ok=True, detail=f"check failed: {exc}")


def _config_check() -> Check:
    directory = Path(
        os.environ.get("AGENT_DISPATCH_CONFIG_DIR", "~/.config/agent-dispatch")
    ).expanduser()
    path = directory / "config.yaml"
    detail = f"found {path}" if path.is_file() else f"defaults ({path})"
    return Check(name="config", ok=True, detail=detail)


#: omp не передаёт родительское окружение MCP-серверам по умолчанию: без этих
#: переменных вложенный dispatch исполнителя приходит с hop=0 и без родителя.
OMP_DISPATCH_VARS = (
    "AGENT_DISPATCH_HOP",
    "AGENT_DISPATCH_TASK_ID",
    "AGENT_DISPATCH_ROOT_AGENT",
)
OMP_SOURCE_VAR = "AGENT_DISPATCH_SOURCE_AGENT"
OMP_SOURCE_VALUE = "omp"
#: Минимальная версия omp, с которой работает миграция и профиль x5.
OMP_MIN_VERSION = "18.15.0"
OMP_COMMAND = "omp"
X5_PROFILE_PROVIDER = "copilot-x5/"


def _omp_dir() -> Path:
    """Каталог установки omp; HOME берём из окружения, чтобы тесты подменяли его."""
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".omp"


def omp_mcp_env_check(path: Path) -> Check:
    """Проверяет, что MCP-сервер agent-dispatch в omp получает hop-переменные."""
    name = "omp-mcp-env"
    if not path.is_file():
        return Check(name=name, ok=True, detail=f"no omp mcp config ({path}), skipped")
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return Check(name=name, ok=False, detail=f"cannot parse {path}: {exc}")
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    server = servers.get("agent-dispatch") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return Check(name=name, ok=False, detail=f"{path}: no agent-dispatch MCP server")
    env = server.get("env")
    env = env if isinstance(env, dict) else {}
    # Переменная пробрасывается записью вида "VAR": "VAR": имя и значение совпадают.
    missing = [var for var in OMP_DISPATCH_VARS if env.get(var) != var]
    if env.get(OMP_SOURCE_VAR) != OMP_SOURCE_VALUE:
        missing.append(f"{OMP_SOURCE_VAR}={OMP_SOURCE_VALUE}")
    if not missing:
        return Check(name=name, ok=True, detail=f"{path}: hop vars forwarded, source=omp")
    return Check(
        name=name,
        ok=False,
        detail=(
            f"{path}: mcpServers.agent-dispatch.env does not forward {', '.join(missing)}; "
            f"omp does not pass the parent env to MCP servers by default, add under "
            f'mcpServers.agent-dispatch.env entries like "{OMP_DISPATCH_VARS[0]}": '
            f'"{OMP_DISPATCH_VARS[0]}" and {OMP_SOURCE_VAR}: "{OMP_SOURCE_VALUE}"'
        ),
    )


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)+", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _version_at_least(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    width = max(len(version), len(minimum))
    padded = version + (0,) * (width - len(version))
    padded_minimum = minimum + (0,) * (width - len(minimum))
    return padded >= padded_minimum


def omp_version_check(command: str | None = None) -> Check:
    """Проверяет, что CLI omp установлен и не старее зафиксированного минимума."""
    name = "omp-version"
    binary = command or OMP_COMMAND
    try:
        outcome = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name=name, ok=False, detail=f"cannot run {binary} --version: {exc}")
    if outcome.returncode != 0:
        stderr = outcome.stderr.strip() or f"exit code {outcome.returncode}"
        return Check(name=name, ok=False, detail=f"{binary} --version failed: {stderr}")
    stdout = outcome.stdout.strip()
    version = stdout.splitlines()[0] if stdout else ""
    parsed = _version_tuple(version)
    if parsed is None:
        return Check(name=name, ok=False, detail=f"cannot parse version from {version!r}")
    minimum = _version_tuple(OMP_MIN_VERSION) or ()
    if not _version_at_least(parsed, minimum):
        found = ".".join(str(part) for part in parsed)
        return Check(
            name=name,
            ok=False,
            detail=f"omp {found} is older than required {OMP_MIN_VERSION}: update oh-my-pi",
        )
    return Check(name=name, ok=True, detail=f"omp {version} ({OMP_MIN_VERSION} required)")


def omp_x5_profile_check(path: Path) -> Check:
    """Профиль x5 должен держать все роли и enabledModels внутри copilot-x5."""
    name = "omp-x5-profile"
    if not path.is_file():
        return Check(
            name=name,
            ok=False,
            detail=(
                f"{path}: x5 profile not found, corporate perimeter is wide open; "
                "install the x5 profile from omp-setup"
            ),
        )
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return Check(name=name, ok=False, detail=f"cannot parse {path}: {exc}")
    if not isinstance(config, dict):
        return Check(name=name, ok=False, detail=f"{path}: top-level must be a mapping")
    problems: list[str] = []
    roles = config.get("modelRoles")
    roles = roles if isinstance(roles, dict) else {}
    if not roles:
        problems.append("modelRoles are not set")
    else:
        outside_roles = sorted(
            f"{role}={roles[role]}"
            for role, value in roles.items()
            if not (isinstance(value, str) and value.startswith(X5_PROFILE_PROVIDER))
        )
        if outside_roles:
            problems.append("modelRoles outside copilot-x5: " + ", ".join(outside_roles))
    enabled = config.get("enabledModels")
    if not isinstance(enabled, list) or not enabled:
        problems.append("enabledModels do not restrict providers")
    else:
        outside_enabled = sorted(
            model
            for model in enabled
            if not (isinstance(model, str) and model.startswith(X5_PROFILE_PROVIDER))
        )
        if outside_enabled:
            problems.append("enabledModels outside copilot-x5: " + ", ".join(outside_enabled))
    if problems:
        return Check(
            name=name, ok=False, detail=f"{path}: периметр дырявый: " + "; ".join(problems)
        )
    return Check(
        name=name, ok=True, detail=f"{path}: all roles and enabledModels inside copilot-x5"
    )


def _omp_mcp_env_check() -> Check:
    return omp_mcp_env_check(_omp_dir() / "agent" / "mcp.json")


def _omp_version_check() -> Check:
    return omp_version_check()


def _omp_x5_profile_check() -> Check:
    return omp_x5_profile_check(_omp_dir() / "profiles" / "x5" / "agent" / "config.yml")


def _trapped_secret_hint(settings: Settings, error: str) -> str:
    """Подсказка, если CLI не нашёл ключ, который лежит в env-файле agent-dispatch.

    `child_env` не пускает секреты в env исполнителей, и CLI падает с именем
    ключа в stderr. Значение ключа не печатается.
    """
    # Только env-файл agent-dispatch (`Settings.secrets`): ключ из окружения
    # процесса демона туда не относится, и совет переложить его был бы ложным.
    trapped = sorted(
        name
        for name, value in settings.secrets.items()
        if name in error and value.get_secret_value()
    )
    if not trapped:
        return ""
    return (
        f"; {', '.join(trapped)} is in the agent-dispatch env file, but child_env keeps "
        "secrets out of executor env: put it where the CLI reads it "
        "(its own config file or the daemon process environment)"
    )


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def worktrees_check(settings: Settings) -> Check:
    """Сводит количество, размер и возраст деревьев во всём каталоге данных."""
    try:
        trees = worktree.list_stored_worktrees(settings.worktree_base)
    except worktree.WorktreeError as exc:
        return Check(name="worktrees", ok=False, detail=str(exc))
    old = sum(tree.age_seconds >= 7 * 86400 for tree in trees)
    total = sum(worktree.directory_size(tree.path) for tree in trees)
    return Check(
        name="worktrees",
        ok=True,
        warning=old > 0,
        detail=f"{len(trees)} trees, {_format_size(total)} total, {old} older than 7 days",
    )


async def _daemon_check(settings: Settings) -> Check:
    state = read_state(settings.server.data_dir)
    if state is None or not is_running(state):
        return Check(name="daemon", ok=False, detail="not running")
    try:
        async with DispatchClient(state, timeout=5) as client:
            await client.health()
    except (DaemonUnavailable, RuntimeError) as exc:
        return Check(name="daemon", ok=False, detail=f"not running ({exc})")
    return Check(name="daemon", ok=True, detail=f"running pid {state.pid} port {state.port}")


async def run_checks(settings: Settings, online: bool = False) -> list[Check]:
    checks = [_config_check(), _install_check()]
    if settings.router.backend == "claude_local":
        checks.append(Check(name="env", ok=True, detail="not required for claude_local"))
    else:
        key_name = settings.router.jev.api_key_env
        key = settings.secret(key_name)
        checks.append(
            Check(
                name="env",
                ok=bool(key),
                detail=f"{key_name}: set" if key else f"{key_name}: not set",
            )
        )
    checks.append(await _daemon_check(settings))
    checks.append(_omp_mcp_env_check())
    checks.append(_omp_version_check())
    checks.append(_omp_x5_profile_check())
    checks.append(worktrees_check(settings))
    safe_env = child_env(settings)
    for name, executor in settings.executors.items():
        availability = await check_cli_version(name, executor.resolved_command, safe_env)
        error = availability.error or "unknown error"
        checks.append(
            Check(
                name=f"executor:{name}",
                ok=availability.available,
                detail=availability.version or error + _trapped_secret_hint(settings, error),
            )
        )
    if not online:
        checks.append(Check(name="jev", ok=True, detail="skipped (use --online)"))
        return checks

    started = time.monotonic()
    candidates = settings.enabled_executors()
    try:
        async with httpx.AsyncClient() as client:
            router = (
                ClaudeLocalRouter(settings)
                if settings.router.backend == "claude_local"
                else JevRouter(settings, client)
            )
            decision = await router.decide(
                DispatchRequest(task="ping", cwd=".", source_agent=SourceAgent.cli), candidates
            )
        elapsed = time.monotonic() - started
        router_name = settings.router.backend
        detail = f"ok executor={decision.executor} {elapsed:.1f}s"
        if router_name == "claude_local":
            detail = f"ok router={router_name} executor={decision.executor} {elapsed:.1f}s"
        checks.append(Check(name="jev", ok=True, detail=detail))
    except Exception as exc:  # doctor must report every router failure, not crash
        checks.append(Check(name="jev", ok=False, detail=str(exc)))
    return checks


def format_checks(checks: list[Check]) -> str:
    return "\n".join(
        f"{'FAIL' if not check.ok else 'WARN' if check.warning else 'ok'}  "
        f"{check.name}: {check.detail}"
        for check in checks
    )
