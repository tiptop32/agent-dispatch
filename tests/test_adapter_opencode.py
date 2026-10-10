import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.opencode import OpenCodeAdapter
from agent_dispatch.executors.process import ProcessOutcome

ROOT = Path(__file__).parent


def parse_stdout(stdout: str):
    adapter = OpenCodeAdapter(
        "opencode", ExecutorSettings(adapter="opencode", command="opencode", model="m")
    )
    return adapter._parse_result(ProcessOutcome(0, stdout, "", False, 12), [])


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


VALID_RESULT = (
    '```agent-dispatch-result\n{"status":"completed","summary":"ok","changed_files":[]}\n```'
)


def ctx(repo, tmp_path, **env):
    return RunContext(
        cwd=str(repo),
        timeout_seconds=3,
        env={"PATH": os.environ.get("PATH", ""), **env},
        log_path=tmp_path / "run.log",
        task_id="t",
        prompt="do task",
    )


@pytest.mark.asyncio
async def test_opencode_ok_argv_model_usage(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode/kimi",
        ExecutorSettings(
            adapter="opencode",
            command=str(ROOT / "fakes/opencode_ok.sh"),
            model="kimi",
            extra_args=["--quiet"],
        ),
    )
    result = await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))
    assert (
        result.status == "completed" and result.model == "kimi" and result.changed_files == ["a.py"]
    )
    assert result.usage.input_tokens == 81586 and result.usage.output_tokens == 71
    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args[:5] == ["run", "--format", "json", "--model", "kimi"]
    assert "--dir" not in args
    assert args[-3:] == ["--quiet", "--auto", "do task"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake, expected", [("opencode_noblock.sh", "partial"), ("opencode_error.sh", "failed")]
)
async def test_opencode_special_results(git_repo, tmp_path, fake, expected):
    result = await OpenCodeAdapter(
        "opencode",
        ExecutorSettings(adapter="opencode", command=str(ROOT / "fakes" / fake), model="m"),
    ).execute(ctx(git_repo, tmp_path))
    assert result.status == expected
    if expected == "failed":
        assert result.error == "No cookie auth credentials found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_args", "expected_tail"),
    [
        (["--no-auto"], ["--no-auto", "do task"]),
        (["--auto=false"], ["--auto=false", "do task"]),
        (["--auto", "false"], ["--auto", "false", "do task"]),
        (["--auto"], ["--auto", "do task"]),
        (["--auto=true"], ["--auto=true", "do task"]),
        (["--auto", "true"], ["--auto", "true", "do task"]),
    ],
)
async def test_opencode_task_preserves_explicit_auto_configuration(
    git_repo, tmp_path, extra_args, expected_tail
):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode",
            command=str(ROOT / "fakes/opencode_ok.sh"),
            model="m",
            extra_args=extra_args,
        ),
    )

    result = await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))

    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args[-len(expected_tail) :] == expected_tail
    assert result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_args", "expected_tail"),
    [
        (["--agent=build", "--auto"], ["--agent", "plan", "do task"]),
        (["--agent", "build", "--auto=true"], ["--agent", "plan", "do task"]),
        (["--auto", "true"], ["--agent", "plan", "do task"]),
        (["--no-auto"], ["--no-auto", "--agent", "plan", "do task"]),
        (["--auto=false"], ["--auto=false", "--agent", "plan", "do task"]),
        (["--auto", "false"], ["--auto", "false", "--agent", "plan", "do task"]),
    ],
)
async def test_opencode_read_only_forces_plan_and_removes_only_auto_enable(
    git_repo, tmp_path, extra_args, expected_tail
):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode",
            command=str(ROOT / "fakes/opencode_ok.sh"),
            model="m",
            extra_args=extra_args,
        ),
    )
    review = ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)).model_copy(update={"read_only": True})

    result = await adapter.execute(review)

    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args[-len(expected_tail) :] == expected_tail
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_opencode_read_only_env_allows_tmp_external_directory(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode", command=str(ROOT / "fakes/opencode_env_capture.sh"), model="m"
        ),
    )
    review = ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)).model_copy(update={"read_only": True})

    result = await adapter.execute(review)

    assert result.status == "completed"
    lines = (tmp_path / "cap.env").read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    permission = json.loads(env["OPENCODE_PERMISSION"])
    assert permission["external_directory"]["/tmp/*"] == "allow"
    assert permission["external_directory"]["/private/tmp/*"] == "allow"
    assert permission["external_directory"][f"{tempfile.gettempdir()}/*"] == "allow"


@pytest.mark.asyncio
async def test_opencode_normal_task_env_has_no_permission_override(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode", command=str(ROOT / "fakes/opencode_env_capture.sh"), model="m"
        ),
    )

    result = await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))

    assert result.status == "completed"
    lines = (tmp_path / "cap.env").read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    assert "OPENCODE_PERMISSION" not in env


@pytest.mark.asyncio
async def test_opencode_check(tmp_path):
    result = await OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode", command=str(ROOT / "fakes/version_only.sh"), model="m"
        ),
    ).check()
    assert result.version == "1.2.3"


@pytest.mark.asyncio
async def test_opencode_non_git_cwd_fails_before_cli(tmp_path):
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(adapter="opencode", command=str(ROOT / "fakes/opencode_ok.sh"), model="m"),
    )
    result = await adapter.execute(ctx(tmp_path, tmp_path))
    assert result.status == "failed" and result.error == f"cwd is not a git repository: {tmp_path}"


def test_opencode_requires_model():
    settings = ExecutorSettings.model_construct(adapter="opencode", model=None)
    with pytest.raises(ValueError, match="opencode executor requires model"):
        OpenCodeAdapter("opencode", settings)


