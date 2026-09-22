from __future__ import annotations

import json
import subprocess

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors import workspace
from agent_dispatch.executors.base import (
    RunContext,
    check_cli_version,
    cwd_error_result,
    default_base_env,
)
from agent_dispatch.executors.process import run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize
from agent_dispatch.models import Availability, ExecutionResult, Usage


class OpenCodeAdapter:
    def __init__(
        self, name: str, settings: ExecutorSettings, base_env: dict[str, str] | None = None
    ):
        if settings.model is None:
            raise ValueError("opencode executor requires model")
        self.name = name
        self.settings = settings
        self.command = settings.resolved_command
        self.base_env = base_env if base_env is not None else default_base_env()

    async def check(self) -> Availability:
        return await check_cli_version(self.name, self.command, self.base_env)

    async def execute(self, ctx: RunContext):
        try:
            before = workspace.snapshot(ctx.cwd)
        except subprocess.CalledProcessError:
            return cwd_error_result(self.name, self.settings.model, ctx.cwd, ctx.log_path)
        argv = [
            self.command,
            "run",
            "--format",
            "json",
            "--dir",
            ctx.cwd,
            "--model",
            self.settings.model,
            *self.settings.extra_args,
            ctx.prompt,
        ]
        try:
            outcome = await run_cli(
                argv,
                cwd=ctx.cwd,
                env=ctx.env,
                stdin=None,
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
        text_parts = []
        input_tokens = output_tokens = 0
        cost = 0.0
        error_message = None
        for line in outcome.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "text" and isinstance(event.get("part"), dict):
                text_parts.append(str(event["part"].get("text", "")))
            elif event.get("type") == "step_finish" and isinstance(event.get("part"), dict):
                part = event["part"]
                tokens = part.get("tokens") or {}
                input_tokens += int(tokens.get("input", 0))
                output_tokens += int(tokens.get("output", 0))
                cost += float(part.get("cost", 0) or 0)
            elif event.get("type") == "error":
                error_message = ((event.get("error") or {}).get("data") or {}).get("message")
        text = "".join(text_parts)
        result = normalize(
            extract_result_block(text), outcome, changed, self.name, self.settings.model
        )
        if error_message:
            result = result.model_copy(update={"status": "failed", "error": error_message})
        if input_tokens or output_tokens or cost:
            result = result.model_copy(
                update={
                    "usage": Usage(
                        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost
                    )
                }
            )
        return result.model_copy(update={"meta": {**result.meta, "log_path": str(ctx.log_path)}})
