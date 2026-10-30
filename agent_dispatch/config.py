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
    #: Общий потолок корневых задач на правку (hop 0, kind: task) поверх
    #: отдельных пулов `max_concurrent_per_group`. При дефолтах одновременно
    #: работают не больше четырёх задач и одного ревью, то есть пяти корневых
    #: CLI-процессов. В общем случае максимум равен сумме этого предела и
    #: `max_concurrent_reviews`.
    max_concurrent_tasks: int = Field(4, ge=1)
    #: Ёмкость каждой `ExecutorSettings.resolved_limit_group`. Задача сначала
    #: занимает этот слот, потом общий: ожидание занятой группы не блокирует
    #: свободную группу общим слотом.
    max_concurrent_per_group: int = Field(2, ge=1)
    #: Собственная ёмкость корневых ревью (hop 0, kind: review). Лишние ревью
    #: ждут своей очереди, задачи их не блокируют. Вложенная работа (hop > 0)
    #: идёт мимо обоих пулов, как раньше.
    max_concurrent_reviews: int = Field(1, ge=1)
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
    #: Доля пригодных решений Jev, в которых запускается статический balanced-
    #: исполнитель. Разбиение детерминировано по task_id и переживает рестарты.
    explore_share: float = Field(0.0, ge=0.0, le=1.0)
    #: После ожидания занятой группы можно взять свободный резерв того же тира.
    #: 0 отключает переключение; периметр и guards сохраняются.
    preferred_executor_max_wait_seconds: float = Field(60.0, ge=0, allow_inf_nan=False, strict=True)
    #: Куда уходит задача без уверенного решения роутера. Строка или упорядоченный
    #: список: берётся первый доступный кандидат, остальные идут резервом.
    fallback_executor: str | list[str] = "codex"
    max_hops: int = Field(2, ge=0)
    max_children: int = Field(2, ge=0)
    #: Порог уверенности ответа Jev о корпоративных данных. Ответ ниже порога не
    #: сужает пул до периметра. `noul`-уверенность это |p - 0.5| * 2, поэтому 0.06
    #: значит «модель не знает». 0 — доверять любому ответу, как было раньше.
    #: Поднимая порог, вы разрешаете отправлять наружу задачи, которые Jev счёл
    #: корпоративными неуверенно: это ослабление периметра данных, решение ваше.
    corporate_min_confidence: float = Field(0.0, ge=0.0, le=1.0)
    #: Когда false, вопрос о корпоративных данных Jev не задаётся и пул не
    #: сужается до исполнителей с `corporate: true`: корпоративный код может
    #: уехать за периметр. Отключать периметр вправе только человек, поэтому
    #: по умолчанию true и поведение прежнее.
    corporate_perimeter: bool = True
    exclude_source_agent: bool = True
    default_timeout_seconds: int = Field(1800, ge=0)
    availability_ttl_seconds: int = Field(60, ge=0)
    #: На сколько секунд исполнитель выходит из ротации после сбоя, который
    #: повторится на любой задаче: лимит расходов, перегруженная модель, отказ
    #: авторизации, обрыв сети. 0 выключает остывание.
    failure_cooldown_seconds: int = Field(900, ge=0)
    #: Остывание после исчерпанного лимита, если CLI не сказал, когда лимит
    #: сбросится. Когда сказал («resets 5am», «Try again at 5:02 PM»),
    #: исполнитель остывает ровно до сброса. 0 в `failure_cooldown_seconds`
    #: выключает и это.
    quota_cooldown_seconds: int = Field(3600, ge=1)
    #: Источник -> адаптеры, которым он вправе отдать только ревью (`kind: review`),
    #: но не задачу на правку. По умолчанию Codex не тратит токены Claude на
    #: работу, которую делает сам, а просит у него только ревью.
    review_only: dict[str, list[str]] = Field(default_factory=lambda: {"codex": ["claude"]})
    #: Потолок расходов цепочки эскалации вместе с подзадачами её звеньев, в
    #: долларах. Дошла до него — следующее звено не запускается. Считаются только
    #: расходы, о которых сообщил CLI (claude, opencode; codex цену не сообщает).
    #: null выключает.
    max_chain_cost_usd: float | None = Field(None, gt=0)
    #: Потолок расходов за локальные сутки: исполнители и решения роутера. Дошли
    #: до него — новые задачи, подзадачи и звенья эскалации получают отказ
    #: `budget` до полуночи. null выключает.
    daily_cost_limit_usd: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RoutingSettings:
        if self.autonomous_confidence < self.min_confidence:
            raise ValueError("autonomous_confidence must be >= min_confidence")
        return self

    @model_validator(mode="after")
    def validate_review_only(self) -> RoutingSettings:
        sources = {"claude", "codex", "opencode", "cli", "unknown"}
        adapters = {"claude", "codex", "opencode"}
        for source, targets in self.review_only.items():
            if source not in sources:
                raise ValueError(f"review_only.{source}: unknown source agent")
            for target in targets:
                if target not in adapters:
                    raise ValueError(f"review_only.{source}: unknown adapter {target!r}")
        return self

    @property
    def fallback_chain(self) -> list[str]:
        """`fallback_executor` в виде списка в порядке предпочтения."""
        if isinstance(self.fallback_executor, str):
            return [self.fallback_executor]
        return list(self.fallback_executor)


