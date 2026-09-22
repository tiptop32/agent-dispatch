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


def test_commit_puts_the_work_on_the_branch(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    (tree.path / "new.py").write_text("y = 3\n")

    sha = worktree.commit(tree, "agent-dispatch: task deadbeef")

    assert sha and len(sha) == 40
    assert git(tree.path, "rev-parse", "HEAD").strip() == sha
    assert git(tree.path, "status", "--porcelain").strip() == ""
    assert git(tree.path, "rev-list", "--count", f"{tree.base}..HEAD").strip() == "1"


def test_commit_returns_none_when_the_executor_changed_nothing(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")

    assert worktree.commit(tree, "empty") is None
    assert git(tree.path, "rev-parse", "HEAD").strip() == tree.base


def test_commit_does_not_run_repository_hooks(git_repo: Path, tmp_path: Path):
    # Служебный коммит на черновой ветке не должен зависеть от pre-commit репы:
    # тот гоняет тесты и падал бы на любой недоделанной задаче плана.
    hook = git_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")

    assert worktree.commit(tree, "despite the hook") is not None


def test_patch_is_the_same_before_and_after_the_commit(git_repo: Path, tmp_path: Path):
    # Патч отсчитывается от базы, а не от HEAD: иначе промежуточный коммит
    # съедал бы его целиком и в рабочую копию ничего не возвращалось.
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    (tree.path / "new.py").write_text("y = 3\n")

    before = worktree.build_patch(tree)
    worktree.commit(tree, "wip")
    after = worktree.build_patch(tree)

    assert before == after
    assert "new.py" in after


def test_committed_work_survives_removing_the_worktree(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    sha = worktree.commit(tree, "wip")

    worktree.remove(tree, keep_branch=True)

    assert not tree.path.exists()
    assert git(git_repo, "rev-parse", "agent-dispatch/t").strip() == sha
    assert git(git_repo, "show", f"{sha}:a.py") == "x = 2\n"


def test_list_branches_shows_branches_whose_worktree_is_gone(git_repo: Path, tmp_path: Path):
    attached = worktree.create(git_repo, tmp_path / "wt" / "kept", "agent-dispatch/kept")
    orphan = worktree.create(git_repo, tmp_path / "wt" / "gone", "agent-dispatch/gone")
    (orphan.path / "a.py").write_text("x = 2\n")
    worktree.commit(orphan, "wip")
    worktree.remove(orphan, keep_branch=True)

    assert worktree.list_branches(git_repo, "agent-dispatch") == ["agent-dispatch/gone"]
    assert attached.branch not in worktree.list_branches(git_repo, "agent-dispatch")


def test_delete_branch_removes_an_orphan(git_repo: Path, tmp_path: Path):
    tree = worktree.create(git_repo, tmp_path / "wt" / "t", "agent-dispatch/t")
    (tree.path / "a.py").write_text("x = 2\n")
    worktree.commit(tree, "wip")
    worktree.remove(tree, keep_branch=True)

    worktree.delete_branch(git_repo, "agent-dispatch/t")

    assert worktree.list_branches(git_repo, "agent-dispatch") == []


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
