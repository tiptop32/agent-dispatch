from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from agent_dispatch.executors.base import BaseExecutorAdapter, RunContext
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import normalize
from agent_dispatch.models import ExecutionResult, Usage
from agent_dispatch.schemas import load_agent_result_strict_schema


class CodexAdapter(BaseExecutorAdapter):
    async def execute(self, ctx: RunContext) -> ExecutionResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="agent-dispatch-codex-"))
        try:
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
            return await self._execute_common(
                ctx,
                argv,
                stdin=ctx.prompt,
                parse_result=lambda outcome, changed: self._parse_result(
                    outcome, changed, last_path
                ),
                run_cli_fn=run_cli,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _parse_result(
        self, outcome: ProcessOutcome, changed: list[str], last_path: Path
    ) -> ExecutionResult:
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
                if event.get("type") in {"thread.started", "turn.started"} and event.get("model"):
                    model = event["model"]
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
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
        return result
