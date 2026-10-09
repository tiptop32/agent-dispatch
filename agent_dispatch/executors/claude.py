from __future__ import annotations

import json
import uuid

from agent_dispatch.executors.base import (
    BaseExecutorAdapter,
    RunContext,
    strip_flags,
)
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize
from agent_dispatch.models import ExecutionResult, Usage

#: Ревью: из встроенных инструментов существуют только чтение и поиск.
#: `--allowedTools` для этого мало: правила `allow` из ~/.claude/settings.json
#: складываются с ним, и живая проверка 2026-10-03 создала файл при
#: `--disallowedTools Write`. `--tools` убирает остальные инструменты целиком,
#: `--strict-mcp-config` отключает MCP-серверы пользователя (среди них бывают
#: пишущие). Дифф рабочей копии ревьюер получает в Task Package, Bash ему не нужен.
READ_ONLY_ARGS = [
    "--tools",
    "Read,Glob,Grep",
    "--strict-mcp-config",
    "--permission-mode",
    "dontAsk",
]


class ClaudeAdapter(BaseExecutorAdapter):
    # `-p --output-format json` печатает результат одним JSON в самом конце.
    streams_output = False

    async def execute(self, ctx: RunContext) -> ExecutionResult:
        extra = self.settings.extra_args
        if ctx.read_only:
            extra = [
                *strip_flags(
                    extra,
                    {
                        "--permission-mode",
                        "--allowedTools",
                        "--allowed-tools",
                        "--disallowedTools",
                        "--disallowed-tools",
                        "--tools",
                        "--mcp-config",
                    },
                    {"--dangerously-skip-permissions", "--strict-mcp-config"},
                ),
                *READ_ONLY_ARGS,
            ]
        # id новой сессии задаёт демон, а не CLI: при таймауте JSON в конце не
        # напечатается, а продолжить именно такую задачу полезнее всего.
        session = ctx.resume_session or str(uuid.uuid4())
        argv = [
            self.command,
            "-p",
            "--output-format",
            "json",
            *(["--resume", session] if ctx.resume_session else ["--session-id", session]),
            "--add-dir",
            ctx.cwd,
            *(["--model", self.settings.model] if self.settings.model else []),
            *extra,
        ]
        return await self._execute_common(
            ctx,
            argv,
            stdin=ctx.prompt,
            parse_result=lambda outcome, changed: self._parse_result(outcome, changed, session),
            run_cli_fn=run_cli,
        )

    def _parse_result(
        self, outcome: ProcessOutcome, changed: list[str], session: str | None = None
    ) -> ExecutionResult:
        model = self.settings.model
        usage = None
        raw = None
        result_text = ""
        try:
            payload = json.loads(outcome.stdout)
            if isinstance(payload, dict):
                session = str(payload.get("session_id") or session or "") or None
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
        if session:
            result.meta["session_id"] = session
        return result
