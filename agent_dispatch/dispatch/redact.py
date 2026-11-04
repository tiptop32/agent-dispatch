"""Очистка отчёта задачи под `prod` от явных секретов перед записью.

Правило данных в Task Package просит исполнителя отвечать выводами, но отчёт
пишет модель, и токен из лога может попасть в `summary` дословно. Очистка
детерминированная и узкая: маскируются только значения, которые выглядят как
секрет по форме, обычный текст и sha коммитов остаются как есть.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from agent_dispatch.models import ExecutionResult

MASK = "[REDACTED]"


def _bearer(match: re.Match[str]) -> str:
    """Токен после `Bearer`, но не слово прозы вроде «Bearer token is required».

    Заглавный `Bearer` (как в заголовке) маскирует токен любой длины, кроме
    короткого слова из строчных букв. Другое написание (`bearer authentication`)
    маскирует только токен с цифрой или символом.
    """
    prefix, token = match.group(1), match.group(2)
    # Точка в конце это конец предложения, а не часть токена.
    tail = token[len(token.rstrip(".")) :]
    token = token.rstrip(".")
    if prefix.startswith("Bearer"):
        secret = bool(token) and not re.fullmatch(r"[a-z]{1,19}", token)
    else:
        secret = bool(re.search(r"[0-9._~+/=-]", token))
    return f"{prefix}{MASK}{tail}" if secret else match.group(0)


#: Порядок важен: PEM и строки подключения целиком раньше пар `key=value`,
#: JWT раньше `Bearer`, чтобы один секрет не считался дважды.
_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    # `схема://логин:пароль@хост`: пароль маскируется, схема, логин и хост
    # остаются. Логин бывает пустым (`redis://:пароль@`), пароль в base64
    # содержит `/`; порт с путём (`host:8080/@scope`) паролем не считается.
    (
        re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]*:)(?!\d+/)[^\s@]+@"),
        rf"\g<1>{MASK}@",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        "[REDACTED JWT]",
    ),
    (re.compile(r"\b(bearer\s+)([A-Za-z0-9._~+/-]+=*)", re.IGNORECASE), _bearer),
    # Ключ кончается словом-секретом (`DB_PASSWORD`, `access_token`), поэтому
    # `max_tokens=100` не трогается. `pwd` только после разделителя (`db_pwd`):
    # `PWD=/path` и `OLDPWD` в выводе `env` это каталоги, а не пароли.
    # Двоеточие только у ключа в кавычках (JSON): в прозе «token: expired» это
    # не секрет.
    (
        re.compile(
            r"""(?ix)
            (\b[A-Za-z0-9_.-]*(?:password|passwd|[_.-]pwd|token|secret)
             (?:\s*=\s*|["']\s*:\s*))
            ("[^"\n]*"|'[^'\n]*'|[^\s"'&,;]+)
            """
        ),
        rf"\g<1>{MASK}",
    ),
]


def _sub_counted(
    pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str], text: str
) -> tuple[str, int]:
    """`subn` с функцией, где совпадение, возвращённое как есть, заменой не считается."""
    count = 0

    def counted(match: re.Match[str]) -> str:
        nonlocal count
        result = replace(match)
        count += result != match.group(0)
        return result

    return pattern.sub(counted, text), count


def redact_text(text: str | None) -> tuple[str | None, int]:
    """Текст без явных секретов и число замен."""
    if not text:
        return text, 0
    total = 0
    for pattern, replacement in _PATTERNS:
        if callable(replacement):
            text, count = _sub_counted(pattern, replacement, text)
        else:
            text, count = pattern.subn(replacement, text)
        total += count
    return text, total


def redact_result(result: ExecutionResult) -> None:
    """Очистить `summary` и `error`; факт и число замен идут в `meta.redacted`."""
    fields: list[str] = []
    total = 0
    summary, count = redact_text(result.summary)
    if count:
        result.summary, total = summary or "", total + count
        fields.append("summary")
    error, count = redact_text(result.error)
    if count:
        result.error, total = error, total + count
        fields.append("error")
    if total:
        result.meta["redacted"] = {"replacements": total, "fields": fields}
