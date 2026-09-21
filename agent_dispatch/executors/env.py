"""Окружение для дочерних CLI-процессов (исполнители и claude_local-роутер)."""

from __future__ import annotations

import os

from agent_dispatch.config import Settings

# Маркеры родительской сессии Claude Code. С ними `claude -p` отказывается
# запускаться как вложенная сессия, а демон обычно стартует именно из MCP
# внутри Claude Code.
_SESSION_MARKERS = ("CLAUDECODE",)
_SESSION_PREFIXES = ("CLAUDE_CODE_",)


def child_env(settings: Settings, extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ без секретов и без маркеров сессии Claude Code, плюс extra."""
    secrets = settings.secret_names()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in secrets and k not in _SESSION_MARKERS and not k.startswith(_SESSION_PREFIXES)
    }
    if extra:
        env.update(extra)
    return env
