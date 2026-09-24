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
        permission_rejections = []
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
            elif event.get("type") == "tool_use" and isinstance(event.get("part"), dict):
                part = event["part"]
                state = part.get("state")
                if isinstance(state, dict):
                    error = state.get("error")
                    if (
                        state.get("status") == "error"
                        and isinstance(error, str)
                        and "rejected permission" in error.lower()
                    ):
                        inputs = state.get("input")
                        if not isinstance(inputs, dict):
                            inputs = {}
                        detail = next(
                            (
                                inputs[key]
                                for key in ("command", "filePath", "path")
                                if inputs.get(key) is not None
                            ),
                            "",
                        )
                        permission_rejections.append(f"{part.get('tool', '')}: {str(detail)[:120]}")
            elif event.get("type") == "step_finish" and isinstance(event.get("part"), dict):
                part = event["part"]
                tokens = part.get("tokens") or {}
                input_tokens += int(tokens.get("input", 0))
                output_tokens += int(tokens.get("output", 0))
                cost += float(part.get("cost", 0) or 0)
            elif event.get("type") == "error":
                error_message = ((event.get("error") or {}).get("data") or {}).get("message")
        text = "\n".join(text_parts)
        raw = extract_result_block(text)
        result = normalize(raw, outcome, changed, self.name, self.settings.model)
        if error_message:
            result = result.model_copy(update={"status": "failed", "error": error_message})
        if permission_rejections:
            if raw is None:
                result = result.model_copy(
                    update={
                        "status": "failed",
                        "error": "opencode permission rejected: "
                        + "; ".join(permission_rejections),
                    }
                )
            else:
                result = result.model_copy(
                    update={
                        "meta": {
                            **result.meta,
                            "permission_rejected": permission_rejections,
                        }
                    }
                )
        if input_tokens or output_tokens or cost:
            result = result.model_copy(
                update={
                    "usage": Usage(
                        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost
                    )
                }
            )
        return result
