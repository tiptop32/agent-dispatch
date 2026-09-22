"""Окружение для дочерних CLI-процессов (исполнители и claude_local-роутер)."""

from __future__ import annotations

import os

from agent_dispatch.config import Settings

# Маркеры родительской сессии Claude Code. С ними `claude -p` отказывается
# запускаться как вложенная сессия, а демон обычно стартует именно из MCP
# внутри Claude Code.
_SESSION_MARKERS = ("CLAUDECODE",)
_SESSION_PREFIXES = ("CLAUDE_CODE_",)
# Без Settings имена секретов неизвестны, поэтому режем по маске.
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(_SECRET_SUFFIXES) or "PASSWORD" in upper


def child_env(
    settings: Settings | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """os.environ без секретов и без маркеров сессии Claude Code, плюс extra.

    С Settings вырезаются имена из `secret_names()`; без Settings всё,
    что похоже на секрет по имени (`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`).
    """
    secrets = settings.secret_names() if settings is not None else set()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in secrets
        and not (settings is None and _looks_secret(k))
        and k not in _SESSION_MARKERS
        and not k.startswith(_SESSION_PREFIXES)
    }
    if extra:
        env.update(extra)
    return env
