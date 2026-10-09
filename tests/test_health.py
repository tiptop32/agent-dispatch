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
            # Таймаут с изменёнными файлами приходит как partial: исполнитель не виноват.
            ExecutionResult(
                status="partial",
                executor="x",
                model=None,
                summary="дописал модуль",
                error="timeout",
                changed_files=["src/app/main.py"],
            ),
            id="partial-timeout",
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


# --- лимиты: распознавание, время сброса, охват ------------------------------

from datetime import UTC, datetime, timedelta, timezone  # noqa: E402

from agent_dispatch.executors.health import (  # noqa: E402
    RESET_GRACE,
    cooldown_seconds,
    failure_scope,
    parse_reset,
)

YEKT = timezone(timedelta(hours=5))
#: 2026-10-02 09:29:48Z, первая задача дня, упавшая на недельном лимите claude.
NOW = datetime(2026, 10, 2, 9, 29, 48, tzinfo=UTC)


@pytest.mark.parametrize(
    "error",
    [
        # 2 октября 14 подряд отказов claude/opus этой строкой не включали
        # остывание: регулярка знала только «hit your limit».
        pytest.param(
            "You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)", id="claude-weekly"
        ),
        pytest.param(
            "You've hit your session limit · resets 4am (Asia/Yekaterinburg)", id="claude-session"
        ),
        pytest.param("You've hit your Opus limit · resets 5am", id="claude-model-limit"),
        pytest.param("You’ve hit your usage limit. Try again at 5:02 PM.", id="codex-curly"),
    ],
)
def test_every_live_limit_message_is_a_quota_failure(error):
    assert classify_failure(_failed(error)) == "quota"


def test_agent_words_about_limits_in_summary_do_not_count():
    # Задача про лимиты: слова в выводе агента не выключают исполнителя.
    result = _failed("exit code 1", summary="You've hit your weekly limit, says the test")
    assert classify_failure(result) is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            "You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)",
            datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
            id="time-only-is-next-occurrence",
        ),
        pytest.param(
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage"
            "?from=cc_cli_limit_message · your session limit resets 3pm (Asia/Yekaterinburg)",
            datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
            id="later-today",
        ),
        pytest.param(
            "your session limit resets 5:20am (Asia/Yekaterinburg)",
            datetime(2026, 10, 3, 0, 20, tzinfo=UTC),
            id="minutes",
        ),
        pytest.param(
            "You’ve hit your usage limit. Try again at 5:02 PM.",
            datetime(2026, 10, 2, 12, 2, tzinfo=UTC),
            id="codex-local-time",
        ),
        pytest.param(
            "You've hit your usage limit. Try again at Oct 4th, 2026 5:02 PM.",
            datetime(2026, 10, 4, 12, 2, tzinfo=UTC),
            id="codex-other-day",
        ),
        pytest.param(
            "your weekly limit resets Oct 6 at 5am (Asia/Yekaterinburg)",
            datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
            id="claude-date",
        ),
        pytest.param(
            "Rate limit reached, try again in 2 hours 30 minutes",
            NOW + timedelta(hours=2, minutes=30),
            id="duration",
        ),
        pytest.param("You've hit your usage limit. Try again later.", None, id="no-time"),
        pytest.param("resets Sep 26 at 5am (Asia/Yekaterinburg)", None, id="past-date"),
        pytest.param("resets Dec 30 at 5am", None, id="beyond-a-week"),
        pytest.param("resets 13pm", None, id="nonsense-hour"),
    ],
)
def test_parse_reset_reads_the_moment_the_cli_announced(error, expected):
    assert parse_reset(error, NOW, YEKT) == expected


def test_date_without_year_rolls_into_next_year():
    now = datetime(2026, 12, 30, 12, 0, tzinfo=UTC)
    assert parse_reset("resets Jan 2 at 5am", now, UTC) == datetime(2027, 1, 2, 5, 0, tzinfo=UTC)


def test_unknown_time_zone_falls_back_to_local_time():
    assert parse_reset("resets 3pm (Mars/Olympus)", NOW, YEKT) == datetime(
        2026, 10, 2, 10, 0, tzinfo=UTC
    )


def test_quota_cools_down_until_the_announced_reset():
    seconds = cooldown_seconds(
        "quota",
        "You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)",
        NOW,
        failure_seconds=900,
        quota_seconds=3600,
        local_tz=YEKT,
    )
    reset = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    assert seconds == (reset + RESET_GRACE - NOW).total_seconds()


