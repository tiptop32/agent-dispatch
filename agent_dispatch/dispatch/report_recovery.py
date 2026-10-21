"""Дозапрос отчёта: исполнитель сделал работу, но не отдал блок результата.

В телеметрии у opencode/x5-code-large так кончалась каждая седьмая правка
(2026-10-12…21: 12 запусков с изменёнными файлами и `no result block`).
Работа лежит в дереве, но без отчёта `partial` не говорит, готова ли она, а
проверка `verify` судит только код. Продолжение той же сессии коротким «дай
отчёт» стоит секунд и токенов отчёта, а не новой работы.

Чистые функции над результатами; запуск остаётся в `Dispatcher`.
"""

from __future__ import annotations

from agent_dispatch.models import ExecutionResult, Usage


def needs_report(result: ExecutionResult) -> bool:
    """Работа есть, отчёта нет, а сессию можно продолжить.

    Пустой `changed_files` сюда не попадает: там работы нет, и это звено
    эскалирует `no_result`. Ошибка (таймаут, сбой CLI) тоже: агента прервали,
    и отчёт о прерванной работе ничего не добавит к дифу.
    """
    return (
        result.status == "partial"
        and result.meta.get("parse_error") == "no result block"
        and bool(result.changed_files)
        and result.error is None
        and isinstance(result.meta.get("session_id"), str)
    )


def _add(left: int | float | None, right: int | float | None) -> int | float | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _usage(first: Usage | None, second: Usage | None) -> Usage | None:
    if first is None or second is None:
        return first or second
    return Usage(
        input_tokens=_add(first.input_tokens, second.input_tokens),
        output_tokens=_add(first.output_tokens, second.output_tokens),
        cost_usd=_add(first.cost_usd, second.cost_usd),
    )


def merge_report(original: ExecutionResult, report: ExecutionResult) -> ExecutionResult:
    """Итог запуска после дозапроса.

    Удачный отчёт заменяет статус, итог и тесты, но список файлов остаётся
    объединением: дозапрос видит только свои изменения, а работа сделана в
    первом запуске. Отчёт стоит того же доверия, что и обычный: модель пишет
    его в той же сессии, помня свою работу, а `verify` демона судит его так же.
    Неудачный оставляет исходный `partial` с пометкой.
    Расходы складываются в любом случае: оба запуска оплачены.
    """
    usage = _usage(original.usage, report.usage)
    # Дозапрос просили ничего не менять, но модель могла не послушаться: файлы,
    # которые он тронул, лежат в дереве при любом исходе.
    files = list(original.changed_files)
    files += [path for path in report.changed_files if path not in files]
    failure = report.error or report.meta.get("parse_error")
    if failure:
        meta = {**original.meta, "report_recovery": f"failed: {str(failure)[:200]}"}
        # Остывание, включённое сбоем дозапроса, видно в итоге задачи.
        for key in ("executor_failure", "cooldown_until"):
            if key in report.meta:
                meta[key] = report.meta[key]
        return original.model_copy(update={"changed_files": files, "usage": usage, "meta": meta})
    # Метаданные запуска (лог, длительность, сессия) остаются от первого: работа
    # была там. От отчёта берётся только то, что говорит о нём самом.
    meta = {key: value for key, value in original.meta.items() if key != "parse_error"}
    if report.meta.get("coerced"):
        meta["coerced"] = report.meta["coerced"]
    meta["report_recovery"] = "recovered"
    return report.model_copy(update={"changed_files": files, "usage": usage, "meta": meta})
