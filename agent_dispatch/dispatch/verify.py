"""Проверка результата демоном, а не со слов исполнителя.

Самоотчёт `tests: passed` исполнитель пишет сам, и эскалация по нему верит
модели на слово. Команды `verify` из запроса демон запускает сам в каталоге,
где работал исполнитель, и судит по коду выхода.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from agent_dispatch.executors.process import run_cli
from agent_dispatch.models import ExecutionResult, VerificationInfo, VerifyCommand

#: Сколько хвоста вывода команды попадает в результат; полный вывод в логе задачи.
OUTPUT_TAIL = 2000

_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
_TEST_NAME = re.compile(
    r"^(?:test_.+\.py|.+_test\.(?:py|go|rs|rb|exs?)|.+\.(?:test|spec)\.[cm]?[jt]sx?|.+Test\.\w+)$"
)


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    return bool(_TEST_DIRS & {part.lower() for part in parts[:-1]}) or bool(
        _TEST_NAME.match(parts[-1])
    )


def _output_tail(stdout: str, stderr: str) -> str:
    """Хвост вывода с обоих потоков: предупреждения в stderr не должны вытеснить
    итог тестов из stdout, и наоборот."""
    if not stderr.strip():
        return stdout[-OUTPUT_TAIL:]
    if not stdout.strip():
        return stderr[-OUTPUT_TAIL:]
    half = OUTPUT_TAIL // 2
    return f"{stdout[-half:]}\n[stderr]\n{stderr[-half:]}"


async def run_verification(
    commands: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    timeout_seconds: float,
    log_path: Path,
    result: ExecutionResult,
) -> VerificationInfo:
    """Выполнить команды по порядку до первой упавшей.

    Команда идёт через `/bin/sh -c`: вызывающий пишет её так же, как в
    терминале (`uv run pytest tests -q && ruff check .`).
    """
    done: list[VerifyCommand] = []
    passed = True
    for command in commands:
        try:
            with log_path.open("a") as log_file:
                log_file.write(f"\n[verify] $ {command}\n")
            outcome = await run_cli(
                ["/bin/sh", "-c", command],
                cwd=cwd,
                env=env,
                stdin=None,
                timeout_seconds=timeout_seconds,
                log_path=log_path,
            )
        except OSError as exc:
            done.append(VerifyCommand(command=command, output_tail=f"cannot start: {exc}"))
            passed = False
            break
        done.append(
            VerifyCommand(
                command=command,
                exit_code=outcome.exit_code,
                timed_out=outcome.timed_out,
                duration_ms=outcome.duration_ms,
                output_tail=_output_tail(outcome.stdout, outcome.stderr),
            )
        )
        if outcome.timed_out or outcome.exit_code != 0:
            passed = False
            break
    reported = result.tests.result if result.tests else None
    return VerificationInfo(
        result="passed" if passed else "failed",
        commands=done,
        tests_changed=[path for path in result.changed_files if is_test_path(path)],
        contradicts_report=not passed and reported == "passed",
    )


def apply_verification(result: ExecutionResult, info: VerificationInfo) -> None:
    """Записать проверку в результат; упавшая снимает `completed`.

    Статус становится `partial`, а не `failed`: работа есть, и без эскалации
    вызывающий судит о ней по дифу. `error` не несёт вывода команды намеренно:
    `health.classify_failure` ищет в нём сообщения CLI о лимитах, и тест про
    rate limiting не должен выводить исполнителя из ротации.
    """
    result.verification = info
    if info.result == "passed":
        return
    if result.status == "completed":
        result.status = "partial"
    failed = info.commands[-1] if info.commands else None
    if failed is not None and result.error is None:
        result.error = f"verification failed ({failed.failure_reason}): {failed.command[:200]}"
