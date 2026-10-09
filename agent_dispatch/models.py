from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextMode(StrEnum):
    prompt = "prompt"
    prompt_summary = "prompt+summary"
    full = "full"


class SourceAgent(StrEnum):
    claude = "claude"
    codex = "codex"
    opencode = "opencode"
    cli = "cli"
    unknown = "unknown"


class TaskStatus(StrEnum):
    queued = "queued"
    routing = "routing"
    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    needs_context = "needs_context"
    needs_escalation = "needs_escalation"
    cancelled = "cancelled"


TerminalStatus = Literal["completed", "partial", "failed", "needs_context", "needs_escalation"]

#: Статусы, после которых задача больше не меняется. Единственное определение:
#: демон, CLI и HTTP-слой должны видеть один и тот же набор.
FINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.completed,
        TaskStatus.partial,
        TaskStatus.failed,
        TaskStatus.needs_context,
        TaskStatus.needs_escalation,
        TaskStatus.cancelled,
    }
)


class RouterKind(StrEnum):
    jev = "jev"
    claude_local = "claude_local"
    fallback = "fallback"
    override = "override"


class GuardReason(StrEnum):
    low_confidence = "low_confidence"
    low_margin = "low_margin"
    disabled = "disabled"
    unavailable = "unavailable"
    router_unavailable = "router_unavailable"
    user_override = "user_override"
    single_candidate = "single_candidate"
    max_hops = "max_hops"
    bad_cwd = "bad_cwd"
    unknown_parent = "unknown_parent"
    max_children = "max_children"
    escalated = "escalated"
    review_only = "review_only"
    budget = "budget"


#: `task` меняет файлы, `review` только читает и возвращает замечания.
TaskKind = Literal["task", "review"]


class DispatchRequest(_Model):
    task: str = Field(min_length=1)
    cwd: str
    kind: TaskKind = "task"
    context: str | None = None
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    context_mode: ContextMode = ContextMode.prompt_summary
    source_agent: SourceAgent = SourceAgent.unknown
    executor: str | None = None
    allow_escalation: bool = True
    wait_seconds: int = Field(1800, ge=0)
    timeout_seconds: int | None = None
    parent_task_id: str | None = None
    root_agent: SourceAgent | None = None
    hop: int = Field(0, ge=0)
    workspace_mode: Literal["in_place", "worktree"] | None = None
    #: Shell-команды, которые демон сам запускает после исполнителя (обычно
    #: тесты). Задача считается сделанной, только если все вышли с кодом 0:
    #: самоотчёт исполнителя о тестах не проверка.
    verify: list[str] = Field(default_factory=list)
    #: Задача, которую продолжает этот запрос. Его `task` — новое сообщение
    #: исполнителю, остальное демон берёт из продолжаемой задачи.
    followup_of: str | None = None

    @field_validator("files")
    @classmethod
    def validate_files(cls, files: list[str]) -> list[str]:
        for file_name in files:
            path = Path(file_name)
            if path.is_absolute() or PureWindowsPath(file_name).is_absolute() or ".." in path.parts:
                raise ValueError("files must contain relative paths without '..'")
        return files


class FollowupRequest(_Model):
    """Продолжение готовой задачи тем же исполнителем, по возможности в его сессии."""

    message: str = Field(min_length=1)
    #: None — те же команды проверки, что у продолжаемой задачи.
    verify: list[str] | None = None
    allow_escalation: bool = True
    wait_seconds: int = Field(1800, ge=0)
    timeout_seconds: int | None = None
    #: Кто просит продолжения. Заданный `source_agent` заменяет источник, hop и
    #: родителя продолжаемой задачи: guards (`review_only`, глубина, квоты) судят
    #: того, кто вызывает сейчас, а вложенный вызов не встаёт в корневую очередь
    #: за своим же родителем. None — всё как у продолжаемой задачи (CLI, API).
    source_agent: SourceAgent | None = None
    parent_task_id: str | None = None
    root_agent: SourceAgent | None = None
    hop: int = Field(0, ge=0)


class Judgment(_Model):
    kind: Literal["choice", "score", "noul"]
    value: str | float | bool
    confidence: float
    probabilities: dict[str, float]


class GuardEvent(_Model):
    reason: GuardReason
    detail: str
    executor: str | None = None


ConfidenceTier = Literal["autonomous", "advisory", "fallback"]


class RouteDecision(_Model):
    executor: str
    confidence: float
    scores: dict[str, float]
    router: RouterKind
    reason: GuardReason | None = None
    #: Какой уровень работы запросил роутер: fast, balanced, strong.
    capability: str | None = None
    #: Насколько решению можно доверять: autonomous, advisory, fallback.
    confidence_tier: ConfidenceTier | None = None
    judgments: dict[str, Judgment] = Field(default_factory=dict)
    latency_ms: int = 0
    cost_usd: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Availability(_Model):
    available: bool
    version: str | None = None
    error: str | None = None
    checked_at: datetime


class TestsInfo(_Model):
    command: str | None = None
    result: Literal["passed", "failed", "not_run"] | None = None
    output_tail: str | None = None


class VerifyCommand(_Model):
    command: str
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = 0
    output_tail: str = ""

    @property
    def failure_reason(self) -> str:
        """Почему команда не прошла: для ошибки, промпта follow-up и ответа MCP."""
        return "timed out" if self.timed_out else f"exit {self.exit_code}"


class VerificationInfo(_Model):
    """Проверка, которую демон провёл сам, а не со слов исполнителя."""

    result: Literal["passed", "failed"]
    #: Выполненные команды; после первой упавшей остальные не запускаются.
    commands: list[VerifyCommand] = Field(default_factory=list)
    #: Изменённые файлы, похожие на тесты. Не приговор (задача могла требовать
    #: тестов), а указатель для вызывающего: проверку можно «пройти», ослабив её.
    tests_changed: list[str] = Field(default_factory=list)
    #: Исполнитель отчитался `tests: passed`, а проверка упала.
    contradicts_report: bool = False


class Usage(_Model):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ExecutionResult(_Model):
    status: TerminalStatus
    executor: str
    model: str | None
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    tests: TestsInfo | None = None
    verification: VerificationInfo | None = None
    confidence: float | None = None
    needs_escalation: bool = False
    error: str | None = None
    usage: Usage | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(_Model):
    task_id: str
    parent_task_id: str | None
    escalated_from: str | None
    root_agent: SourceAgent
    source_agent: SourceAgent
    hop: int
    request: DispatchRequest
    status: TaskStatus
    decision: RouteDecision | None
    result: ExecutionResult | None
    log_path: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskView(TaskRecord):
    log_tail: str