@pytest.mark.parametrize(
    ("kind", "error", "failure", "expected"),
    [
        ("quota", "You've hit your usage limit. Try again later.", 900, 3600),
        ("capacity", "Selected model is at capacity.", 900, 900),
        ("quota", "You've hit your weekly limit · resets 5am", 0, 0),
    ],
    ids=["quota-without-reset", "not-a-quota", "mechanism-off"],
)
def test_cooldown_without_a_reset_uses_the_configured_seconds(kind, error, failure, expected):
    assert (
        cooldown_seconds(kind, error, NOW, failure_seconds=failure, quota_seconds=3600) == expected
    )


@pytest.mark.parametrize(
    ("kind", "error", "expected"),
    [
        ("quota", "You've hit your weekly limit · resets 5am", "group"),
        ("quota", "You've hit your Opus limit · resets 5am", "executor"),
        ("capacity", "Selected model is at capacity.", "executor"),
        ("auth", "Invalid API key · Please run /login", "group"),
        ("network", "timeout", "group"),
    ],
)
def test_account_wide_failures_take_out_the_whole_limit_group(kind, error, expected):
    assert failure_scope(kind, error) == expected


def test_spend_limit_is_rechecked_within_the_quota_window_even_with_a_far_reset():
    # 2026-09-22 23:11Z: «weekly limit resets Sep 26», а claude снова работал в 06:49Z.
    now = datetime(2026, 9, 22, 23, 11, 38, tzinfo=UTC)
    error = (
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage"
        "?from=cc_cli_limit_message · your weekly limit resets Sep 26 at 5am (Asia/Yekaterinburg)"
    )
    assert (
        cooldown_seconds(
            error=error, now=now, kind="quota", failure_seconds=900, quota_seconds=3600
        )
        == 3600
    )
    soon = (
        "You've hit your monthly spend limit · your session limit resets 5:20am"
        " (Asia/Yekaterinburg)"
    )
    early = datetime(2026, 9, 24, 0, 10, 47, tzinfo=UTC)
    reset = datetime(2026, 9, 24, 0, 20, tzinfo=UTC)
    assert (
        cooldown_seconds("quota", soon, early, failure_seconds=900, quota_seconds=3600)
        == (reset + RESET_GRACE - early).total_seconds()
    )


def test_recheck_caps_an_announced_reset_for_a_pool_of_accounts():
    # 2026-10-02: codex через codex-lb сказал «Try again at 5:02 PM» (12:02Z),
    # а в 11:21Z следующий запрос ушёл на свободный аккаунт и прошёл.
    error = "You’ve hit your usage limit. Try again at 5:02 PM."
    now = datetime(2026, 10, 2, 10, 2, 47, tzinfo=UTC)
    common = {"failure_seconds": 900, "quota_seconds": 3600, "local_tz": YEKT}
    assert cooldown_seconds("quota", error, now, recheck=True, **common) == 3600
    assert cooldown_seconds("quota", error, now, **common) > 3600


def test_repeated_hour_at_fall_back_picks_the_nearer_instant():
    # 2026-11-01 в Нью-Йорке 1:00-2:00 бывает дважды. Сообщение в первые 1:40
    # (EDT, 05:40Z): «resets 1:30am» это вторые 1:30 (EST, 06:30Z), а не завтра.
    now = datetime(2026, 11, 1, 5, 40, tzinfo=UTC)
    assert parse_reset("resets 1:30am (America/New_York)", now) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )


def test_nonexistent_time_at_spring_forward_moves_to_the_next_day():
    # 2026-03-08 в Нью-Йорке 2:00-3:00 нет.
    now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert parse_reset("resets 2:30am (America/New_York)", now) == datetime(
        2026, 3, 9, 6, 30, tzinfo=UTC
    )


def test_local_time_across_a_dst_change_uses_the_zone_not_todays_offset(monkeypatch):
    from agent_dispatch.executors import health

    monkeypatch.setenv("TZ", "America/New_York")
    assert str(health.local_zone()) == "America/New_York"
    # 31 октября EDT (-4), сброс 1 ноября уже по EST (-5): 17:00 EST = 22:00Z.
    now = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    assert parse_reset("Try again at Nov 1st, 2026 5:00 PM.", now) == datetime(
        2026, 11, 1, 22, 0, tzinfo=UTC
    )


def test_dated_reset_in_the_repeated_hour_picks_the_future_instant():
    now = datetime(2026, 11, 1, 5, 40, tzinfo=UTC)  # первые 1:40 EDT
    assert parse_reset("resets Nov 1 at 1:30am (America/New_York)", now) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )
