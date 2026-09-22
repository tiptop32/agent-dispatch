import subprocess
from pathlib import Path

import pytest

from agent_dispatch.executors import worktree


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_create_gives_an_isolated_tree_on_its_own_branch(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t1", "agent-dispatch/t1")

    assert tree.path.is_dir() and (tree.path / "a.py").read_text() == "x = 1\n"
    assert tree.branch in git(git_repo, "branch", "--list", "agent-dispatch/t1")
    # Правка в worktree не видна в исходной копии.
    (tree.path / "a.py").write_text("x = 2\n")
    assert (git_repo / "a.py").read_text() == "x = 1\n"


def test_two_worktrees_do_not_collide(git_repo: Path, tmp_path: Path):
    first = worktree.create(git_repo, tmp_path / "wt" / "a", "agent-dispatch/a")
    second = worktree.create(git_repo, tmp_path / "wt" / "b", "agent-dispatch/b")

    (first.path / "a.py").write_text("first\n")
    (second.path / "a.py").write_text("second\n")

    assert (first.path / "a.py").read_text() == "first\n"
    assert (second.path / "a.py").read_text() == "second\n"


def test_patch_carries_edits_and_new_files_into_the_original_copy(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    (tree.path / "new.py").write_text("y = 3\n")

    patch = worktree.build_patch(tree)
    patch_path = tmp_path / "t.patch"
    patch_path.write_text(patch)
    worktree.apply_patch(git_repo, patch_path)

    assert (git_repo / "a.py").read_text() == "x = 2\n"
    assert (git_repo / "new.py").read_text() == "y = 3\n"


def test_patch_keeps_unrelated_uncommitted_work_of_the_original_copy(
    git_repo: Path, tmp_path: Path
):
    (git_repo / "mine.py").write_text("mine = 1\n")
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")

    patch_path = tmp_path / "t.patch"
    patch_path.write_text(worktree.build_patch(tree))
    worktree.apply_patch(git_repo, patch_path)

    assert (git_repo / "mine.py").read_text() == "mine = 1\n"
    assert (git_repo / "a.py").read_text() == "x = 2\n"


def test_conflicting_change_in_the_original_copy_is_an_error_not_a_silent_overwrite(
    git_repo: Path, tmp_path: Path
):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("from worktree\n")
    (git_repo / "a.py").write_text("from the agent\n")

    patch_path = tmp_path / "t.patch"
    patch_path.write_text(worktree.build_patch(tree))
    with pytest.raises(worktree.WorktreeError):
        worktree.apply_patch(git_repo, patch_path)
    assert (git_repo / "a.py").read_text() == "from the agent\n"


def test_changed_files_are_counted_inside_the_worktree(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    (tree.path / "new.py").write_text("y = 3\n")

    assert worktree.changed_files(tree) == ["a.py", "new.py"]


def test_empty_run_produces_an_empty_patch(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    assert worktree.build_patch(tree).strip() == ""


def test_remove_drops_the_tree_and_optionally_the_branch(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    worktree.remove(tree, keep_branch=False)

    assert not tree.path.exists()
    assert git(git_repo, "branch", "--list", "agent-dispatch/t").strip() == ""


def test_remove_keeps_the_branch_when_the_result_was_not_integrated(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=tree.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "wip"],
        cwd=tree.path,
        check=True,
        capture_output=True,
    )
    worktree.remove(tree, keep_branch=True)

    assert not tree.path.exists()
    assert "agent-dispatch/t" in git(git_repo, "branch", "--list", "agent-dispatch/t")


def test_list_worktrees_shows_only_ours(git_repo: Path, tmp_path: Path):
    worktree.create(git_repo, tmp_path / "wt" / "mine", "agent-dispatch/mine")
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/x", str(tmp_path / "wt" / "other"), "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    found = worktree.list_worktrees(git_repo, "agent-dispatch")

    assert [tree.branch for tree in found] == ["agent-dispatch/mine"]


def test_not_a_repository_is_a_clear_error(tmp_path: Path):
    with pytest.raises(worktree.WorktreeError, match="not a git repository"):
        worktree.repo_root(tmp_path)
