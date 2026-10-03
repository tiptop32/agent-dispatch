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

import os
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_dispatch.models import ExecutionResult

FailureKind = Literal["quota", "capacity", "auth", "network"]
FailureScope = Literal["group", "executor"]

_ERROR_PATTERNS: tuple[tuple[FailureKind, re.Pattern[str]], ...] = (
    (
        "quota",
        re.compile(
            # «hit your weekly limit», «hit your session limit», «hit your
            # monthly spend limit», «hit your usage limit» у claude и codex.
            r"spend limit|usage limit|hit your (?:[\w-]+ )*limit|insufficient_quota"
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


#: Лимит конкретной модели («hit your Opus limit»), а не всего аккаунта.
_MODEL_LIMIT = re.compile(r"hit your (?:opus|sonnet|haiku|fable)\b", re.IGNORECASE)


def failure_scope(kind: FailureKind, error: str) -> FailureScope:
    """Кого выводить из ротации: одного исполнителя или всех с его лимитом.

    Лимит, авторизация и сеть общие для аккаунта: у `claude/opus` и
    `claude/haiku` один лимит, и после отказа одного второй откажет так же.
    Перегружена же конкретная модель, соседняя может работать.
    """
    if kind == "capacity" or _MODEL_LIMIT.search(error):
        return "executor"
    return "group"


_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DATE = (
    r"(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4})\s+)?)?"
)
_TIME = r"(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b"
#: claude: «resets 5am (Asia/Yekaterinburg)», «resets Sep 26 at 5am (…)»;
#: codex: «Try again at 5:02 PM.», «Try again at Sep 27th, 2026 5:02 PM.».
_RESET_AT = re.compile(
    r"(?:resets|try again at)\s+" + _DATE + _TIME + r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_RESET_IN = re.compile(r"try again in\s+(?P<span>[\d\s\w,]+)", re.IGNORECASE)
_SPAN_PART = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
#: Сброс дальше этого считается ошибкой разбора: самый длинный лимит недельный.
MAX_RESET = timedelta(days=8)


def local_zone() -> tzinfo:
    """IANA-зона машины демона.

    `datetime.now().astimezone().tzinfo` это фиксированное смещение текущего
    момента: сброс «завтра в 5:02 PM» через переход часов вышел бы на час мимо.
    """
    for name in (os.environ.get("TZ", "").lstrip(":"), _localtime_link()):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return datetime.now().astimezone().tzinfo or UTC


def _localtime_link() -> str:
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def _zone(name: str | None, default: tzinfo) -> tzinfo:
    if not name:
        return default
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return default


def _reset_at(match: re.Match[str], now: datetime, local_tz: tzinfo) -> datetime | None:
    zone = _zone(match["tz"], local_tz)
    local_now = now.astimezone(zone)
    hour = int(match["hour"]) % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    minute = int(match["minute"] or 0)
    if minute > 59 or int(match["hour"]) > 12:
        return None
    if match["mon"]:
        month = _MONTHS.get(match["mon"].lower())
        if month is None:
            return None
        year = int(match["year"]) if match["year"] else local_now.year
        try:
            day = date(year, month, int(match["day"]))
        except ValueError:
            return None
        if not match["year"] and day < local_now.date() - timedelta(days=1):
            # «Jan 2» в конце декабря это следующий год.
            try:
                day = day.replace(year=year + 1)
            except ValueError:
                return None
        instants = _instants(day, hour, minute, zone)
        future = [moment for moment in instants if moment > now]
        # Прошедший момент вернётся как есть, и `parse_reset` его отбросит.
        return min(future) if future else max(instants, default=None)
    # Время без даты это ближайший такой момент в будущем: «resets 5am»,
    # сказанное в 14:00, значит завтра. Перебор идёт по UTC-моментам, а не по
    # стенным часам: при переводе часов назад 1:30 бывает дважды (оба варианта,
    # `fold`), при переводе вперёд 2:30 не бывает вовсе (пропускается).
    moments = [
        moment
        for days in range(3)
        for moment in _instants((local_now + timedelta(days=days)).date(), hour, minute, zone)
        if moment > now
    ]
    return min(moments) if moments else None


def _instants(day: date, hour: int, minute: int, zone: tzinfo) -> list[datetime]:
    """UTC-моменты, когда в `zone` на часах `day hour:minute`: 0, 1 или 2."""
    result = []
    for fold in (0, 1):
        wall = datetime(day.year, day.month, day.day, hour, minute, fold=fold)
        moment = wall.replace(tzinfo=zone).astimezone(UTC)
        if moment.astimezone(zone).replace(tzinfo=None) == wall and moment not in result:
            result.append(moment)
    return result


def parse_reset(error: str, now: datetime, local_tz: tzinfo | None = None) -> datetime | None:
    """Момент сброса лимита из сообщения CLI, в UTC; None, если его там нет.

    Время без часового пояса (codex пишет «Try again at 5:02 PM») это местное
    время машины, где работает CLI, то есть демона. Из нескольких моментов берётся
    самый поздний: раньше него исполнитель всё равно откажет.
    """
    local_tz = local_tz or local_zone()
    moments: list[datetime] = []
    for match in _RESET_AT.finditer(error):
        moment = _reset_at(match, now, local_tz)
        if moment is not None:
            moments.append(moment)
    for match in _RESET_IN.finditer(error):
        seconds = sum(
            int(amount) * _UNIT_SECONDS[unit[0].lower()]
            for amount, unit in _SPAN_PART.findall(match["span"])
        )
        if seconds:
            moments.append(now + timedelta(seconds=seconds))
    valid = [m.astimezone(UTC) for m in moments if now < m <= now + MAX_RESET]
    return max(valid) if valid else None


#: Запас после объявленного сброса: часы CLI и демона расходятся на секунды.
RESET_GRACE = timedelta(seconds=60)


def cooldown_seconds(
    kind: FailureKind,
    error: str,
    now: datetime,
    *,
    failure_seconds: int,
    quota_seconds: int,
    local_tz: tzinfo | None = None,
    recheck: bool = False,
) -> float:
    """На сколько вывести исполнителя из ротации; 0 значит не выводить.

    Исчерпанный лимит держит исполнителя до объявленного сброса: недельный лимит
    claude, остывающий по 15 минут, 2 октября дал 14 отказов подряд за день.

    Лимит расходов («monthly spend limit · raise it at …») человек поднимает сам
    в любую минуту, поэтому сброс из того же сообщения для него только верхняя
    граница: 22 сентября claude написал «weekly limit resets Sep 26», а через
    7.5 часа снова работал. Такой лимит перепроверяется через `quota_seconds`.
    Так же с `recheck`: пул аккаунтов за балансировщиком отвечает сбросом одного
    аккаунта, а следующий запрос может уйти на свободный.
    """
    if failure_seconds <= 0:
        return 0
    if kind != "quota":
        return failure_seconds
    reset = parse_reset(error, now, local_tz)
    if reset is None:
        return quota_seconds
    seconds = (reset + RESET_GRACE - now).total_seconds()
    if recheck or _SPEND_LIMIT.search(error):
        return min(seconds, quota_seconds)
    return seconds


_SPEND_LIMIT = re.compile(r"spend limit", re.IGNORECASE)
