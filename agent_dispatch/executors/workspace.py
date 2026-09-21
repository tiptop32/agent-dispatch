import os
import subprocess
from pathlib import Path


def is_git_repo(cwd: str | Path) -> bool:
    """Return whether ``cwd`` is inside a work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
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


def diff(before: set[str], after: set[str]) -> list[str]:
    """Return newly changed paths in sorted order.

    A file modified both before and after the run is intentionally excluded;
    this cannot distinguish edits made by the original agent from executor edits.
    """
    return sorted(after - before)
