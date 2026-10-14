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
import time
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


@dataclass(frozen=True)
class StoredWorktree:
    """Worktree из общего каталога AgentDispatch, найденный без исходного cwd."""

    path: Path
    branch: str
    repo: Path
    age_seconds: float
    repo_exists: bool


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


def git_location(cwd: str | Path) -> dict[str, str | None] | None:
    """Общий git dir (абсолютный) и ветка cwd; None, если cwd не git.

    Вызывающий часто работает в своём feature-worktree и удаляет его после
    слияния. Коммиты остаются в общем git dir, по нему `evals.replay` и судит,
    принят ли диф исполнителя.
    """
    common = common_dir(cwd)
    if common is None:
        return None
    try:
        # cwd могли удалить между двумя вызовами: ветка тогда просто неизвестна.
        branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd).stdout.strip()
    except WorktreeError:
        branch = ""
    return {"common_dir": common, "branch": branch or None}


def common_dir(cwd: str | Path) -> str | None:
    """Абсолютный общий git dir каталога; None, если он не в git или его нет."""
    try:
        proc = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    except WorktreeError:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def holds_cwd(tree_path: Path, cwds: list[str]) -> bool:
    """Лежит ли один из cwd в дереве (или совпадает с ним).

    Дерево с cwd незавершённой задачи нельзя убирать: ни демону после задачи,
    ни человеку через `worktrees --clean`.
    """
    root = tree_path.expanduser().resolve()
    for cwd in cwds:
        path = Path(cwd).expanduser().resolve()
        if path == root or root in path.parents:
            return True
    return False


def ensure_stored_path(base: str | Path, path: Path) -> None:
    """Проверка на границе удаления: путь это настоящий каталог прямо в base.

    Инвентаризация уже отсеяла симлинки, но между ней и удалением каталог
    могли подменить симлинком на чужой worktree.
    """
    if path.is_symlink() or not path.is_dir():
        raise WorktreeError(f"not a plain directory: {path}")
    if path.resolve().parent != Path(base).expanduser().resolve():
        raise WorktreeError(f"outside the worktree directory: {path}")


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


def directory_size(path: Path) -> int:
    """Считает размер файлов дерева, не переходя по симлинкам.

    Обход всех файлов дорог (гигабайты в node_modules и .venv), поэтому
    размер считает только тот, кому он нужен (`doctor`), а не инвентаризация.
    """
    total = 0
    for root, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat(follow_symlinks=False).st_size
            except OSError:
                # Дерево может меняться параллельно с doctor; пропавший файл не
                # должен обрывать инвентаризацию остальных каталогов.
                continue
    return total


def _stored_worktree(path: Path, now: float) -> StoredWorktree | None:
    marker = path / ".git"
    try:
        line = marker.read_text().strip()
        if not line.startswith("gitdir: "):
            return None
        git_dir = Path(line.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = (path / git_dir).resolve()
        common_file = git_dir / "commondir"
        if common_file.is_file():
            common_dir = (git_dir / common_file.read_text().strip()).resolve()
        else:
            # Обычный linked worktree лежит в <common>/.git/worktrees/<name>.
            common_dir = git_dir.parent.parent
        repo = common_dir.parent if common_dir.name == ".git" else common_dir
        repo_exists = common_dir.is_dir() and repo.is_dir()
        branch = "-"
        head = git_dir / "HEAD"
        if head.is_file():
            value = head.read_text().strip()
            branch = value.removeprefix("ref: refs/heads/") if value.startswith("ref: ") else value
        age_seconds = max(0.0, now - marker.stat().st_mtime)
    except OSError:
        return None
    return StoredWorktree(
        path=path,
        branch=branch,
        repo=repo,
        age_seconds=age_seconds,
        repo_exists=repo_exists,
    )


def list_stored_worktrees(base: str | Path, *, now: float | None = None) -> list[StoredWorktree]:
    """Находит worktree всех репозиториев в каталоге данных AgentDispatch."""
    root = Path(base).expanduser()
    try:
        children = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise WorktreeError(f"cannot read worktree directory {root}: {exc}") from exc
    moment = time.time() if now is None else now
    resolved_root = root.resolve()
    # Только настоящие каталоги прямо в base: симлинк с именем задачи мог бы
    # увести `--clean` в чужой worktree вне каталога данных.
    return [
        tree
        for path in children
        if not path.is_symlink() and path.is_dir() and path.resolve().parent == resolved_root
        if (tree := _stored_worktree(path, moment))
    ]


def remove_orphaned_directory(tree: StoredWorktree) -> None:
    """Удаляет каталог дерева, git-репозиторий которого уже исчез."""
    if tree.repo_exists:
        raise WorktreeError(f"repository still exists: {tree.repo}")
    try:
        shutil.rmtree(tree.path)
    except OSError as exc:
        raise WorktreeError(f"cannot remove {tree.path}: {exc}") from exc
