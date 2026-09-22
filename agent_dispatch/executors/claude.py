from __future__ import annotations

import json

from agent_dispatch.executors.base import (
    BaseExecutorAdapter,
    RunContext,
)
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize
from agent_dispatch.models import ExecutionResult, Usage


class ClaudeAdapter(BaseExecutorAdapter):
    async def execute(self, ctx: RunContext) -> ExecutionResult:
        argv = [
            self.command,
            "-p",
            "--output-format",
            "json",
            "--add-dir",
            ctx.cwd,
            *(["--model", self.settings.model] if self.settings.model else []),
            *self.settings.extra_args,
        ]
        return await self._execute_common(
            ctx,
            argv,
            stdin=ctx.prompt,
            parse_result=self._parse_result,
            run_cli_fn=run_cli,
        )

    def _parse_result(self, outcome: ProcessOutcome, changed: list[str]) -> ExecutionResult:
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
        return result
