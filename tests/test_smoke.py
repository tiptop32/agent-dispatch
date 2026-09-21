import re

import agent_dispatch


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", agent_dispatch.__version__)


def test_git_repo_fixture_has_commit(git_repo) -> None:
    assert (git_repo / ".git").is_dir()
    assert (git_repo / "a.py").read_text() == "x = 1\n"
