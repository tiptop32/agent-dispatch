from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import BaseExecutorAdapter, RunContext
from agent_dispatch.executors.health import omp_error
from agent_dispatch.executors.process import ProcessOutcome, run_cli
from agent_dispatch.executors.result_parser import extract_result_block, normalize, tail
from agent_dispatch.models import ExecutionResult, Usage


class OmpAdapter(BaseExecutorAdapter):
    """Адаптер исполнителя omp (oh-my-pi) на `omp -p --mode json`.

    omp пишет NDJSON-события по ходу работы (первая строка — заголовок сессии,
    итог — последний ассистентский `message_end`), поэтому сторож молчания
    применим, как у codex и opencode. Формат снят живыми прогонами, факты в
    `docs/research/omp-cli-facts.md`.
    """

    #: omp печатает события по ходу работы: на жёстком таймауте и при зависании
    #: `meta.last_output_age_s` отличает агента, работавшего до конца, от висящего.
    streams_output = True

    #: Оверлей диспетчера из репозитория omp-setup: `retry.maxRetries: 1`.
    #: Повторами по умолчанию (до 10 с паузами) занимается эскалация диспетчера,
    #: а не получасовой процесс omp.
    OVERLAY_PATH = "~/.omp/agent/dispatch-overlay.yml"

    #: Корпоративный CA copilot-x5, выгруженный install.sh omp-setup. bun
    #: системному Keychain не доверяет: без переменной TLS к api-copilot.x5.ru
    #: падает (`TLS certificate verification failed`).
    X5_ROOT_PEM = "~/.omp/agent/x5-root.pem"

    #: Ключ балансера agent-lb. Его экспортирует профиль shell пользователя, а
    #: окружение запускающего процесса доходит до bash агента omp: без вырезания
    #: ключ утёк бы в сессию агента.
    LB_KEY_ENV = "AGENT_LB_API_KEY"

    #: Инструменты режима ревью. Инструмента `find` у omp нет: перечисление
    #: `--tools …,find` даёт код 2 и обрывает запуск (факты Task 1).
    REVIEW_TOOLS = "read,grep,glob"

    #: Уровень рассуждений, если пользователь не задал свой через `extra_args`.
    DEFAULT_THINKING = "medium"

    #: Ошибка хода без единого ответа модели; по началу строки её узнаёт
    #: `health.classify_failure` и остужает группу, как у opencode.
    NO_RESPONSE = "omp: no model response (the run ended before the first model reply)"

    def __init__(
        self,
        name: str,
        settings: ExecutorSettings,
        base_env: dict[str, str] | None = None,
        data_dir: str | Path | None = None,
    ):
        if settings.model is None:
            raise ValueError("omp executor requires model")
        super().__init__(name, settings, base_env)
        # Сессии живут в общем каталоге данных демона: follow-up продолжается
        # через --resume с тем же --session-dir, cwd и профилем.
        if data_dir is None:
            data_dir = os.environ.get("AGENT_DISPATCH_DATA_DIR", "~/.local/share/agent-dispatch")
        self.session_dir = Path(data_dir).expanduser() / "omp-sessions"
        # Раскрывается один раз в синхронном __init__: из execute() блокирующих
        # операций pathlib быть не должно.
        self.x5_root_pem = str(Path(self.X5_ROOT_PEM).expanduser())

    def build_argv(self, ctx: RunContext) -> list[str]:
        extra = self.settings.extra_args

        def configured(flag: str) -> bool:
            return any(arg.split("=", 1)[0] == flag for arg in extra)

        argv = [
            self.command,
            "-p",
            "--mode",
            "json",
            # Профиль x5 без единого внешнего провайдера: периметр данных
            # держится им, включая сабагентов (факты Task 1, x5_task.ndjson).
            *(["--profile", "x5"] if self.settings.corporate else []),
            "--model",
            self.settings.model,
        ]
        if not configured("--thinking"):
            argv += ["--thinking", self.DEFAULT_THINKING]
        argv += [
            # Свой лимит времени у omp: он успевает завершить ход и выйти сам,
            # а не умирать от SIGTERM диспетчера.
            "--max-time",
            # Целые секунды: справка omp показывает форматы 600, 10m, 1h.
            str(max(1, int(ctx.timeout_seconds))),
            "--session-dir",
            str(self.session_dir),
            "--cwd",
            ctx.cwd,
            "--config",
            str(Path(self.OVERLAY_PATH).expanduser()),
        ]
        if ctx.resume_session:
            argv += ["--resume", ctx.resume_session]
        if ctx.read_only:
            # Ревью только читает: yolo в этом режиме не ставим.
            argv += ["--tools", self.REVIEW_TOOLS]
        else:
            argv += ["--approval-mode", "yolo"]
        return [*argv, *extra, ctx.prompt]

    async def execute(self, ctx: RunContext) -> ExecutionResult:
        env = {k: v for k, v in ctx.env.items() if k != self.LB_KEY_ENV}
        if self.settings.corporate:
            env["NODE_EXTRA_CA_CERTS"] = self.x5_root_pem
        ctx = ctx.model_copy(update={"env": env})
        # stdin=None даёт DEVNULL в run_cli: без TTY omp ждёт EOF на stdin даже
        # с промптом в аргументе и висит до таймаута (факты Task 1).
        return await self._execute_common(
            ctx,
            self.build_argv(ctx),
            stdin=None,
            parse_result=self._parse_result,
            run_cli_fn=run_cli,
        )

    def _parse_result(self, outcome: ProcessOutcome, changed: list[str]) -> ExecutionResult:
        session = None
        text_parts: list[str] = []
        usage: Usage | None = None
        stop_error = False
        #: Модель хоть раз ответила: текст или вызов инструмента.
        responded = False
        for line in outcome.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # обрыв потока или мусорная строка — не повод ронять разбор
            if not isinstance(event, dict):
                continue
            if session is None and event.get("type") == "session" and event.get("id"):
                session = str(event["id"])
            if event.get("type") != "message_end":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    text_parts.append(str(item["text"]))
                if item.get("type") in ("text", "toolCall"):
                    responded = True
            if isinstance(message.get("usage"), dict):
                # У каждого хода свой `usage`: стоимость задачи — сумма по ходам,
                # иначе потолки бюджета видели бы только последний ход.
                usage = _add_usage(usage, _usage(message["usage"]))
            if message.get("stopReason") == "error":
                stop_error = True
        raw = extract_result_block("\n".join(text_parts))
        # `--max-cost` (код 3) не сбой: normalize на ненулевом выходе отбросил бы
        # успевший дойти отчёт агента, поэтому разбираем поток как успешный выход.
        parsed = replace(outcome, exit_code=0) if outcome.exit_code == 3 else outcome
        result = normalize(raw, parsed, changed, self.name, self.settings.model)
        if stop_error:
            # Сообщение хода с ошибкой пишет сам CLI о провайдере (badkey, down,
            # TLS), а не агент: `health.classify_failure` узнаёт его и остужает.
            result = result.model_copy(
                update={
                    "status": "failed",
                    "error": omp_error(outcome.stdout) or tail(outcome.stderr),
                }
            )
        elif outcome.exit_code == 3:
            # omp сам остановил ход по потолку расходов (--max-cost). Повтор на
            # следующей задаче зависит от бюджета, а не от исполнителя: статус
            # по правилам проекта для бюджета, остывание не положено.
            result = result.model_copy(
                update={
                    **(
                        {"status": "partial", "error": "omp: cost cap reached (--max-cost)"}
                        if raw is None
                        else {}
                    ),
                    "meta": {**result.meta, "budget": {"reason": "omp_max_cost"}},
                }
            )
        elif not responded and raw is None and not changed and result.status == "partial":
            # Код 0, но ни одного ответа модели: omp молча вышел, когда запрос
            # к провайдеру не вернулся. Это сбой исполнителя, а не задачи.
            result = result.model_copy(update={"status": "failed", "error": self.NO_RESPONSE})
        if usage is not None:
            result = result.model_copy(update={"usage": usage})
        if session:
            result.meta["session_id"] = session
        return result


def _usage(u: dict) -> Usage:
    """Токены и стоимость из `usage` ассистентского `message_end`.

    У agent-lb `cost.total` в долларах ненулевой, у copilot-x5 всегда 0.
    """
    cost = u.get("cost")
    return Usage(
        input_tokens=_as_int(u.get("input")),
        output_tokens=_as_int(u.get("output")),
        cost_usd=_as_float(cost.get("total")) if isinstance(cost, dict) else None,
    )


def _add_usage(total: Usage | None, turn: Usage) -> Usage:
    """Сумма токенов и стоимости по ходам; None в слагаемом не обнуляет сумму."""
    if total is None:
        return turn

    def plus(a, b):
        return b if a is None else a if b is None else a + b

    return Usage(
        input_tokens=plus(total.input_tokens, turn.input_tokens),
        output_tokens=plus(total.output_tokens, turn.output_tokens),
        cost_usd=plus(total.cost_usd, turn.cost_usd),
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
