"""Изолированный git worktree на исполнителя.

Два исполнителя в одной рабочей копии затаптывают друг друга: при fan-out
сабагенты идут параллельно и правят одни файлы. Worktree даёт каждому свою
копию дерева и свою ветку, а результат возвращается в исходную рабочую копию
патчем. Worktree создаётся от HEAD, поэтому незакоммиченные изменения исходной
копии исполнителю не видны; об этом говорится в Task Package.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .workspace import git_env, snapshot


class WorktreeError(RuntimeError):
    """Не удалось создать, применить или убрать worktree."""


@dataclass
class Worktree:
    path: Path
    branch: str
    repo: Path


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, check=False
    )


def repo_root(cwd: str | Path) -> Path:
    result = _git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        raise WorktreeError(f"not a git repository: {cwd}")
    return Path(result.stdout.strip())


def create(cwd: str | Path, directory: Path, branch: str) -> Worktree:
    """Создать worktree от HEAD исходной копии на новой ветке."""
    root = repo_root(cwd)
    directory.parent.mkdir(parents=True, exist_ok=True)
    result = _git(["worktree", "add", "-b", branch, str(directory), "HEAD"], root)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git worktree add failed")
    return Worktree(path=directory, branch=branch, repo=root)


def build_patch(worktree: Worktree) -> str:
    """Все изменения worktree одним патчем, включая новые файлы.

    Файлы добавляются в индекс worktree, поэтому `diff --cached` видит и
    неотслеживаемые; индекс worktree свой и на исходную копию не влияет.
    """
    add = _git(["add", "-A"], worktree.path)
    if add.returncode != 0:
        raise WorktreeError(add.stderr.strip() or "git add failed")
    diff = _git(["diff", "--cached", "--binary", "HEAD"], worktree.path)
    if diff.returncode != 0:
        raise WorktreeError(diff.stderr.strip() or "git diff failed")
    return diff.stdout


def apply_patch(cwd: str | Path, patch_path: Path) -> None:
    """Применить патч в исходную рабочую копию, сохранив её незакоммиченные правки."""
    result = _git(["apply", "--3way", "--whitespace=nowarn", str(patch_path)], repo_root(cwd))
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git apply failed")


def changed_files(worktree: Worktree) -> list[str]:
    return sorted(snapshot(worktree.path))


def remove(worktree: Worktree, *, keep_branch: bool) -> None:
    """Убрать worktree; ветку удалить, только если результат уже перенесён."""
    result = _git(["worktree", "remove", "--force", str(worktree.path)], worktree.repo)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git worktree remove failed")
    if not keep_branch:
        _git(["branch", "-D", worktree.branch], worktree.repo)


def list_worktrees(cwd: str | Path, prefix: str) -> list[Worktree]:
    """Worktree этой репы, созданные AgentDispatch (по префиксу ветки)."""
    root = repo_root(cwd)
    result = _git(["worktree", "list", "--porcelain"], root)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git worktree list failed")
    found: list[Worktree] = []
    path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and path is not None:
            branch = line[len("branch ") :].removeprefix("refs/heads/")
            if branch.startswith(f"{prefix}/"):
                found.append(Worktree(path=path, branch=branch, repo=root))
            path = None
    return found
