from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable

from agent_dispatch.config import Settings
from agent_dispatch.executors.env import child_env
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.serve_state import ServeState, is_alive, read_state


async def ensure_daemon(
    settings: Settings,
    *,
    deadline_seconds: float = 5.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    popen=subprocess.Popen,
    health: Callable[[ServeState], Awaitable[bool]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ServeState:
    data_dir = settings.server.data_dir

    async def healthy(state: ServeState) -> bool:
        if health is not None:
            return await health(state)
        try:
            await DispatchClient(state, 5).health()
        except (DaemonUnavailable, RuntimeError):
            return False
        return True

    state = read_state(data_dir)
    if state is not None and is_alive(state) and await healthy(state):
        return state

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "serve.log"
    log_file = log_path.open("ab")
    try:
        popen(
            [sys.executable, "-m", "agent_dispatch.cli", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_env(settings),
        )
    finally:
        log_file.close()

    started = clock()
    while clock() - started <= deadline_seconds:
        state = read_state(data_dir)
        if state is not None and await healthy(state):
            return state
        await sleep(0.1)
    raise DaemonUnavailable(f"daemon did not start within {deadline_seconds:g} s, see {log_path}")
