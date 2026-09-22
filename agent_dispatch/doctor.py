from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

from agent_dispatch.config import Settings
from agent_dispatch.executors.base import check_cli_version
from agent_dispatch.executors.env import child_env
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import DispatchRequest, SourceAgent
from agent_dispatch.routing.claude_local import ClaudeLocalRouter
from agent_dispatch.routing.jev import JevRouter
from agent_dispatch.serve_state import is_alive, read_state


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


def _config_check() -> Check:
    directory = Path(
        os.environ.get("AGENT_DISPATCH_CONFIG_DIR", "~/.config/agent-dispatch")
    ).expanduser()
    path = directory / "config.yaml"
    detail = f"found {path}" if path.is_file() else f"defaults ({path})"
    return Check(name="config", ok=True, detail=detail)


async def _daemon_check(settings: Settings) -> Check:
    state = read_state(settings.server.data_dir)
    if state is None or not is_alive(state):
        return Check(name="daemon", ok=False, detail="not running")
    try:
        async with DispatchClient(state, timeout=5) as client:
            await client.health()
    except (DaemonUnavailable, RuntimeError) as exc:
        return Check(name="daemon", ok=False, detail=f"not running ({exc})")
    return Check(name="daemon", ok=True, detail=f"running pid {state.pid} port {state.port}")


async def run_checks(settings: Settings, online: bool = False) -> list[Check]:
    checks = [_config_check()]
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
    candidates = {
        name: executor.description for name, executor in settings.enabled_executors().items()
    }
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
