import hashlib
import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
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


def _path_state(root: Path, name: str, *, metadata_only: bool = False) -> str:
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
            # Nested repositories are represented by the directory, not read recursively.
            return DIRECTORY
        if metadata_only:
            return f"stat:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
        digest = hashlib.sha256()
        digest.update(f"mode:{stat.S_IMODE(info.st_mode):o}\0".encode("ascii"))
        digest.update(bytes.fromhex(_hash_file(path)))
        return digest.hexdigest()
    except FileNotFoundError:
        return DELETED
    except OSError:
        return UNREADABLE


def bounded_git_output(
    cwd: str | Path, args: list[str], *, limit: int, timeout: float = 2
) -> tuple[bytes, bool]:
    """Read at most limit+1 Git bytes, with a deadline and unconditional child cleanup."""
    with (
        subprocess.Popen(
            ["git", "--no-pager", *args],
            cwd=cwd,
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process,
        selectors.DefaultSelector() as ready,
    ):
        data = bytearray()
        ready.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while len(data) <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise subprocess.TimeoutExpired(process.args, timeout)
                chunk = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                process.kill()
            process.stdout.close()
            code = process.wait(timeout=timeout)
            if code != 0 and len(data) <= limit:
                raise subprocess.CalledProcessError(code, process.args)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=timeout)
    return bytes(data[:limit]), len(data) > limit


def snapshot(cwd: str | Path, *, metadata_only: bool = False) -> dict[str, str]:
    """Return dirty paths from git status mapped to their state fingerprints.

    The fingerprint covers the file bytes plus the file mode, so re-editing or
    re-permissioning a file that was already dirty is visible in the snapshot,
    not only new paths. Symlinks hash their target, directories (submodules,
    nested repos) map to a stable marker, a deleted path to ``<deleted>`` and
    an unreadable one to ``<unreadable>`` instead of raising.
    With metadata_only, files use size/mode/mtime/ctime instead of reading bytes;
    semantic supervision uses this bounded-read mode, not content attribution.
    """
    if metadata_only:
        status, truncated = bounded_git_output(
            cwd, ["status", "--porcelain", "-z", "--untracked-files=all"], limit=262144
        )
        if truncated:
            raise ValueError("workspace status exceeds supervisor snapshot bound")
    else:
        status = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            check=True,
        ).stdout
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
            timeout=2 if metadata_only else None,
        ).stdout.strip()
    )
    entries = status.split(b"\0")
    index = 0
    while index < len(entries) - 1:
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii")
        name = os.fsdecode(entry[3:])
        states[name] = _path_state(root, name, metadata_only=metadata_only)
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
    for args in (
        ["diff", "HEAD", "--binary"],
        ["ls-files", "-o", "--exclude-standard", "--full-name", "-z"],
    ):
        result = subprocess.run(
            ["git", *args], cwd=root, env=git_env(), capture_output=True, check=True
        )
        digest.update(result.stdout)
        if args[0] == "ls-files":
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


class DiffStatSnapshot:
    """Measure task changes without writing caller files into its Git object store."""

    def __init__(self, cwd: str | Path, *, inherited_files: list[str] | None = None):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-dispatch-stat-")
        self.env = git_env()
        self.root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.cwd = Path(cwd).resolve()
        objects = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        index = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        temporary = Path(self.temp.name)
        (temporary / "objects").mkdir()
        self.env["GIT_OBJECT_DIRECTORY"] = str(temporary / "objects")
        self.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = objects
        self.env["GIT_INDEX_FILE"] = str(temporary / "index")
        if Path(index).is_file():
            shutil.copyfile(index, self.env["GIT_INDEX_FILE"])
        else:
            subprocess.run(
                ["git", "read-tree", "HEAD"],
                cwd=self.root,
                env=self.env,
                capture_output=True,
                check=True,
            )
        self.before = self._tree()
        self.tracked: set[str] = set()
        if inherited_files:
            for args in (["ls-files", "-z"], ["ls-tree", "-r", "--name-only", "-z", "HEAD"]):
                raw = subprocess.run(
                    ["git", *args], cwd=self.root, env=self.env, capture_output=True, check=True
                ).stdout
                self.tracked.update(os.fsdecode(name) for name in raw.split(b"\0") if name)
        if inherited_files:
            # Only successfully inherited paths use HEAD as their baseline.
            # Preserve the caller's dirty baseline everywhere else, and never
            # modify the real index or working tree.
            paths = [self._repo_path(name) for name in inherited_files]
            paths = [name for name in paths if name in self.tracked]
            if paths:
                subprocess.run(
                    [
                        "git",
                        "reset",
                        "-q",
                        "HEAD",
                        "--",
                        *[f":(top,literal){name}" for name in paths],
                    ],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    check=True,
                )
            self.before = subprocess.run(
                ["git", "write-tree"],
                cwd=self.root,
                env=self.env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

    def _repo_path(self, name: str) -> str:
        # Adapter snapshots are repo-relative; reported paths may be cwd-relative.
        if name in self.tracked or (Path(self.root) / name).exists():
            return name
        return os.path.relpath(self.cwd / name, self.root)

    def _tree(self) -> str:
        subprocess.run(
            ["git", "add", "-A", "--", "."],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            check=True,
        )
        return subprocess.run(
            ["git", "write-tree"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def diff_stat(self, files: list[str]) -> dict[str, int]:
        if not files:
            return {"files": 0, "added": 0, "removed": 0}
        after = self._tree()
        raw = subprocess.run(
            [
                "git",
                "diff",
                "--numstat",
                "-z",
                "--no-renames",
                self.before,
                after,
                "--",
                *[f":(top,literal){self._repo_path(name)}" for name in files],
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            check=True,
        ).stdout
        rows = [row.split(b"\t", 2) for row in raw.split(b"\0") if row]
        return {
            "files": len(rows),
            "added": sum(int(row[0]) for row in rows if row[0] != b"-"),
            "removed": sum(int(row[1]) for row in rows if row[1] != b"-"),
        }

    def close(self) -> None:
        self.temp.cleanup()
