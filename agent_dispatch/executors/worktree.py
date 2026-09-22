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
    #: Коммит, от которого отпочковано дерево. Патч считается от него, а не от
    #: HEAD, поэтому он одинаков и до промежуточного коммита, и после него.
    #: Пустая строка у деревьев, найденных через `list_worktrees`: там базу взять
    #: неоткуда, и патч возвращается к отсчёту от HEAD.
    base: str = ""


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, check=False
    )


def _identity(cwd: str | Path) -> list[str]:
    """`-c user.*`, только когда личность коммитера в репозитории не настроена.

    Демон коммитит в чужих репозиториях: если у пользователя личность задана,
    коммит должен остаться за ним, а не за AgentDispatch.
    """
    if _git(["config", "user.email"], cwd).stdout.strip():
        return []
    return ["-c", "user.name=AgentDispatch", "-c", "user.email=agent-dispatch@localhost"]


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
    base = _git(["rev-parse", "HEAD"], directory)
    return Worktree(path=directory, branch=branch, repo=root, base=base.stdout.strip())


def commit(worktree: Worktree, message: str) -> str | None:
    """Закоммитить работу исполнителя на ветку worktree. SHA, либо None если нечего.

    Коммит делает результат долговечным: он переживает и неудачную интеграцию, и
    уборку каталога, а `git worktree remove` перестаёт зависеть от `--force`.

    Хуки не запускаются намеренно. Это служебный коммит на черновой ветке, а
    pre-commit репозитория обычно гоняет полный прогон тестов и падал бы на любой
    честно недоделанной задаче плана. Настоящий гейт — коммит вызывающего после
    того, как он прочитал диф.
    """
    add = _git(["add", "-A"], worktree.path)
    if add.returncode != 0:
        raise WorktreeError(add.stderr.strip() or "git add failed")
    if _git(["diff", "--cached", "--quiet"], worktree.path).returncode == 0:
        return None
    result = _git(
        [*_identity(worktree.path), "commit", "--no-verify", "-m", message], worktree.path
    )
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git commit failed")
    head = _git(["rev-parse", "HEAD"], worktree.path)
    if head.returncode != 0:
        raise WorktreeError(head.stderr.strip() or "git rev-parse failed")
    return head.stdout.strip()


def build_patch(worktree: Worktree) -> str:
    """Все изменения worktree одним патчем, включая новые файлы.

    Файлы добавляются в индекс worktree, поэтому диф видит и неотслеживаемые;
    индекс worktree свой и на исходную копию не влияет. Отсчёт идёт от базового
    коммита, а не от HEAD, поэтому патч одинаков до промежуточного коммита и
    после: `diff --cached HEAD` после коммита вернул бы пустоту.
    """
    add = _git(["add", "-A"], worktree.path)
    if add.returncode != 0:
        raise WorktreeError(add.stderr.strip() or "git add failed")
    diff = _git(["diff", "--binary", worktree.base or "HEAD"], worktree.path)
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


def delete_branch(repo: Path, branch: str) -> None:
    result = _git(["branch", "-D", branch], repo)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git branch -D failed")


def list_branches(cwd: str | Path, prefix: str) -> list[str]:
    """Ветки AgentDispatch, у которых уже нет worktree.

    Их оставляет режим `integrate: branch`: каталог убран, результат живёт
    коммитом на ветке. Без этого списка такие ветки копились бы незаметно.
    """
    root = repo_root(cwd)
    result = _git(["branch", "--list", f"{prefix}/*", "--format=%(refname:short)"], root)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git branch failed")
    attached = {tree.branch for tree in list_worktrees(root, prefix)}
    names = (line.strip() for line in result.stdout.splitlines())
    return [name for name in names if name and name not in attached]


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
