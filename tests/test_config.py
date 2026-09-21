import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_dispatch.config import ConfigError, Settings, load_env_file, load_settings

FIXTURES = Path(__file__).parent / "fixtures" / "config"


def copy_fixture(config_dir: Path, name: str) -> None:
    (config_dir / "config.yaml").write_text((FIXTURES / name).read_text())


def test_defaults_without_user_files_are_working(tmp_config_dir: Path) -> None:
    settings = load_settings()

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 7433
    assert settings.routing.fallback_executor == "codex"
    assert set(settings.enabled_executors()) == {
        "claude",
        "codex",
        "opencode/kimi",
        "opencode/x5-code",
    }


def test_full_file_parses_into_settings(tmp_config_dir: Path) -> None:
    copy_fixture(tmp_config_dir, "full.yaml")

    settings = load_settings()

    assert isinstance(settings, Settings)
    assert settings.server.host == "0.0.0.0"
    assert settings.router.backend == "claude_local"
    assert settings.executors["local"].resolved_command == "codex-custom"


def test_fallback_executor_must_exist(tmp_config_dir: Path) -> None:
    copy_fixture(tmp_config_dir, "bad_fallback.yaml")

    with pytest.raises(ConfigError, match=r"routing\.fallback_executor"):
        load_settings()


def test_opencode_executor_requires_model(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "config.yaml").write_text(
        """executors:
  custom:
    adapter: opencode
    description: Missing model
"""
    )

    with pytest.raises(ConfigError, match=r"executors\.custom\.model"):
        load_settings()


def test_escalation_executor_names_must_exist(tmp_config_dir: Path) -> None:
    copy_fixture(tmp_config_dir, "bad_escalation.yaml")

    with pytest.raises(ConfigError, match=r"escalation\.codex"):
        load_settings()


def test_command_defaults_to_adapter(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "config.yaml").write_text(
        """executors:
  local:
    adapter: codex
    description: Local
"""
    )

    assert load_settings().executors["local"].resolved_command == "codex"


def test_default_extra_args_come_from_packaged_config(tmp_config_dir: Path) -> None:
    settings = load_settings()

    assert settings.executors["codex"].extra_args == ["--sandbox", "workspace-write"]


def test_executors_merge_by_name(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "config.yaml").write_text(
        """executors:
  claude:
    enabled: false
  custom:
    adapter: codex
    description: Custom executor
"""
    )

    settings = load_settings()

    assert settings.executors["claude"].enabled is False
    assert settings.executors["claude"].extra_args[0:2] == [
        "--permission-mode",
        "acceptEdits",
    ]
    assert settings.executors["custom"].adapter == "codex"
    assert "claude" not in settings.enabled_executors()
    assert "custom" in settings.enabled_executors()


def test_env_file_parses_assignments_comments_and_equals(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("# comment\n\nONE=first\nTOKEN=left=right\n")

    secrets = load_env_file(env_file)

    assert {name: value.get_secret_value() for name, value in secrets.items()} == {
        "ONE": "first",
        "TOKEN": "left=right",
    }


def test_env_file_skips_indented_comments_with_assignments(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("# KEY=VALUE\n  # INDENTED=1\nREAL=1\n")

    secrets = load_env_file(env_file)

    assert {name: value.get_secret_value() for name, value in secrets.items()} == {
        "REAL": "1",
    }


def test_env_file_accepts_export_quotes_and_preserves_value_spaces(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text('export QUOTED="secret value"\nSPACED=  value  \n')

    secrets = load_env_file(env_file)

    assert secrets["QUOTED"].get_secret_value() == "secret value"
    assert secrets["SPACED"].get_secret_value() == "  value  "


def test_loading_env_file_does_not_modify_process_environment(
    tmp_config_dir: Path,
) -> None:
    (tmp_config_dir / "env").write_text("PRIVATE_TEST_KEY=hidden\n")

    settings = load_settings()

    assert "PRIVATE_TEST_KEY" not in os.environ
    assert settings.secret("PRIVATE_TEST_KEY") == "hidden"


def test_secrets_are_secret_str_and_redacted(tmp_config_dir: Path) -> None:
    secret_value = "never-print-this-value"  # pragma: allowlist secret
    (tmp_config_dir / "env").write_text(f"OPENROUTER_API_KEY={secret_value}\n")

    settings = load_settings()

    assert isinstance(settings.secrets["OPENROUTER_API_KEY"], SecretStr)
    assert secret_value not in repr(settings)
    assert secret_value not in settings.model_dump_json()


def test_data_dir_expands_user_home(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_DISPATCH_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_config_dir / "config.yaml").write_text("server:\n  data_dir: ~/dispatch-data\n")

    settings = load_settings()

    assert settings.server.data_dir == (tmp_path / "dispatch-data").resolve()
    assert settings.server.data_dir.is_absolute()


def test_data_dir_environment_variable_wins_over_yaml(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_data_dir = tmp_path / "from-env"
    monkeypatch.setenv("AGENT_DISPATCH_DATA_DIR", str(env_data_dir))
    (tmp_config_dir / "config.yaml").write_text("server:\n  data_dir: /from-yaml\n")

    assert load_settings().server.data_dir == env_data_dir.resolve()


def test_config_dir_environment_variable_selects_files(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected = tmp_path / "selected-config"
    selected.mkdir()
    (selected / "config.yaml").write_text("server:\n  port: 8123\n")
    monkeypatch.setenv("AGENT_DISPATCH_CONFIG_DIR", str(selected))

    assert load_settings().server.port == 8123


def test_explicit_config_dir_wins_over_environment(tmp_config_dir: Path, tmp_path: Path) -> None:
    selected = tmp_path / "explicit-config"
    selected.mkdir()
    (selected / "config.yaml").write_text("server:\n  port: 8124\n")

    assert load_settings(selected).server.port == 8124


def test_secret_falls_back_to_process_environment(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI_ONLY_KEY", "from-ci")

    assert load_settings().secret("CI_ONLY_KEY") == "from-ci"


def test_secret_names_include_loaded_and_router_names(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "env").write_text("SECONDARY_KEY=value\n")

    assert load_settings().secret_names() == {"OPENROUTER_API_KEY", "SECONDARY_KEY"}


def test_unknown_field_reports_its_path(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "config.yaml").write_text("server:\n  unknown_option: true\n")

    with pytest.raises(ConfigError, match=r"server\.unknown_option"):
        load_settings()


def test_invalid_user_yaml_raises_config_error_with_path(tmp_config_dir: Path) -> None:
    user_path = tmp_config_dir / "config.yaml"
    user_path.write_text("server: [unclosed\n")

    with pytest.raises(ConfigError) as error:
        load_settings()

    assert str(error.value).startswith(f"{user_path}: invalid YAML:")


def test_user_yaml_top_level_must_be_mapping(tmp_config_dir: Path) -> None:
    user_path = tmp_config_dir / "config.yaml"
    user_path.write_text("- one\n- two\n")

    with pytest.raises(ConfigError, match=r"top-level must be a mapping"):
        load_settings()


def test_secrets_in_user_yaml_raise_config_error(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "config.yaml").write_text("secrets:\n  TOKEN: from-yaml\n")

    with pytest.raises(
        ConfigError,
        match=r"secrets: keys belong in the env file, not config\.yaml",
    ):
        load_settings()


def test_max_children_defaults_to_two(tmp_config_dir: Path) -> None:
    assert load_settings(tmp_config_dir).routing.max_children == 2
