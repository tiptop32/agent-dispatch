from __future__ import annotations

import json
import tempfile

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import (
    BaseExecutorAdapter,
    RunContext,
    strip_flags,
)
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize
from agent_dispatch.models import ExecutionResult, Usage


def _has_auto_option(args: list[str]) -> bool:
    return any(arg == "--no-auto" or arg == "--auto" or arg.startswith("--auto=") for arg in args)


def _without_auto_enable(args: list[str]) -> list[str]:
    """Remove auto-approval while retaining explicit false forms."""
    result: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--auto":
            value = args[index + 1].lower() if index + 1 < len(args) else None
            if value in {"true", "false"}:
                if value == "false":
                    result.extend(args[index : index + 2])
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("--auto="):
            if arg.partition("=")[2].lower() == "false":
                result.append(arg)
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def review_permissions_env() -> str:
    """OPENCODE_PERMISSION для ревью: читать /tmp разрешено, править — нет.

    Ревью идёт с `--agent plan` и без `--auto`, поэтому чтение лога в /tmp,
    названного вызывающим, иначе автоотклоняется (проверено живьём на
    opencode 1.18.35: без переменной чтение «auto-rejecting», с ней файл
    читается). Обычным задачам переменную не ставим: там своё `--auto`.
    """
    return json.dumps(
        {
            "external_directory": {
                "/tmp/*": "allow",
                "/private/tmp/*": "allow",
                f"{tempfile.gettempdir()}/*": "allow",
            }
        }
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _error_from_event(event: dict) -> str | None:
    """Сообщение об ошибке из события; последняя ошибка побеждает.

    v1 присылает вложенный ``error.data.message`` (или только имя/тип),
    v2 — ``error.message`` внутри события: ``{"error": {"type":
    "provider.no-route" | "provider.auth", "message": ...}}``.
    """
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return data["message"]
        for key in ("message", "name", "type"):
            value = error.get(key)
            if isinstance(value, str):
                return value
    return None


class OpenCodeAdapter(BaseExecutorAdapter):
    def __init__(
        self, name: str, settings: ExecutorSettings, base_env: dict[str, str] | None = None
    ):
        if settings.model is None:
            raise ValueError("opencode executor requires model")
        super().__init__(name, settings, base_env)

    def build_argv(self, ctx: RunContext) -> list[str]:
        extra = self.settings.extra_args
        if ctx.read_only:
            # Встроенный агент `plan` в OpenCode запрещает правку файлов.
            # Автоодобрение в режиме ревью ослабило бы read-only защиту.
            # Явные opt-out формы оставляем как дополнительный запрет.
            extra = [
                *_without_auto_enable(strip_flags(extra, {"--agent"})),
                "--agent",
                "plan",
            ]
        elif not _has_auto_option(extra):
            # Headless-запуск: без `--auto` OpenCode спрашивает разрешение на
            # инструменты. Явную настройку пользователя не переопределяем.
            extra = [*extra, "--auto"]
        # `--dir` поддерживает только CLI v1; v2 выбирает рабочий каталог
        # через process.env.PWD (upstream cli-run.ts:73), который execute()
        # выставляет в ctx.cwd. build_argv не должен знать про cwd.
        return [
            self.command,
            "run",
            "--format",
            "json",
            "--model",
            self.settings.model,
            *(["--session", ctx.resume_session] if ctx.resume_session else []),
            *extra,
            ctx.prompt,
        ]

    async def execute(self, ctx: RunContext) -> ExecutionResult:
        env = {**ctx.env, "PWD": ctx.cwd}
        if ctx.read_only:
            env["OPENCODE_PERMISSION"] = review_permissions_env()
        ctx = ctx.model_copy(update={"env": env})
        argv = self.build_argv(ctx)
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
        session = None
        for line in outcome.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if session is None and isinstance(event.get("sessionID"), str):
                session = event["sessionID"]
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
                tokens = part.get("tokens")
                if isinstance(tokens, dict):
                    input_tokens += _as_int(tokens.get("input"))
                    output_tokens += _as_int(tokens.get("output"))
                cost += _as_float(part.get("cost"))
            elif event.get("type") == "error":
                message = _error_from_event(event)
                if message:
                    error_message = message
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
        if session:
            result.meta["session_id"] = session
        return result
