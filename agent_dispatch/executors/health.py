"""Сбой исполнителя, который говорит о самом исполнителе, а не о задаче.

Лимит расходов, перегруженная модель, отказ авторизации и обрыв сети повторятся
на следующей задаче так же, как на этой. Пока такой исполнитель в ротации, роутер
и цепочки эскалации отправляют к нему задачу за задачей, и каждая тратит время до
отказа: в телеметрии это семь подряд таймаутов по 30 минут у исполнителя без сети
и три подряд отказа по лимиту расходов.

Классификация смотрит только на узнаваемые сообщения самих CLI. Поле `error` это
stderr или ошибка из отчёта CLI, туда не попадает вывод инструментов агента.
`summary` у упавшего запуска это хвост stdout, где лежит и работа агента, поэтому
в нём ищется только событие переподключения codex, и только когда агент ничего
не изменил. Ошибки MCP-серверов в stderr codex (например, протухший OAuth-токен
mcphub) намеренно не считаются: исполнитель с ними продолжает работать.
"""

from __future__ import annotations

import re
from typing import Literal

from agent_dispatch.models import ExecutionResult

FailureKind = Literal["quota", "capacity", "auth", "network"]

_ERROR_PATTERNS: tuple[tuple[FailureKind, re.Pattern[str]], ...] = (
    (
        "quota",
        re.compile(
            r"spend limit|usage limit|hit your limit|insufficient_quota"
            r"|rate[ _]limit (?:exceeded|reached)|credit balance is too low|quota exceeded",
            re.IGNORECASE,
        ),
    ),
    ("capacity", re.compile(r"at capacity|overloaded", re.IGNORECASE)),
    (
        "auth",
        re.compile(
            r"no cookie auth credentials|invalid api key|invalid x-api-key|authentication_error"
            r"|401 unauthorized|not logged in|please run /login",
            re.IGNORECASE,
        ),
    ),
)

#: Событие codex при потере связи с API: `{"type":"error","message":"Reconnecting... 5/5 ..."}`.
_RECONNECT = re.compile(r"Reconnecting\.\.\. \d+/\d+")


def classify_failure(result: ExecutionResult) -> FailureKind | None:
    """Вид сбоя исполнителя или None, если сбой относится к задаче."""
    if result.status != "failed":
        return None
    error = result.error or ""
    for kind, pattern in _ERROR_PATTERNS:
        if pattern.search(error):
            return kind
    # Таймаут и сторож молчания сами по себе ничего не говорят об исполнителе:
    # агент мог честно работать. Обрыв связи виден по событиям переподключения,
    # а пустой changed_files значит, что до работы дело не дошло.
    if error == "timeout" or error.startswith("stalled:"):
        if not result.changed_files and _RECONNECT.search(result.summary):
            return "network"
    return None
