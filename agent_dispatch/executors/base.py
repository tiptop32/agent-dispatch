from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.process import run_cli
from agent_dispatch.models import Availability, ExecutionResult


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str
    timeout_seconds: float
    env: dict[str, str]
    log_path: Path
    task_id: str
    prompt: str


class ExecutorAdapter(Protocol):
    name: str

    async def check(self) -> Availability: ...

    async def execute(self, ctx: RunContext) -> ExecutionResult: ...


def default_base_env() -> dict[str, str]:
    """Env для check() без Settings: секреты по маске и маркеры сессии вырезаны."""
    return child_env(None)


async def check_cli_version(
    name: str,
    command: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 15,
) -> Availability:
    path = Path(tempfile.gettempdir()) / f"agent-dispatch-check-{name}.log"
    try:
        outcome = await run_cli(
            [command, "--version"],
            cwd=".",
            env=env if env is not None else default_base_env(),
            stdin=None,
            timeout_seconds=timeout_seconds,
            log_path=path,
        )
        if outcome.exit_code == 0:
            version = next(
                (line.strip() for line in outcome.stdout.splitlines() if line.strip()), None
            )
            return Availability(available=True, version=version, checked_at=datetime.now(UTC))
        return Availability(
            available=False,
            error=outcome.stderr or f"exit code {outcome.exit_code}",
            checked_at=datetime.now(UTC),
        )
    except (OSError, TimeoutError) as exc:
        return Availability(available=False, error=str(exc), checked_at=datetime.now(UTC))


def cwd_error_result(name: str, model: str | None, cwd: str, log_path: Path) -> ExecutionResult:
    return ExecutionResult(
        status="failed",
        executor=name,
        model=model,
        summary="",
        error=f"cwd is not a git repository: {cwd}",
        meta={"log_path": str(log_path)},
    )
