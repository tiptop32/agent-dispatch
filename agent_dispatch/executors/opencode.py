from __future__ import annotations

import json

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import (
    BaseExecutorAdapter,
    RunContext,
)
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize
from agent_dispatch.models import ExecutionResult, Usage


class OpenCodeAdapter(BaseExecutorAdapter):
    def __init__(
        self, name: str, settings: ExecutorSettings, base_env: dict[str, str] | None = None
    ):
        if settings.model is None:
            raise ValueError("opencode executor requires model")
        super().__init__(name, settings, base_env)

    async def execute(self, ctx: RunContext) -> ExecutionResult:
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
        return await self._execute_common(
            ctx,
            argv,
            stdin=None,
            parse_result=self._parse_result,
            run_cli_fn=run_cli,
        )

    def _parse_result(self, outcome: ProcessOutcome, changed: list[str]) -> ExecutionResult:
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
        return result
