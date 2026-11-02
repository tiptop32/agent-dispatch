"""Тесты миграции пользовательского конфига на omp (agent-dispatch migrate-config)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_dispatch.config import load_settings
from agent_dispatch.migrate import migrate_dict, migrate_file

OLD_CONFIG = """
# Пользовательский конфиг до миграции: codex/* через agent-lb и opencode/* на copilot.
routing:
  fallback_executor:
    - opencode/x5-code-large
    - codex/sol
    - codex/terra
    - codex/luna
  review_only:
    codex:
      - claude
executors:
  claude:
    enabled: false
  codex:
    enabled: false
  opencode/kimi:
    enabled: false
  opencode/x5-code-large:
    adapter: opencode
    model: copilot/x5-airun-code-large
    tier: strong
    corporate: true
    description: "Корпоративный основной"
  codex/sol:
    adapter: codex
    command: ~/.local/bin/codex-agent-lb
    model: gpt-5.6-sol
    tier: balanced
    limit_reset: recheck
    extra_args: ["--sandbox", "workspace-write"]
    description: "Основной рабочий"
  codex/terra:
    adapter: codex
    command: ~/.local/bin/codex-agent-lb
    model: gpt-6.1-terra
  codex/luna:
    adapter: codex
    command: ~/.local/bin/codex-agent-lb
    model: gpt-5.6-luna
    tier: fast
  codex/gpt-5.5:
    adapter: codex
    command: ~/.local/bin/codex-agent-lb
    model: gpt-5.5
    enabled: false
  claude/haiku:
    adapter: claude
    enabled: false
  claude/opus:
    adapter: claude
escalation:
  codex/luna:
    - codex/terra
    - codex/sol
    - claude/opus
  codex/sol:
    - claude/opus
  opencode/x5-code-large:
    - codex/sol
