import os
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.claude import ClaudeAdapter

ROOT = Path(__file__).parent


def context(repo, tmp_path, **env):
    return RunContext(
        cwd=str(repo),
        timeout_seconds=3,
        env={"PATH": os.environ.get("PATH", ""), **env},
        log_path=tmp_path / "run.log",
        task_id="t1",
        prompt="fix it",
    )


@pytest.mark.asyncio
async def test_claude_execute_contract_and_usage(git_repo, tmp_path):
    capture = tmp_path / "capture"
    adapter = ClaudeAdapter(
        "claude/test",
        ExecutorSettings(
            adapter="claude", command=str(ROOT / "fakes/claude_ok.sh"), extra_args=["--verbose"]
        ),
    )
    result = await adapter.execute(context(git_repo, tmp_path, FAKE_CAPTURE=str(capture)))
    assert result.status == "completed", result
    assert result.changed_files == ["a.py"]
    assert result.model == "claude-opus-5"
    assert result.usage.cost_usd == 0.392015
    assert (tmp_path / "capture.stdin").read_text() == "fix it"
    assert (tmp_path / "capture.argv").read_text().splitlines() == [
        "-p",
        "--output-format",
        "json",
        "--add-dir",
        str(git_repo),
        "--verbose",
    ]
    assert result.meta["log_path"] == str(tmp_path / "run.log")


@pytest.mark.asyncio
async def test_claude_check_and_missing_binary(tmp_path):
    ok = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/version_only.sh"))
    )
    assert (await ok.check()).version == "1.2.3"
    missing = ClaudeAdapter(
        "missing", ExecutorSettings(adapter="claude", command="/no/such/command")
    )
    availability = await missing.check()
    assert not availability.available and availability.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake, expected", [("claude_noblock.sh", "partial"), ("claude_error.sh", "failed")]
)
async def test_claude_special_results(git_repo, tmp_path, fake, expected):
    adapter = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes" / fake))
    )
    result = await adapter.execute(context(git_repo, tmp_path))
    assert result.status == expected
    if expected == "failed":
        assert result.error == "Something broke"


@pytest.mark.asyncio
async def test_claude_timeout(git_repo, tmp_path):
    adapter = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/sleep_forever.sh"))
    )
    ctx = context(git_repo, tmp_path).model_copy(update={"timeout_seconds": 0})
    result = await adapter.execute(ctx)
    assert result.status == "failed" and result.error == "timeout"


@pytest.mark.asyncio
async def test_claude_env_reaches_cli(git_repo, tmp_path):
    capture = tmp_path / "env"
    adapter = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/env_capture.sh"))
    )
    result = await adapter.execute(context(git_repo, tmp_path, FAKE_CAPTURE=str(capture)))
    assert result.status == "partial"
    assert "FAKE_CAPTURE" in capture.read_text()


@pytest.mark.asyncio
async def test_claude_non_git_cwd_fails_before_cli(tmp_path):
    adapter = ClaudeAdapter(
        "claude", ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/claude_ok.sh"))
    )
    result = await adapter.execute(context(tmp_path, tmp_path))
    assert result.status == "failed" and result.error == f"cwd is not a git repository: {tmp_path}"


@pytest.mark.asyncio
async def test_claude_check_preserves_base_environment(tmp_path):
    capture = tmp_path / "env"
    adapter = ClaudeAdapter(
        "claude",
        ExecutorSettings(adapter="claude", command=str(ROOT / "fakes/version_capture.sh")),
        base_env={"PATH": os.environ["PATH"], "HOME": "/x", "FAKE_CAPTURE": str(capture)},
    )
    assert (await adapter.check()).available
    assert "HOME=/x" in capture.read_text()


@pytest.mark.asyncio
async def test_claude_passes_model_flag_when_configured(git_repo, tmp_path):
    capture = tmp_path / "capture"
    adapter = ClaudeAdapter(
        "claude/sonnet",
        ExecutorSettings(
            adapter="claude", command=str(ROOT / "fakes/claude_ok.sh"), model="sonnet"
        ),
    )
    await adapter.execute(context(git_repo, tmp_path, FAKE_CAPTURE=str(capture)))
    argv = (tmp_path / "capture.argv").read_text().splitlines()
    assert argv[-2:] == ["--model", "sonnet"]
