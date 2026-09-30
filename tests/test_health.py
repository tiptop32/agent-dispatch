"""Сбой исполнителя против сбоя задачи. Строки ошибок взяты из живой телеметрии."""

from __future__ import annotations

import pytest

from agent_dispatch.executors.health import classify_failure
from agent_dispatch.models import ExecutionResult

# Хвост stdout codex, у которого пропала связь с API (задачи 09-23, 30 минут до таймаута).
CODEX_OFFLINE = (
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"Reconnecting... 4/5 (request timed out)"}\n'
    '{"type":"error","message":"Reconnecting... 5/5 (request timed out)"}\n'
    '{"type":"item.completed","item":{"id":"item_2","type":"error",'
    '"message":"Falling back from WebSockets to HTTPS transport. request timed out"}}\n'
)


def _failed(error: str, summary: str = "", changed_files: list[str] | None = None):
    return ExecutionResult(
        status="failed",
        executor="x",
        model=None,
        summary=summary,
        error=error,
        changed_files=changed_files or [],
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage"
            "?from=cc_cli_limit_message · your session limit resets 3pm (Asia/Yekaterinburg)",
            "quota",
            id="claude-spend-limit",
        ),
        pytest.param("You've hit your usage limit. Try again later.", "quota", id="codex-usage"),
        pytest.param(
            "Selected model is at capacity. Please try a different model.",
            "capacity",
            id="codex-capacity",
        ),
        pytest.param("No cookie auth credentials found", "auth", id="opencode-auth"),
        pytest.param("Invalid API key · Please run /login", "auth", id="claude-login"),
    ],
)
def test_cli_messages_about_the_executor_are_classified(error, expected):
    assert classify_failure(_failed(error)) == expected


def test_timeout_after_codex_lost_the_api_is_a_network_failure():
    assert classify_failure(_failed("timeout", CODEX_OFFLINE)) == "network"


def test_stall_after_codex_lost_the_api_is_a_network_failure():
    assert classify_failure(_failed("stalled: no output for 900s", CODEX_OFFLINE)) == "network"


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            # Таймаут работавшего агента: тесты идут, файлы меняются.
            _failed("timeout", "2348 passed, 69 warnings in 81.74s"),
            id="timeout-while-working",
        ),
        pytest.param(
            # Связь терялась, но агент успел поработать: это задача, а не исполнитель.
            _failed("timeout", CODEX_OFFLINE, changed_files=["src/app/main.py"]),
            id="reconnects-but-changed-files",
        ),
        pytest.param(
            # Протухший OAuth MCP-сервера в stderr codex: сам codex работает дальше.
            _failed(
                "ERROR codex_rmcp_client::oauth::refresh_transaction: error=failed to refresh "
                "OAuth tokens for server mcphub: OAuth refresh token was rejected"
            ),
            id="mcp-server-oauth",
        ),
        pytest.param(
            # Работа агента в stdout: задача про rate limiting, а не лимит исполнителя.
            _failed("exit 1", summary="implemented rate limit exceeded handler, spend limit"),
            id="words-in-agent-output",
        ),
        pytest.param(_failed("opencode permission rejected: read: /tmp/x.log"), id="permission"),
        pytest.param(
            ExecutionResult(
                status="completed", executor="x", model=None, summary="ok", error="spend limit"
            ),
            id="not-failed",
        ),
    ],
)
def test_task_failures_do_not_blame_the_executor(result):
    assert classify_failure(result) is None
