"""`redact`: очистка отчёта продовой задачи от явных секретов."""

from __future__ import annotations

import pytest

from agent_dispatch.dispatch.redact import redact_result, redact_text
from agent_dispatch.models import ExecutionResult

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # pragma: allowlist secret
    "MIIEpAIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"  # pragma: allowlist secret
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        (
            "Authorization: Bearer abcdef1234567890xyz",
            "abcdef1234567890xyz",
            "Authorization: Bearer",
        ),
        (f"token in header {JWT} expired", JWT, "expired"),
        ("password=hunter2 for svc", "hunter2", "password="),
        ("DB_PASSWORD = 's3cr et!'", "s3cr et!", "DB_PASSWORD"),  # pragma: allowlist secret
        ("access_token=ya29.a0AfH6SMB&x=1", "ya29.a0AfH6SMB", "&x=1"),
        ("client_secret=Zq9XvT2 next", "Zq9XvT2", "next"),
        ('{"token": "ghp_abcdefgh12345"}', "ghp_abcdefgh12345", '{"token": '),
        (f"key:\n{PEM}\nend", "MIIEpAIBAAKCAQEAu1SU1L", "end"),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B",  # pragma: allowlist secret
            "MIIEvQIBADANBgkqhkiG9w0B",
            "",
        ),
        (
            "dsn postgres://svc_user:hunter2@db.example:5432/orders",  # pragma: allowlist secret
            "hunter2",
            "postgres://svc_user:[REDACTED]@db.example:5432/orders",
        ),
        ("amqp://guest:p%40ss@mq:5672", "p%40ss", "@mq:5672"),  # pragma: allowlist secret
    ],
)
def test_each_secret_kind_is_masked(text, secret, kept):
    redacted, count = redact_text(text)
    assert secret not in redacted
    assert kept in redacted
    assert count == 1


@pytest.mark.parametrize(
    "text",
    [
        "Checked 1520 orders: 3 failed with status 500, the rest are ok.",
        "commit 3f2a9c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b fixed it; short sha 3f2a9c1",
        "max_tokens=4096, tokens: 1200 in, 300 out; token budget is fine",
        "the password reset flow works; Token: expired after 15 min",
        "see https://scm.x5.ru/group/repo/-/merge_requests/12 and http://host:8080/path",
        "user@example.local wrote ssh://git@scm.x5.ru:2222/repo.git",
        "Bearer token is required by the API",
        "secret_count=",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert redact_text(text) == (text, 0)


def test_none_is_left_alone():
    assert redact_text(None) == (None, 0)


def test_result_records_the_fact_without_values():
    result = ExecutionResult(
        status="failed",
        executor="omp/x5",
        model=None,
        summary=f"password=hunter2 and {JWT}",
        error="Bearer abcdef1234567890",
    )
    redact_result(result)

    assert "hunter2" not in result.summary and JWT not in result.summary
    assert "abcdef1234567890" not in (result.error or "")  # pragma: allowlist secret
    assert result.meta["redacted"] == {"replacements": 3, "fields": ["summary", "error"]}
    assert "hunter2" not in repr(result.meta)


def test_clean_result_gets_no_mark():
    result = ExecutionResult(status="completed", executor="omp/x5", model=None, summary="42 rows")
    redact_result(result)
    assert result.summary == "42 rows" and "redacted" not in result.meta
