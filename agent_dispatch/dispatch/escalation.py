from __future__ import annotations

from agent_dispatch.config import Settings
from agent_dispatch.models import ExecutionResult


def should_escalate(result: ExecutionResult) -> str | None:
    if result.status == "failed":
        return "failed"
    if result.needs_escalation or result.status == "needs_escalation":
        return "needs_escalation"
    if result.tests is not None and result.tests.result == "failed":
        return "tests_failed"
    if result.status == "partial" and result.meta.get("parse_error") and not result.changed_files:
        # Исполнитель не отдал отчёт и не тронул ни одного файла: работы нет.
        # Раньше это был тупик — `partial` не поднимал цепочку, и задача
        # застревала навсегда, сколько её ни переспрашивай.
        #
        # Непустой `changed_files` сюда не попадает намеренно: там работа есть,
        # просто отчёт не разобрался. Повторный запуск лёг бы поверх неё, а
        # судить о ней должен вызывающий по дифу.
        return "no_result"
    return None


def next_executor(current: str, settings: Settings, tried: list[str]) -> str | None:
    for executor in settings.escalation.get(current, []):
        configured = settings.executors.get(executor)
        if configured is None:
            continue
        if executor not in tried and configured.enabled:
            return executor
    return None