"""


@pytest.fixture
def old_config_dir(tmp_config_dir: Path) -> Path:
    (tmp_config_dir / "config.yaml").write_text(OLD_CONFIG)
    return tmp_config_dir


def test_migrate_dict_maps_all_codex_executors() -> None:
    migration = migrate_dict(yaml.safe_load(OLD_CONFIG))
    executors = migration.config["executors"]
    sol = executors["omp/sol"]
    assert sol["adapter"] == "omp"
    assert sol["command"] == "omp"
    assert sol["model"] == "agent-lb/gpt-5.6-sol"
    assert sol["limit_group"] == "agent-lb"
    assert sol["limit_reset"] == "recheck"
    assert sol["tier"] == "balanced"
    assert sol["description"] == "Основной рабочий"
    # Специфичный для codex CLI-флаг не переезжает в omp.
    assert "extra_args" not in sol
    assert executors["omp/terra"]["model"] == "agent-lb/gpt-6.1-terra"
    assert executors["omp/luna"]["model"] == "agent-lb/gpt-5.6-luna"
    assert executors["omp/luna"]["tier"] == "fast"
    gpt55 = executors["omp/gpt-5.5"]
    assert gpt55["model"] == "agent-lb/gpt-5.5"
    assert gpt55["enabled"] is False
    # claude-исполнители не трогаем.
    assert executors["claude/haiku"] == {"adapter": "claude", "enabled": False}


def test_migrate_dict_maps_opencode_copilot() -> None:
    migration = migrate_dict(yaml.safe_load(OLD_CONFIG))
    executors = migration.config["executors"]
    x5 = executors["omp/x5-code-large"]
    assert x5["adapter"] == "omp"
    assert x5["command"] == "omp"
    assert x5["model"] == "copilot-x5/x5-airun-code-large"
    assert x5["corporate"] is True
    assert x5["tier"] == "strong"
    assert x5["description"] == "Корпоративный основной"


def test_migrate_dict_rewrites_references() -> None:
    migration = migrate_dict(yaml.safe_load(OLD_CONFIG))
    config = migration.config
    assert config["routing"]["fallback_executor"] == [
        "omp/x5-code-large",
        "omp/sol",
        "omp/terra",
        "omp/luna",
    ]
    assert config["routing"]["review_only"] == {"omp": ["claude"]}
    assert config["escalation"]["omp/luna"] == ["omp/terra", "omp/sol", "claude/opus"]
    assert config["escalation"]["omp/sol"] == ["claude/opus"]
    assert config["escalation"]["omp/x5-code-large"] == ["omp/sol"]


def test_migrate_dict_disables_unknown_opencode_provider() -> None:
    migration = migrate_dict(yaml.safe_load(OLD_CONFIG))
    assert "opencode/kimi" not in migration.config["executors"]
    commented = [name for name, _fields, comment in migration.items if comment is not None]
    assert "opencode/kimi" in commented
    assert any("kimi" in note for note in migration.notes)


def test_migrate_dict_drops_bare_codex_when_slashed_present() -> None:
    migration = migrate_dict(yaml.safe_load(OLD_CONFIG))
    assert migration.config["executors"]["omp/sol"]["model"] == "agent-lb/gpt-5.6-sol"
    assert "codex" not in migration.config["executors"]


def test_migrate_dict_bare_codex_alone_becomes_omp_sol() -> None:
    migration = migrate_dict({"executors": {"codex": {"enabled": False, "tier": "balanced"}}})
    sol = migration.config["executors"]["omp/sol"]
    assert sol["adapter"] == "omp"
    assert sol["model"] == "agent-lb/gpt-5.6-sol"
    assert sol["enabled"] is False
    assert sol["tier"] == "balanced"


def test_migrate_dict_thinking_variant_gets_flags() -> None:
    config = {
        "executors": {
            "opencode/copilot-code-flash-thinking": {
                "adapter": "opencode",
                "model": "copilot/copilot-code-flash-thinking",
            }
        }
    }
    migration = migrate_dict(config)
    entry = migration.config["executors"]["omp/copilot-code-flash-thinking"]
    assert entry["model"] == "copilot-x5/copilot-code-flash"
    assert entry["extra_args"] == ["--thinking", "high"]


def test_migrate_dict_unknown_codex_name_without_model_is_disabled() -> None:
    config = {"executors": {"codex/preview": {"adapter": "codex"}}}
    migration = migrate_dict(config)
    assert "omp/preview" not in migration.config["executors"]
    assert any("codex/preview" in note for note in migration.notes)


def test_migrate_file_writes_backup_and_migrated_config(old_config_dir: Path) -> None:
    original = (old_config_dir / "config.yaml").read_text()
    code, report = migrate_file(old_config_dir)
    assert code == 0
    assert report.count(".bak-") == 1
    backups = list(old_config_dir.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    migrated = yaml.safe_load((old_config_dir / "config.yaml").read_text())
    assert migrated["executors"]["omp/sol"]["model"] == "agent-lb/gpt-5.6-sol"


def test_migrated_config_loads_via_load_settings(old_config_dir: Path) -> None:
    migrate_file(old_config_dir)
    settings = load_settings(old_config_dir)
    assert "omp/sol" in settings.executors
    assert settings.executors["omp/sol"].model == "agent-lb/gpt-5.6-sol"
    assert settings.executors["omp/x5-code-large"].corporate is True


def test_migrate_file_is_idempotent(old_config_dir: Path) -> None:
    migrate_file(old_config_dir)
    first = (old_config_dir / "config.yaml").read_bytes()
    code, report = migrate_file(old_config_dir)
    assert code == 0
    assert "уже мигрирован" in report
    assert (old_config_dir / "config.yaml").read_bytes() == first
    assert len(list(old_config_dir.glob("config.yaml.bak-*"))) == 1


def test_migrate_file_dry_run_writes_nothing(old_config_dir: Path) -> None:
    original = (old_config_dir / "config.yaml").read_text()
    code, report = migrate_file(old_config_dir, dry_run=True)
    assert code == 0
    diff_lines = report.splitlines()
    assert any(line.startswith("-") and "adapter: codex" in line for line in diff_lines)
    assert any(line.startswith("+") and "adapter: omp" in line for line in diff_lines)
    assert (old_config_dir / "config.yaml").read_text() == original
    assert not list(old_config_dir.glob("config.yaml.bak-*"))


def test_migrate_file_dry_run_on_migrated_config(old_config_dir: Path) -> None:
    migrate_file(old_config_dir)
    text = (old_config_dir / "config.yaml").read_text()
    code, report = migrate_file(old_config_dir, dry_run=True)
    assert code == 0
    assert "уже мигрирован" in report
    assert (old_config_dir / "config.yaml").read_text() == text


def test_migrate_file_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DISPATCH_CONFIG_DIR", raising=False)
    code, report = migrate_file(tmp_path)
    assert code == 1
    assert "не найден" in report


def test_migrate_file_reports_lost_comments(old_config_dir: Path) -> None:
    code, report = migrate_file(old_config_dir)
    assert code == 0
    assert "комментарии" in report


def test_migrate_file_rejects_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("- just\n- a list\n")
    code, report = migrate_file(tmp_path)
    assert code == 1
    assert "отображением" in report
