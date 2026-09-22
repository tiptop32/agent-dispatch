import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

FAKES_DIR = Path(__file__).parent / "fakes"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def warm_fake_cli() -> None:
    """Один раз exec-нуть каждый fake-скрипт.

    На macOS первый exec нового файла проходит проверку syspolicyd и может занять
    сотни миллисекунд; тесты run_cli с таймаутом 0.2-1.0 с из-за этого флакали
    при первом прогоне после изменения скриптов. Popen возвращается после exec,
    поэтому достаточно запустить и сразу убить.
    """
    for script in sorted(FAKES_DIR.glob("*.sh")):
        proc = subprocess.Popen(
            [str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


@pytest.fixture(autouse=True)
def clean_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убрать GIT_* из окружения тестов.

    Gate-набор запускается из pre-commit, где выставлены GIT_DIR и GIT_INDEX_FILE.
    Унаследованные, они уводят git-вызовы тестов в основную репозиторию.
    """
    for key in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированные config.yaml, env и data_dir. Ничего из ~/.config не читается."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv("AGENT_DISPATCH_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AGENT_DISPATCH_DATA_DIR", str(data_dir))
    # Секреты не должны утекать из окружения разработчика в тесты.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return config_dir


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Временная git-репа с одним коммитом и файлом a.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Внутри git-хука (pre-commit) выставлены GIT_INDEX_FILE, GIT_DIR и другие GIT_*,
    # они утекли бы во вложенную репу и сломали её. Убираем всё GIT_*.
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "HOME": str(tmp_path),  # не подхватывать глобальные хуки и подписи
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    yield repo
