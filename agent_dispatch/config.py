from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class ConfigError(ValueError):
    """Raised when the AgentDispatch configuration is invalid."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(_ConfigModel):
    host: str = "127.0.0.1"
    port: int = 7433
    max_concurrent_tasks: int = 2
    data_dir: Path = Path("~/.local/share/agent-dispatch")

    @model_validator(mode="after")
    def normalize_data_dir(self) -> ServerSettings:
        self.data_dir = self.data_dir.expanduser().resolve()
        return self


class McpSettings(_ConfigModel):
    wait_seconds: int = Field(50, ge=0)


class JevSettings(_ConfigModel):
    base_url: str = "https://openrouter.ai/api/alpha/decisions"
    model: str = "typesafe/jev-1.13"
    api_key_env: str = "OPENROUTER_API_KEY"
    retries: int = Field(2, ge=0)
    timeout_seconds: float = Field(15, gt=0)


class ClaudeLocalSettings(_ConfigModel):
    command: str = "claude"
    model: str | None = None


class RouterSettings(_ConfigModel):
    backend: Literal["jev", "claude_local"] = "jev"
    jev: JevSettings = Field(default_factory=JevSettings)
    claude_local: ClaudeLocalSettings = Field(default_factory=ClaudeLocalSettings)


class RoutingSettings(_ConfigModel):
    min_confidence: float = 0.60
    autonomous_confidence: float = 0.85
    min_margin: float = 0.10
    fallback_executor: str = "codex"
    max_hops: int = Field(2, ge=0)
    max_children: int = Field(2, ge=0)
    exclude_source_agent: bool = True
    default_timeout_seconds: int = Field(1800, ge=0)
    availability_ttl_seconds: int = Field(60, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RoutingSettings:
        if self.autonomous_confidence < self.min_confidence:
            raise ValueError("autonomous_confidence must be >= min_confidence")
        return self


class ExecutionSettings(_ConfigModel):
    """Где исполнитель правит файлы: прямо в рабочей копии или в git worktree."""

    workspace_mode: Literal["in_place", "worktree"] = "in_place"
    worktree_dir: Path | None = None
    branch_prefix: str = "agent-dispatch"
    integrate: Literal["apply", "manual"] = "apply"
    keep_worktrees: bool = False


class ExecutorSettings(_ConfigModel):
    adapter: Literal["claude", "codex", "opencode"]
    command: str | None = None
    model: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str = ""
    #: Уровень работы, который executor закрывает. По нему его находит решение Jev.
    tier: Literal["fast", "balanced", "strong"] = "balanced"
    #: Исполнитель внутри корпоративного периметра: данные не уходят наружу.
    corporate: bool = False

    @model_validator(mode="after")
    def validate_adapter(self) -> ExecutorSettings:
        if self.adapter == "opencode" and not self.model:
            raise ValueError("model is required for adapter opencode")
        return self

    @property
    def resolved_command(self) -> str:
        return self.command or self.adapter


class Settings(_ConfigModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    executors: dict[str, ExecutorSettings] = Field(default_factory=dict)
    escalation: dict[str, list[str]] = Field(default_factory=dict)
    secrets: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> Settings:
        if self.routing.fallback_executor not in self.executors:
            raise ValueError(
                f"routing.fallback_executor: unknown executor {self.routing.fallback_executor!r}"
            )
        names = set(self.executors)
        for source, targets in self.escalation.items():
            if source not in names:
                raise ValueError(f"escalation.{source}: unknown executor")
            for index, target in enumerate(targets):
                if target not in names:
                    raise ValueError(f"escalation.{source}[{index}]: unknown executor {target!r}")
        return self

    def enabled_executors(self) -> dict[str, ExecutorSettings]:
        return {name: executor for name, executor in self.executors.items() if executor.enabled}

    def secret(self, name: str) -> str | None:
        value = self.secrets.get(name)
        if value is not None:
            return value.get_secret_value()
        return os.environ.get(name)

    def secret_names(self) -> set[str]:
        return set(self.secrets) | {self.router.jev.api_key_env}


def load_env_file(path: Path) -> dict[str, SecretStr]:
    secrets: dict[str, SecretStr] = {}
    if not path.is_file():
        return secrets
    for raw_line in path.read_text().splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        line = raw_line
        if not line:
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        secrets[key] = SecretStr(value)
    return secrets


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _config_path(config_dir: Path | None) -> Path:
    if config_dir is not None:
        return config_dir.expanduser()
    env_dir = os.environ.get("AGENT_DISPATCH_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path("~/.config/agent-dispatch").expanduser()


def _error_path(error: dict[str, Any]) -> str:
    location = error.get("loc", ())
    if isinstance(location, str):
        return location
    return ".".join(str(part) for part in location) or "config"


def load_settings(config_dir: Path | None = None) -> Settings:
    directory = _config_path(config_dir)
    default_path = Path(__file__).with_name("config_default.yaml")
    defaults = yaml.safe_load(default_path.read_text()) or {}
    user_path = directory / "config.yaml"
    if user_path.is_file():
        try:
            user = yaml.safe_load(user_path.read_text())
        except yaml.YAMLError as error:
            raise ConfigError(f"{user_path}: invalid YAML: {error}") from error
        if not isinstance(user, dict):
            raise ConfigError(f"{user_path}: top-level must be a mapping")
    else:
        user = {}
    merged = _deep_merge(defaults, user or {})
    if "secrets" in merged:
        raise ConfigError("secrets: keys belong in the env file, not config.yaml")
    data_dir = os.environ.get("AGENT_DISPATCH_DATA_DIR")
    if data_dir is not None:
        merged.setdefault("server", {})["data_dir"] = data_dir
    merged["secrets"] = load_env_file(directory / "env")
    try:
        return Settings.model_validate(merged)
    except ValidationError as error:
        first = error.errors()[0]
        path = _error_path(first)
        if "model is required for adapter opencode" in first["msg"]:
            path = f"{path}.model"
        raise ConfigError(f"{path}: {first['msg']}") from error
    except ValueError as error:
        raise ConfigError(str(error)) from error