def test_opencode_joins_text_events_with_newlines_before_parsing_result():
    result = parse_stdout(
        stream(
            {"type": "text", "part": {"text": "Готово."}},
            {"type": "text", "part": {"text": VALID_RESULT}},
        )
    )

    assert result.status == "completed"


def test_opencode_permission_rejection_without_result_is_failed():
    result = parse_stdout(
        stream(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "state": {
                        "status": "error",
                        "input": {"filePath": "/Users/me/other-repo/file.py"},
                        "error": "The user rejected permission to use this specific tool call.",
                    },
                },
            }
        )
    )

    assert result.status == "failed"
    assert result.error == ("opencode permission rejected: read: /Users/me/other-repo/file.py")


def test_opencode_permission_rejection_with_result_is_reported_in_meta():
    command = "cp -R /abs/.ref .ref && ls .ref"
    result = parse_stdout(
        stream(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "error",
                        "input": {"command": command},
                        "error": "The user rejected permission to use this specific tool call.",
                    },
                },
            },
            {"type": "text", "part": {"text": VALID_RESULT}},
        )
    )

    assert result.status == "completed"
    assert result.meta["permission_rejected"] == [f"bash: {command}"]


STALE_PWD = "/nonexistent/stale-opencode-cwd"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_opencode_v2_runs_from_pwd_env_without_dir_flag(git_repo, tmp_path, read_only):
    cap = tmp_path / "cap"
    original = ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap), PWD=STALE_PWD)
    original_snapshot = dict(original.env)
    review = original.model_copy(update={"read_only": read_only})
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode", command=str(ROOT / "fakes/opencode_pwd.py"), model="m"
        ),
    )

    result = await adapter.execute(review)

    assert result.status == "completed"
    # v2 выбирает PWD из окружения, а не getcwd: устаревший PWD вызывающего
    # должен быть перезаписан каталогом задачи.
    assert (tmp_path / "cap.pwd").read_text().strip() == str(git_repo)
    argv = (tmp_path / "cap.argv").read_text().splitlines()
    assert "--dir" not in argv
    assert argv[-1] == "do task"
    if read_only:
        env_lines = (tmp_path / "cap.env").read_text().splitlines()
        env = dict(line.split("=", 1) for line in env_lines if "=" in line)
        assert "OPENCODE_PERMISSION" in env
    # Адаптер не имеет права мутировать входное окружение вызывающего.
    assert original.env == original_snapshot
    assert original.env["PWD"] == STALE_PWD


def test_opencode_v2_error_message_fails():
    result = parse_stdout(
        stream(
            {
                "type": "error",
                "error": {"type": "provider.auth", "message": "User not found.", "status": 401},
            }
        )
    )
    assert result.status == "failed"
    assert result.error == "User not found."


def test_opencode_v2_error_message_fixture_from_live_probe():
    stdout = (ROOT / "fixtures/agent_output/opencode_v2_provider_error.jsonl").read_text()
    result = parse_stdout(stdout)
    assert result.status == "failed"
    assert result.error == "User not found."


def test_opencode_error_last_event_wins():
    result = parse_stdout(
        stream(
            {"type": "error", "error": {"data": {"message": "first"}}},
            {"type": "error", "error": {"data": {"message": "second"}}},
        )
    )
    assert result.status == "failed" and result.error == "second"


def test_opencode_error_last_event_wins_across_v1_and_v2():
    result = parse_stdout(
        stream(
            {"type": "error", "error": {"data": {"message": "first"}}},
            {"type": "error", "error": {"type": "provider.no-route", "message": "second"}},
        )
    )
    assert result.status == "failed" and result.error == "second"


def test_opencode_v1_error_falls_back_to_name_and_type():
    by_name = parse_stdout(stream({"type": "error", "error": {"name": "APIError"}}))
    assert by_name.status == "failed" and by_name.error == "APIError"
    by_type = parse_stdout(stream({"type": "error", "error": {"type": "unknown_error"}}))
    assert by_type.status == "failed" and by_type.error == "unknown_error"


def test_opencode_step_finish_ignores_non_dict_tokens():
    result = parse_stdout(
        stream(
            {"type": "step_finish", "part": {"tokens": "oops", "cost": 0.5}},
            {"type": "text", "part": {"text": VALID_RESULT}},
        )
    )
    assert result.status == "completed"
    assert result.usage.input_tokens == 0 and result.usage.output_tokens == 0
    assert result.usage.cost_usd == 0.5


def test_opencode_v2_probe_fixture_parses_session_and_usage():
    stdout = (ROOT / "fixtures/agent_output/opencode_v2_run.jsonl").read_text()
    result = parse_stdout(stdout)
    assert result.status == "completed"
    assert result.usage.input_tokens == 6248 and result.usage.output_tokens == 108
    assert result.usage.cost_usd == 0.0
    assert result.meta["session_id"] == "ses_probe000000000000000000000"


@pytest.mark.asyncio
async def test_opencode_check_preserves_base_environment(tmp_path):
    capture = tmp_path / "env"
    adapter = OpenCodeAdapter(
        "opencode",
        ExecutorSettings(
            adapter="opencode", command=str(ROOT / "fakes/version_capture.sh"), model="m"
        ),
        base_env={"PATH": os.environ["PATH"], "HOME": "/x", "FAKE_CAPTURE": str(capture)},
    )
    assert (await adapter.check()).available
    assert "HOME=/x" in capture.read_text()


@pytest.mark.asyncio
async def test_opencode_missing_binary_reports_unavailable(tmp_path):
    result = await OpenCodeAdapter(
        "opencode", ExecutorSettings(adapter="opencode", command="/nonexistent/opencode", model="m")
    ).check()
    assert not result.available and result.error
