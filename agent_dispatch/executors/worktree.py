"""Изолированный git worktree на исполнителя.

Два исполнителя в одной рабочей копии затаптывают друг друга: при fan-out
сабагенты идут параллельно и правят одни файлы. Worktree даёт каждому свою
копию дерева и свою ветку, а результат возвращается в исходную рабочую копию
патчем. Worktree создаётся от HEAD, поэтому незакоммиченные изменения исходной
копии исполнителю не видны; об этом говорится в Task Package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        if not Path(cwd).is_dir():
            raise WorktreeError(f"directory does not exist: {cwd}") from exc
        raise WorktreeError(f"git failed in directory {cwd}: {exc}") from exc


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


def _main_repo_root(cwd: str | Path) -> Path:
    """Главная рабочая копия репозитория, даже если cwd — linked worktree."""
    root = repo_root(cwd)
    common = _git(["rev-parse", "--git-common-dir"], root)
    if common.returncode != 0:
        raise WorktreeError(common.stderr.strip() or "git rev-parse failed")
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    listed = _git(["--git-dir", str(common_dir), "worktree", "list", "--porcelain"], root)
    if listed.returncode != 0:
        raise WorktreeError(listed.stderr.strip() or "git worktree list failed")
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree "))
    raise WorktreeError("git worktree list returned no main repository")


def create(cwd: str | Path, directory: Path, branch: str) -> Worktree:
    """Создать worktree от HEAD исходной копии на новой ветке."""
    root = repo_root(cwd)
    main_root = _main_repo_root(root)
    directory.parent.mkdir(parents=True, exist_ok=True)
    result = _git(["worktree", "add", "-b", branch, str(directory), "HEAD"], root)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git worktree add failed")
    base = _git(["rev-parse", "HEAD"], directory)
    return Worktree(path=directory, branch=branch, repo=main_root, base=base.stdout.strip())


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
        # Индекс чист, но работа уже может лежать коммитом на ветке: демон
        # коммитит до проверки `verify`. Тогда результат это HEAD, а не «пусто».
        if worktree.base:
            head = _git(["rev-parse", "HEAD"], worktree.path)
            if head.returncode == 0 and head.stdout.strip() != worktree.base:
                return head.stdout.strip()
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


def discard_changes(worktree: Worktree) -> None:
    """Вернуть дерево к его HEAD: убрать правки и неотслеживаемые файлы.

    Только для собственного дерева демона после коммита работы: так артефакты
    проверки (кэши, отчёты покрытия) не попадают в патч. Игнорируемые файлы
    не трогаются, в патч они и так не идут.
    """
    for args in (["reset", "--hard", "-q", "HEAD"], ["clean", "-fdq"]):
        result = _git(args, worktree.path)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or f"git {args[0]} failed")


@dataclass
class _PathBackup:
    path: Path
    backup: Path
    existed: bool


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise WorktreeError(f"cannot snapshot unsupported path: {source}")


def _patch_paths(root: Path, patch_path: Path) -> list[Path]:
    result = _git(["apply", "--numstat", "-z", str(patch_path)], root)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "git apply numstat failed")
    fields = result.stdout.split("\0")
    names: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        _, _, name = fields[index].split("\t", 2)
        index += 1
        if name:
            names.append(name)
            continue
        if index + 1 >= len(fields):
            raise WorktreeError("git apply numstat returned an invalid rename")
        names.extend((fields[index], fields[index + 1]))
        index += 2

    paths: list[Path] = []
    for name in dict.fromkeys(names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorktreeError(f"patch contains an unsafe path: {name}")
        paths.append(root / relative)
    return paths


def _backup_path(path: Path, backup: Path) -> _PathBackup:
    existed = path.exists() or path.is_symlink()
    if existed:
        _copy_path(path, backup)
    return _PathBackup(path=path, backup=backup, existed=existed)


def _restore_paths(backups: list[_PathBackup], missing_parents: set[Path]) -> None:
    for item in backups:
        _remove_path(item.path)
        if item.existed:
            _copy_path(item.backup, item.path)
    for parent in sorted(missing_parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # The directory now contains a restored or pre-existing path.
            pass


def apply_patch(cwd: str | Path, patch_path: Path) -> None:
    """Атомарно применить патч, сохранив незакоммиченные правки исходной копии."""
    root = repo_root(cwd)
    touched = _patch_paths(root, patch_path)
    index_result = _git(["rev-parse", "--git-path", "index"], root)
    if index_result.returncode != 0:
        raise WorktreeError(index_result.stderr.strip() or "git rev-parse failed")
    index_path = Path(index_result.stdout.strip())
    if not index_path.is_absolute():
        index_path = root / index_path

    missing_parents: set[Path] = set()
    for path in touched:
        parent = path.parent
        while parent != root and not parent.exists():
            missing_parents.add(parent)
            parent = parent.parent

    with tempfile.TemporaryDirectory(prefix="agent-dispatch-apply-") as temp:
        backup_root = Path(temp)
        backups = [
            _backup_path(path, backup_root / f"path-{number}")
            for number, path in enumerate(touched)
        ]
        backups.append(_backup_path(index_path, backup_root / "index"))
        try:
            result = _git(["apply", "--3way", "--whitespace=nowarn", str(patch_path)], root)
        except WorktreeError:
            _restore_paths(backups, missing_parents)
            raise
        if result.returncode != 0:
            _restore_paths(backups, missing_parents)
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
