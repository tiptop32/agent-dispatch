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
