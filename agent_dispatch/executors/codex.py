from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors import workspace
from agent_dispatch.executors.base import (
    RunContext,
    check_cli_version,
    cwd_error_result,
    default_base_env,
)
from agent_dispatch.executors.process import run_cli
from agent_dispatch.executors.result_parser import normalize
from agent_dispatch.models import Availability, ExecutionResult, Usage
from agent_dispatch.schemas import load_agent_result_strict_schema


class CodexAdapter:
    def __init__(
        self, name: str, settings: ExecutorSettings, base_env: dict[str, str] | None = None
    ):
        self.name = name
        self.settings = settings
        self.command = settings.resolved_command
        self.base_env = base_env if base_env is not None else default_base_env()

    async def check(self) -> Availability:
        return await check_cli_version(self.name, self.command, self.base_env)

    async def execute(self, ctx: RunContext) -> ExecutionResult:
        try:
            before = workspace.snapshot(ctx.cwd)
        except subprocess.CalledProcessError:
            return cwd_error_result(self.name, self.settings.model, ctx.cwd, ctx.log_path)
        temp_dir = Path(tempfile.mkdtemp(prefix="agent-dispatch-codex-"))
        schema_path = temp_dir / "schema.json"
        last_path = temp_dir / "last.json"
        schema_path.write_text(json.dumps(load_agent_result_strict_schema()))
        argv = [
            self.command,
            "exec",
            "--json",
            "-C",
            ctx.cwd,
            "--output-schema",
            str(schema_path),
            "-o",
            str(last_path),
            *(["-m", self.settings.model] if self.settings.model else []),
            *self.settings.extra_args,
            "-",
        ]
        try:
            try:
                outcome = await run_cli(
                    argv,
                    cwd=ctx.cwd,
                    env=ctx.env,
                    stdin=ctx.prompt,
                    timeout_seconds=ctx.timeout_seconds,
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
            raw = None
            if last_path.is_file():
                try:
                    value = json.loads(last_path.read_text())
                    if isinstance(value, dict):
                        raw = value
                except (json.JSONDecodeError, OSError):
                    pass
            model = self.settings.model
            usage = None
            error_message = None
            for line in outcome.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    if event.get("type") in {"thread.started", "turn.started"} and event.get(
                        "model"
                    ):
                        model = event["model"]
                    if event.get("type") == "turn.completed" and isinstance(
                        event.get("usage"), dict
                    ):
                        u = event["usage"]
                        usage = Usage(
                            input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens")
                        )
                    if event.get("type") == "error":
                        error_message = event.get("message")
            result = normalize(raw, outcome, changed, self.name, model)
            if error_message and outcome.exit_code not in (0, None):
                result = result.model_copy(update={"error": error_message})
            if usage is not None:
                result = result.model_copy(update={"usage": usage})
            return result.model_copy(
                update={"meta": {**result.meta, "log_path": str(ctx.log_path)}}
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