class ExecutionSettings(_ConfigModel):
    """Где исполнитель правит файлы: прямо в рабочей копии или в git worktree."""

    workspace_mode: Literal["in_place", "worktree"] = "in_place"
    worktree_dir: Path | None = None
    branch_prefix: str = "agent-dispatch"
    #: apply — вернуть результат патчем в рабочую копию; branch — оставить его
    #: коммитом на ветке задачи, рабочую копию не трогать; manual — оставить и
    #: ветку, и worktree, и файл патча, решает человек.
    integrate: Literal["apply", "manual", "branch"] = "apply"
    keep_worktrees: bool = False
    #: Убить executor, если он столько секунд ничего не пишет в stdout/stderr;
    #: 0 отключает. Жёсткий routing.default_timeout_seconds всё равно действует.
    idle_timeout_seconds: int = Field(900, ge=0)
    #: Предел на каждую команду `verify` из запроса. Зависшая проверка
    #: считается упавшей.
    verify_timeout_seconds: int = Field(600, ge=1)
    #: Запуск правки кончился без блока отчёта, но с изменёнными файлами: демон
    #: продолжает ту же сессию коротким «дай отчёт» с этим пределом; 0 отключает.
    report_recovery_timeout_seconds: int = Field(300, ge=0)


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
    #: Исполнители с общим лимитом: исчерпанный лимит одного выводит из ротации
    #: всю группу. По умолчанию у claude и codex это адаптер (один аккаунт на
    #: все модели), у opencode провайдер из `model`.
    limit_group: str | None = None
    #: `announced` держит исполнителя до сброса, названного в сообщении CLI.
    #: `recheck` для пула аккаунтов за балансировщиком (codex-lb): названный
    #: сброс относится к одному аккаунту, следующий запрос уйдёт на другой,
    #: поэтому исполнитель перепроверяется через `routing.quota_cooldown_seconds`.
    limit_reset: Literal["announced", "recheck"] = "announced"
    #: Жёсткий таймаут задач этого исполнителя, секунды. Запрос с
    #: `timeout_seconds` сильнее, без обоих действует `routing.default_timeout_seconds`.
    timeout_seconds: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def validate_adapter(self) -> ExecutorSettings:
        if self.adapter == "opencode" and not self.model:
            raise ValueError("model is required for adapter opencode")
        return self

    @property
    def resolved_command(self) -> str:
        return self.command or self.adapter

    def resolved_limit_group(self, name: str) -> str:
        if self.limit_group:
            return self.limit_group
        if self.adapter == "opencode":
            provider = (self.model or "").split("/", 1)
            return f"opencode:{provider[0]}" if len(provider) == 2 else name
        return self.adapter


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
        chain = self.routing.fallback_chain
        if not chain:
            raise ValueError("routing.fallback_executor: empty list")
        for name in chain:
            if name not in self.executors:
                raise ValueError(f"routing.fallback_executor: unknown executor {name!r}")
        names = set(self.executors)
        for source, targets in self.escalation.items():
            if source not in names:
                raise ValueError(f"escalation.{source}: unknown executor")
            for index, target in enumerate(targets):
                if target not in names:
                    raise ValueError(f"escalation.{source}[{index}]: unknown executor {target!r}")
        return self

    def timeout_for(self, executor: str, requested: int | None = None) -> int:
        """Жёсткий таймаут запуска: запрос, затем исполнитель, затем `routing`."""
        item = self.executors.get(executor)
        return (
            requested
            or (item.timeout_seconds if item else None)
            or self.routing.default_timeout_seconds
        )

    def limit_group(self, executor: str) -> str:
        """Общая квота исполнителя; неизвестное имя — сама себе группа."""
        item = self.executors.get(executor)
        return item.resolved_limit_group(executor) if item else executor

    @property
    def worktree_base(self) -> Path:
        """Каталог деревьев исполнителей: одно место для демона, CLI и doctor."""
        return Path(
            self.execution.worktree_dir or (self.server.data_dir / "worktrees")
        ).expanduser()

    @property
    def db_path(self) -> Path:
        return self.server.data_dir / "dispatch.db"

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
        # Отступ слева срезается до разбора: иначе у «  export FOO=bar» ключом
        # становится «export FOO». Справа не трогаем, пробелы в конце значения
        # значимы.
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
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
