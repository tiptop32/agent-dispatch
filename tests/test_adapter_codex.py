import asyncio
import os
from pathlib import Path

import pytest

from agent_dispatch.config import ExecutorSettings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.codex import CodexAdapter

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
async def test_codex_ok_argv_stdin_and_cleanup(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = CodexAdapter(
        "codex",
        ExecutorSettings(
            adapter="codex", command=str(ROOT / "fakes/codex_ok.sh"), extra_args=["--foo"]
        ),
    )
    result = await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))
    assert result.status == "completed" and result.changed_files == ["a.py"]
    assert (tmp_path / "cap.stdin").read_text() == "do task"
    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args[:3] == ["exec", "--json", "-C"] and args[-2:] == ["--foo", "-"]
    assert not list(tmp_path.glob("agent-dispatch-codex-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake, expected",
    [
        ("codex_bad_schema.sh", "partial"),
        ("codex_fail.sh", "failed"),
        ("codex_no_output.sh", "partial"),
    ],
)
async def test_codex_failure_shapes(git_repo, tmp_path, fake, expected):
    result = await CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command=str(ROOT / "fakes" / fake))
    ).execute(ctx(git_repo, tmp_path))
    assert result.status == expected
    if fake == "codex_bad_schema.sh":
        assert "Additional properties" in result.meta["parse_error"]
    if fake == "codex_fail.sh":
        assert result.error == "boom"


@pytest.mark.asyncio
async def test_codex_check(tmp_path):
    result = await CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command=str(ROOT / "fakes/version_only.sh"))
    ).check()
    assert result.available and result.version == "1.2.3"


@pytest.mark.asyncio
async def test_codex_schema_matches_contract(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = CodexAdapter(
        "codex",
        ExecutorSettings(adapter="codex", command=str(ROOT / "fakes/codex_capture_schema.sh")),
    )
    await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))
    import json

    from agent_dispatch.schemas import load_agent_result_strict_schema

    assert (tmp_path / "cap.schema").read_text() == json.dumps(load_agent_result_strict_schema())


@pytest.mark.asyncio
async def test_codex_non_git_cwd_fails_before_cli(tmp_path):
    adapter = CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command=str(ROOT / "fakes/codex_ok.sh"))
    )
    result = await adapter.execute(ctx(tmp_path, tmp_path))
    assert result.status == "failed" and result.error == f"cwd is not a git repository: {tmp_path}"


@pytest.mark.asyncio
async def test_codex_timeout_cleans_temp_dir(git_repo, tmp_path):
    import tempfile

    # Смотрим только на каталоги, появившиеся во время этого вызова: в системном tmp
    # могут жить каталоги настоящих задач, если демон работает параллельно с тестами.
    def temp_dirs() -> set[Path]:
        return set(Path(tempfile.gettempdir()).glob("agent-dispatch-codex-*"))

    before = await asyncio.to_thread(temp_dirs)
    adapter = CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command=str(ROOT / "fakes/sleep_forever.sh"))
    )
    result = await adapter.execute(
        ctx(git_repo, tmp_path).model_copy(update={"timeout_seconds": 0.2})
    )
    assert result.status == "failed"
    assert await asyncio.to_thread(temp_dirs) - before == set()


@pytest.mark.asyncio
async def test_codex_model_uses_started_event_only(git_repo, tmp_path, monkeypatch):
    from agent_dispatch.executors import codex as module

    class Outcome:
        exit_code = 0
        stdout = '{"type":"turn.completed","model":"wrong"}\n'
        stderr = ""
        timed_out = False
        duration_ms = 1

    async def fake(*args, **kwargs):
        return Outcome()

    monkeypatch.setattr(module, "run_cli", fake)
    result = await CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command="codex", model="configured")
    ).execute(ctx(git_repo, tmp_path))
    assert result.model == "configured"


@pytest.mark.asyncio
async def test_codex_missing_binary_reports_unavailable(tmp_path):
    result = await CodexAdapter(
        "codex", ExecutorSettings(adapter="codex", command="/nonexistent/codex")
    ).check()
    assert not result.available and result.error


@pytest.mark.asyncio
async def test_codex_passes_model_flag_when_configured(git_repo, tmp_path):
    cap = tmp_path / "cap"
    adapter = CodexAdapter(
        "codex/luna",
        ExecutorSettings(
            adapter="codex", command=str(ROOT / "fakes/codex_ok.sh"), model="gpt-5.6-luna"
        ),
    )
    await adapter.execute(ctx(git_repo, tmp_path, FAKE_CAPTURE=str(cap)))
    args = (tmp_path / "cap.argv").read_text().splitlines()
    assert args[-3:] == ["-m", "gpt-5.6-luna", "-"]


@pytest.mark.asyncio
async def test_codex_passes_the_idle_limit_because_it_streams_events(
    git_repo, tmp_path, monkeypatch
):
    from agent_dispatch.executors import codex as module
    from agent_dispatch.executors.process import ProcessOutcome

    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return ProcessOutcome(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(module, "run_cli", fake)
    context = ctx(git_repo, tmp_path).model_copy(update={"idle_timeout_seconds": 900})
    await CodexAdapter("codex", ExecutorSettings(adapter="codex", command="codex")).execute(context)
    assert seen["idle_timeout_seconds"] == 900
