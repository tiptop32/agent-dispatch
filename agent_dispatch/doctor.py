from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
from pydantic import BaseModel

import agent_dispatch
from agent_dispatch.config import Settings
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
    safe_env = child_env(settings)
    for name, executor in settings.executors.items():
        availability = await check_cli_version(name, executor.resolved_command, safe_env)
        checks.append(
            Check(
                name=f"executor:{name}",
                ok=availability.available,
                detail=availability.version or availability.error or "unknown error",
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
        f"{'ok' if check.ok else 'FAIL'}  {check.name}: {check.detail}" for check in checks
    )
