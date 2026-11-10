import subprocess
from pathlib import Path

import pytest

from agent_dispatch.executors.workspace import DiffStatSnapshot
from agent_dispatch.mcp.server import _task_text
from agent_dispatch.models import ExecutionResult, TaskView
from tests.test_review_by import _make, _req


def test_diff_stat_excludes_callers_edits_and_preserves_index(git_repo):
    path = git_repo / "a.py"
    path.write_text("caller = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True)
    (git_repo / "caller.txt").write_text("not executor work\n")
    index = (git_repo / ".git" / "index").read_bytes()
    before = DiffStatSnapshot(git_repo)
    path.write_text("caller = 1\nexecutor = 2\n")
    (git_repo / "новый файл.txt").write_text("one\ntwo\n")
    assert before.diff_stat(["a.py", "новый файл.txt"]) == {
        "files": 2,
        "added": 3,
        "removed": 0,
    }
    assert (git_repo / ".git" / "index").read_bytes() == index
    blob = subprocess.run(
        ["git", "hash-object", "caller.txt"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", blob], cwd=git_repo, capture_output=True
        ).returncode
        != 0
    )
    before.close()


def test_diff_stat_handles_deletions_binary_and_literal_paths(git_repo):
    (git_repo / "[x].txt").write_text("old\n")
    (git_repo / "binary.dat").write_bytes(b"\x00old")
    before = DiffStatSnapshot(git_repo)
    (git_repo / "a.py").unlink()
    (git_repo / "[x].txt").write_text("new\n")
    (git_repo / "binary.dat").write_bytes(b"\x00new")
    assert before.diff_stat(["a.py", "[x].txt", "binary.dat"]) == {
        "files": 3,
        "added": 1,
        "removed": 2,
    }


def test_diff_stat_includes_force_staged_ignored_files(git_repo):
    (git_repo / ".gitignore").write_text("secret.txt\n")
    path = git_repo / "secret.txt"
    path.write_text("before\n")
    subprocess.run(["git", "add", "-f", "secret.txt"], cwd=git_repo, check=True)
    before = DiffStatSnapshot(git_repo)
    path.write_text("before\nafter\n")
    assert before.diff_stat(["secret.txt"]) == {"files": 1, "added": 1, "removed": 0}
    before.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["in_place", "worktree"])
async def test_dispatch_measures_executor_delta(tmp_path, git_repo, mode):
    _, adapters, dispatcher = await _make(tmp_path, review_by=None, workspace_mode=mode)
    (git_repo / "a.py").write_text("caller = 1\n")

    async def edit(ctx):
        path = Path(ctx.cwd) / "a.py"
        path.write_text(path.read_text() + "executor = 2\n")
        return ExecutionResult(
            status="completed", executor="omp", model=None, summary="done", changed_files=["a.py"]
        )

    adapters["omp"].execute = edit
    try:
        record = await dispatcher.submit(_req(git_repo, allow_escalation=False))
        done = await dispatcher.wait(record.task_id, 10)
        assert done.result.meta["diff_stat"] == {"files": 1, "added": 1, "removed": 0}
        view = TaskView.model_validate({**done.model_dump(), "log_tail": ""})
        assert "diff_stat: 1 files, +1 -0" in _task_text(view)
    finally:
        await dispatcher.shutdown()


def test_compact_review_keeps_multiline_findings():
    from agent_dispatch.mcp.server import _review_line

    findings = "first finding\n" + "second finding " * 200
    text = _review_line({"verdict": "fail", "by": "rev", "summary": findings})
    assert "first finding" in text and "second finding" in text
    assert len(text.split(" — ", 1)[1]) == 1500
