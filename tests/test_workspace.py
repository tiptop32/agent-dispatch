import subprocess
from pathlib import Path

from agent_dispatch.executors.workspace import diff, is_git_repo, snapshot


def test_is_git_repo_returns_true_for_git_repository(git_repo: Path) -> None:
    assert is_git_repo(git_repo) is True


def test_is_git_repo_returns_false_for_non_repository(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False


def test_diff_reports_modified_and_created_files_sorted(git_repo: Path) -> None:
    before = snapshot(git_repo)
    (git_repo / "a.py").write_text("x = 2\n")
    (git_repo / "b.py").write_text("y = 1\n")

    assert diff(before, snapshot(git_repo)) == ["a.py", "b.py"]


def test_snapshot_includes_untracked_and_modified_files(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 2\n")
    (git_repo / "new.py").write_text("new = True\n")

    assert snapshot(git_repo) == {"a.py", "new.py"}


def test_snapshot_uses_new_path_for_renamed_file(git_repo: Path) -> None:
    (git_repo / "b.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "b.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add b"], cwd=git_repo, check=True)
    subprocess.run(["git", "mv", "b.py", "c.py"], cwd=git_repo, check=True)

    assert snapshot(git_repo) == {"c.py"}


def test_snapshot_preserves_spaces_and_unicode_in_renames(git_repo: Path) -> None:
    subprocess.run(["git", "mv", "a.py", "б.py"], cwd=git_repo, check=True)
    (git_repo / "файл с пробелом.py").write_text("y = 1\n")

    assert snapshot(git_repo) == {"файл с пробелом.py", "б.py"}
