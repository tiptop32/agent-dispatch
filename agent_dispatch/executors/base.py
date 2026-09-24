from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors import workspace
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.models import Availability, ExecutionResult


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str
    timeout_seconds: float
    idle_timeout_seconds: float | None = None
    env: dict[str, str]
    log_path: Path
    task_id: str
    prompt: str


class ExecutorAdapter(Protocol):
    name: str

    async def check(self) -> Availability: ...

    async def execute(self, ctx: RunContext) -> ExecutionResult: ...


class BaseExecutorAdapter:
    #: CLI пишет события по ходу работы. Сторож молчания (`idle_timeout_seconds`)
    #: имеет смысл только для таких: CLI, который печатает всё одним куском в
    #: конце, он убивал бы на любой задаче длиннее лимита.
    streams_output: bool = True

    def __init__(
        self, name: str, settings: ExecutorSettings, base_env: dict[str, str] | None = None
    ):
        self.name = name
        self.settings = settings
        self.command = settings.resolved_command
        self.base_env = base_env if base_env is not None else default_base_env()

    async def check(self) -> Availability:
        return await check_cli_version(self.name, self.command, self.base_env)

    async def _execute_common(
        self,
        ctx: RunContext,
        argv: list[str],
        *,
        stdin: str | None,
        parse_result: Callable[[ProcessOutcome, list[str]], ExecutionResult],
        run_cli_fn: Callable[..., Awaitable[ProcessOutcome]],
    ) -> ExecutionResult:
        """Общий цикл CLI-адаптера: snapshot, запуск, snapshot, разбор вывода.

        `run_cli_fn` передаётся каждым адаптером из его собственного модуля, а не
        берётся отсюда по умолчанию: тесты подменяют `run_cli` именно в модуле
        адаптера, и дефолт здесь молча уводил бы вызов мимо подмены.
        """
        try:
            before = workspace.snapshot(ctx.cwd)
        except subprocess.CalledProcessError:
            return cwd_error_result(self.name, self.settings.model, ctx.cwd, ctx.log_path)
        try:
            outcome = await run_cli_fn(
                argv,
                cwd=ctx.cwd,
                env=ctx.env,
                stdin=stdin,
                timeout_seconds=ctx.timeout_seconds,
                idle_timeout_seconds=ctx.idle_timeout_seconds if self.streams_output else None,
                log_path=ctx.log_path,
            )
        except OSError as exc:
            return ExecutionResult(
                status="failed",
                executor=self.name,
                model=self.settings.model,
                summary="",
                error=f"cannot start {self.command}: {exc}",
                meta={"log_path": str(ctx.log_path)},
            )
        try:
            after = workspace.snapshot(ctx.cwd)
        except subprocess.CalledProcessError:
            return cwd_error_result(self.name, self.settings.model, ctx.cwd, ctx.log_path)
        changed = workspace.diff(before, after)
        result = parse_result(outcome, changed)
        return result.model_copy(update={"meta": {**result.meta, "log_path": str(ctx.log_path)}})


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
