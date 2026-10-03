import hashlib
import os
import subprocess
from pathlib import Path


def git_env() -> dict[str, str]:
    """Окружение без GIT_*.

    Демон может быть запущен из git-хука, где выставлены GIT_DIR и
    GIT_INDEX_FILE. Унаследованные, они увели бы любую git-команду в чужую
    репозиторию вместо рабочего каталога задачи.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def is_git_repo(cwd: str | Path) -> bool:
    """Return whether ``cwd`` is inside a work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def snapshot(cwd: str | Path) -> set[str]:
    """Return changed paths from git status, relative to the repository root."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        check=True,
    )
    paths: set[str] = set()
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries) - 1:
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii")
        paths.add(os.fsdecode(entry[3:]))
        if "R" in status or "C" in status:
            index += 1
    return paths


def fingerprint(cwd: str | Path) -> str:
    """Хеш содержимого рабочей копии: дифф против HEAD плюс неотслеживаемые файлы.

    `snapshot` видит только набор грязных путей, и правка файла, который был
    грязным ещё до запуска, в нём не заметна. Ревью обещает ничего не менять,
    поэтому сравнивается содержимое.
    """
    digest = hashlib.sha256()
    for args in (["diff", "HEAD", "--binary"], ["ls-files", "-o", "--exclude-standard", "-z"]):
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=git_env(), capture_output=True, check=True
        )
        digest.update(result.stdout)
        if args[0] == "ls-files":
            root = Path(cwd)
            for name in filter(None, result.stdout.split(b"\0")):
                try:
                    digest.update((root / os.fsdecode(name)).read_bytes())
                except OSError:
                    digest.update(b"<unreadable>")
    return digest.hexdigest()


def diff(before: set[str], after: set[str]) -> list[str]:
    """Return newly changed paths in sorted order.

    A file modified both before and after the run is intentionally excluded;
    this cannot distinguish edits made by the original agent from executor edits.
    """
    return sorted(after - before)
