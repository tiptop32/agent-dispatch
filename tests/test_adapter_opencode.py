import os
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.opencode import OpenCodeAdapter

ROOT = Path(__file__).parent


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
