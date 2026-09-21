import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

FAKES_DIR = Path(__file__).parent / "fakes"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
    env = {
        **os.environ,
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
