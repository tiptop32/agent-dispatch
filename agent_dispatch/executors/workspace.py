import hashlib
import os
import stat
import subprocess
from pathlib import Path

#: Отметки снимка для путей, чьё состояние не сводится к содержимому файла.
DELETED = "<deleted>"
UNREADABLE = "<unreadable>"
DIRECTORY = "<directory>"

#: Размер порции при потоковом чтении файла.
_CHUNK = 1 << 20


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


def _hash_file(path: Path) -> str:
    """sha256 байтов файла, потоково: большой файл не грузится в память целиком."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _path_state(root: Path, name: str) -> str:
    """Отпечаток пути: режим и содержимое; для особых случаев — стабильная метка."""
    path = root / name
    try:
        if path.is_symlink():
            digest = hashlib.sha256()
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(path)))
            return digest.hexdigest()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            # Подмодуль или вложенная репа: снаружи содержимое не читается,
            # отпечатком служит сам факт каталога.
            return DIRECTORY
        digest = hashlib.sha256()
        digest.update(f"mode:{stat.S_IMODE(info.st_mode):o}\0".encode("ascii"))
        digest.update(bytes.fromhex(_hash_file(path)))
        return digest.hexdigest()
    except FileNotFoundError:
        return DELETED
    except OSError:
        return UNREADABLE


def snapshot(cwd: str | Path) -> dict[str, str]:
    """Return dirty paths from git status mapped to their state fingerprints.

    The fingerprint covers the file bytes plus the file mode, so re-editing or
    re-permissioning a file that was already dirty is visible in the snapshot,
    not only new paths. Symlinks hash their target, directories (submodules,
    nested repos) map to a stable marker, a deleted path to ``<deleted>`` and
    an unreadable one to ``<unreadable>`` instead of raising.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        check=True,
    )
    states: dict[str, str] = {}
    # Пути porcelain считаются от корня репозитория, а не от cwd: при cwd в
    # подкаталоге хеш от cwd давал бы `<deleted>` в обоих снимках.
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    )
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries) - 1:
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii")
        name = os.fsdecode(entry[3:])
        states[name] = _path_state(root, name)
        if "R" in status or "C" in status:
            index += 1
    return states


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
                digest.update(_path_state(root, os.fsdecode(name)).encode("ascii"))
    return digest.hexdigest()


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return paths whose state changed between the two snapshots, sorted.

    A path is reported when it is new in ``after``, when its fingerprint
    changed (a re-edit or a mode change of a file that was already dirty),
    or when it was dirty before and is clean now (the executor reverted a
    caller's edit, which is also a change made by the run).

    Attribution caveat: a fingerprint cannot tell who made the change, so an
    edit made concurrently by someone else in the same working copy shows up
    here too. Distinguishing executor edits from outsider edits is out of
    scope for this comparison.
    """
    changed = {path for path, state in after.items() if before.get(path) != state}
    changed |= before.keys() - after.keys()
    return sorted(changed)
