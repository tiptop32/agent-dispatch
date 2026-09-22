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


class ClaudeAdapter:
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
        argv = [
            self.command,
            "-p",
            "--output-format",
            "json",
            "--add-dir",
            ctx.cwd,
            *self.settings.extra_args,
        ]
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
        model = self.settings.model
        usage = None
        raw = None
        result_text = ""
        try:
            payload = json.loads(outcome.stdout)
            if isinstance(payload, dict):
                result_text = str(payload.get("result", ""))
                raw = extract_result_block(result_text)
                model_usage = payload.get("modelUsage") or {}
                if model_usage:
                    model = max(
                        model_usage, key=lambda key: model_usage[key].get("outputTokens", 0)
                    )
                    usage = Usage(
                        input_tokens=sum(
                            int(v.get("inputTokens", 0)) for v in model_usage.values()
                        ),
                        output_tokens=sum(
                            int(v.get("outputTokens", 0)) for v in model_usage.values()
                        ),
                        cost_usd=payload.get("total_cost_usd"),
                    )
                elif payload.get("total_cost_usd") is not None:
                    usage = Usage(cost_usd=payload["total_cost_usd"])
                is_error = bool(payload.get("is_error"))
            else:
                is_error = False
        except (json.JSONDecodeError, TypeError):
            is_error = False
        result = normalize(raw, outcome, changed, self.name, model)
        if is_error:
            result = result.model_copy(
                update={"status": "failed", "summary": "", "error": result_text[:2000]}
            )
        if usage is not None:
            result = result.model_copy(update={"usage": usage})
        result = result.model_copy(update={"meta": {**result.meta, "log_path": str(ctx.log_path)}})
        return result
