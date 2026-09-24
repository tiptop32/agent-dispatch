import json
import os
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
    assert args[:7] == ["run", "--format", "json", "--dir", str(git_repo), "--model", "kimi"]
    assert args[-2:] == ["--quiet", "do task"]


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
