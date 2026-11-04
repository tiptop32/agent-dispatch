"""Очистка отчёта задачи под `prod` от явных секретов перед записью.

Правило данных в Task Package просит исполнителя отвечать выводами, но отчёт
пишет модель, и токен из лога может попасть в `summary` дословно. Очистка
детерминированная и узкая: маскируются только значения, которые выглядят как
секрет по форме, обычный текст и sha коммитов остаются как есть.
"""

from __future__ import annotations

import re

from agent_dispatch.models import ExecutionResult

MASK = "[REDACTED]"

#: Порядок важен: PEM и строки подключения целиком раньше пар `key=value`,
#: JWT раньше `Bearer`, чтобы один секрет не считался дважды.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    # `схема://логин:пароль@хост`: пароль маскируется, схема, логин и хост остаются.
    (
        re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"),
        rf"\g<1>{MASK}@",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        "[REDACTED JWT]",
    ),
    (
        re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/-]{8,}=*", re.IGNORECASE),
        rf"\g<1>{MASK}",
    ),
    # Ключ кончается словом-секретом (`DB_PASSWORD`, `access_token`), поэтому
    # `max_tokens=100` не трогается. Двоеточие только у ключа в кавычках
    # (JSON): в прозе «token: expired» это не секрет.
    (
        re.compile(
            r"""(?ix)
            (\b[A-Za-z0-9_.-]*(?:password|passwd|pwd|token|secret)
             (?:\s*=\s*|["']\s*:\s*))
            ("[^"\n]*"|'[^'\n]*'|[^\s"'&,;]+)
            """
        ),
        rf"\g<1>{MASK}",
    ),
]


def redact_text(text: str | None) -> tuple[str | None, int]:
    """Текст без явных секретов и число замен."""
    if not text:
        return text, 0
    total = 0
    for pattern, replacement in _PATTERNS:
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
