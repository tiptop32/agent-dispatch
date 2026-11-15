import os
import subprocess
from pathlib import Path

from agent_dispatch.executors.workspace import (
    DiffStatSnapshot,
    diff,
    fingerprint,
    is_git_repo,
    review_input_patch,
    snapshot,
)


def _commit_subdir_file(repo: Path) -> Path:
    """Закоммитить pkg/b.py: удаление из подкаталога ломало разрешение путей."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    (repo / "pkg").mkdir()
    (repo / "pkg" / "b.py").write_text("one\ntwo\n")
    for args in (
        ["add", "pkg/b.py"],
        ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "pkg"],
    ):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    return repo / "pkg"


def test_diff_stat_counts_deleted_file_when_cwd_is_subdirectory(git_repo: Path) -> None:
    cwd = _commit_subdir_file(git_repo)
    snap = DiffStatSnapshot(cwd)
    try:
        (cwd / "b.py").unlink()
        # Адаптер отдаёт пути от корня репы: pkg/b.py, а не b.py.
        assert snap.diff_stat(["pkg/b.py"]) == {"files": 1, "added": 0, "removed": 2}
    finally:
        snap.close()


def test_review_input_patch_keeps_deletion_when_cwd_is_subdirectory(git_repo: Path) -> None:
    cwd = _commit_subdir_file(git_repo)
    (cwd / "b.py").unlink()
    patch, oversized = review_input_patch(cwd, ["pkg/b.py"], limit=1 << 20)
    assert not oversized
    assert b"deleted file mode" in patch and b"pkg/b.py" in patch


def test_review_input_patch_skips_path_missing_from_head_and_disk(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("changed\n")
    patch, _ = review_input_patch(git_repo, ["a.py", "gone.py"], limit=1 << 20)
    assert b"a.py" in patch and b"gone.py" not in patch


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

    assert set(snapshot(git_repo)) == {"a.py", "new.py"}


def test_snapshot_uses_new_path_for_renamed_file(git_repo: Path) -> None:
    (git_repo / "b.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "b.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add b"], cwd=git_repo, check=True)
    subprocess.run(["git", "mv", "b.py", "c.py"], cwd=git_repo, check=True)

    assert set(snapshot(git_repo)) == {"c.py"}


def test_snapshot_preserves_spaces_and_unicode_in_renames(git_repo: Path) -> None:
    subprocess.run(["git", "mv", "a.py", "б.py"], cwd=git_repo, check=True)
    (git_repo / "файл с пробелом.py").write_text("y = 1\n")

    assert set(snapshot(git_repo)) == {"файл с пробелом.py", "б.py"}


def test_diff_reports_re_edited_dirty_file(git_repo: Path) -> None:
    # Файл был грязным до запуска, исполнитель переписал его ещё раз: без
    # сравнения содержимого такая правка исчезала из changed_files.
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(git_repo)
    (git_repo / "a.py").write_text("x = 3\n")

    assert diff(before, snapshot(git_repo)) == ["a.py"]


def test_diff_skips_unchanged_dirty_file(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(git_repo)

    assert diff(before, snapshot(git_repo)) == []


def test_diff_reports_dirty_file_reverted_to_head(git_repo: Path) -> None:
    # Исполнитель откатил чужую правку: это тоже его изменение.
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(git_repo)
    (git_repo / "a.py").write_text("x = 1\n")

    assert diff(before, snapshot(git_repo)) == ["a.py"]


def test_diff_reports_edited_untracked_file(git_repo: Path) -> None:
    (git_repo / "new.py").write_text("new = 1\n")
    before = snapshot(git_repo)
    (git_repo / "new.py").write_text("new = 2\n")

    assert diff(before, snapshot(git_repo)) == ["new.py"]


def test_fingerprint_hashes_untracked_files_from_repository_root(git_repo: Path) -> None:
    subdir = git_repo / "pkg"
    subdir.mkdir()
    outside = git_repo / "outside.txt"
    outside.write_text("before\n")

    before = fingerprint(subdir)
    outside.write_text("after\n")

    assert fingerprint(subdir) != before


def test_diff_reports_mode_change_on_dirty_file(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(git_repo)
    (git_repo / "a.py").chmod(0o755)

    assert diff(before, snapshot(git_repo)) == ["a.py"]


def test_diff_reports_deleted_dirty_file(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(git_repo)
    (git_repo / "a.py").unlink()

    assert diff(before, snapshot(git_repo)) == ["a.py"]


def test_diff_reports_changed_symlink_target(git_repo: Path) -> None:
    (git_repo / "one.txt").write_text("one\n")
    (git_repo / "two.txt").write_text("two\n")
    link = git_repo / "link"
    link.symlink_to(git_repo / "one.txt")
    before = snapshot(git_repo)
    link.unlink()
    link.symlink_to(git_repo / "two.txt")

    assert diff(before, snapshot(git_repo)) == ["link"]


def test_diff_reports_re_edited_dirty_file_from_a_subdirectory_cwd(git_repo: Path) -> None:
    # Пути porcelain идут от корня репозитория: при cwd в подкаталоге хеш от
    # cwd давал `<deleted>` в обоих снимках, и правка пропадала.
    sub = git_repo / "sub"
    sub.mkdir()
    (git_repo / "a.py").write_text("x = 2\n")
    before = snapshot(sub)
    (git_repo / "a.py").write_text("x = 3\n")

    assert diff(before, snapshot(sub)) == ["a.py"]
