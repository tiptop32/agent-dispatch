# AgentDispatch: маршрутизация coding-задач между Claude Code, Codex и OpenCode

## Overview

AgentDispatch это отдельный локальный сервис, который принимает coding-задачу от любого агента (Claude Code, Codex, OpenCode), выбирает исполнителя через Jev (judgment-модель TypeSafe) и запускает выбранный CLI-агент в рабочей копии пользователя. Результат возвращается исходному агенту в унифицированном структурированном виде.

Проблема: сейчас выбор «кому отдать задачу» делается вручную или вообще не делается. Агенты не умеют делегировать друг другу, а когда делегируют, передают весь чат и теряют структуру результата.

Что даёт:
- один MCP tool `dispatch` для всех трёх агентов, «Start anywhere. Dispatch anywhere.»
- решение о маршрутизации принимает Jev за ~1 с и $0.00003, код только проверяет hard constraints
- компактный Task Package вместо истории чата, структурированный ExecutionResult вместо свободного текста
- телеметрия каждого решения, чтобы потом измерить routing accuracy и настраивать политику по данным

Интеграция: три агента подключают один stdio MCP-сервер `agent-dispatch mcp`, он проксирует в демон `agent-dispatch serve` по HTTP на 127.0.0.1. CLI `agent-dispatch` ходит в тот же HTTP API.

## Context (from discovery)

Репа `~/my_git_reps/agent-dispatch` пустая (один коммит, README из строки). Всё создаётся с нуля. Уже лежат фикстуры живой пробы Jev в `tests/fixtures/jev/`.

Проверенные факты окружения (2026-09-21):
- CLI: `claude` 2.1.270, `codex-cli` 0.155.1 (через codex-lb, `~/.codex/config.toml` `model_provider = "codex-lb"`), `opencode` 1.18.31, `uv`, `python3` 3.11.
- OpenCode сконфигурирован с провайдерами `openrouter` (`kimi-k2.5`, `minimax-m2.5`) и `copilot` (`x5-airun-code-large-exp` и другие `x5-airun-*`). DeepSeek и GLM НЕ настроены, в registry v0.1 их нет.
- Jev через OpenRouter: `POST https://openrouter.ai/api/alpha/decisions`, модель `typesafe/jev-1.13`. Живая проба: 200 OK, ~1 с, 695 input tokens, cost $0.000029. Ключ `OPENROUTER_API_KEY`.
- Vercel AI Gateway как второй провайдер Jev рассмотрен и отклонён 2026-09-21: требует карту на команде (`403 customer_verification_required`), free tier с жёстким rate limit. Не используем.
- Ключ в `~/.config/agent-dispatch/env` (права 600).
- Формат Jev. Запрос: `{model, state: str|object, questions: {name: {type: choice|score|noul, instructions, criteria}}}`. Для `choice` `criteria` это map `label -> description`; для `score` `criteria` это упорядоченный массив `[{label, description}]` (объект даёт 400 с zod-путём). Тип yes/no называется `noul`. Ответ: `{model, answers: {name: {type: "choice", choice, probabilities: {label: p}, confidence} | {type: "score", score, legend, probabilities, confidence}}, usage: {input_tokens, output_tokens, cost}, id, provider}`. Jev не виден в `GET /api/v1/models`.
- Headless-режимы (флаги проверены по `--help`): `claude -p --output-format json --permission-mode <mode> --allowedTools <list> --add-dir <cwd>`; `codex exec --json -C <cwd> --sandbox workspace-write --output-schema <schema.json> -o <last_message.json>`, вне git-репы требует `--skip-git-repo-check`; `opencode run --format json --dir <cwd> --model <provider/model>`.
- Что НЕ проверено и проверяется в Task 21: (а) выполняет ли `claude -p` Bash без TTY при `--allowedTools`; (б) выполняет ли `opencode run` команды без интерактивного подтверждения; (в) точная форма JSON-вывода `claude -p --output-format json` (по данным ревью там `modelUsage`, а не `model`).
- Версии на PyPI на день плана: mcp 2.2.0, fastapi 0.141.1, pydantic 2.13.5, typer 0.27.2, httpx 0.28.1.

Зависимости: pydantic, fastapi, uvicorn, mcp, httpx, pyyaml, jinja2, typer, jsonschema; dev: pytest, pytest-asyncio, respx, ruff, detect-secrets.

## Development Approach

- **testing approach**: TDD (тесты первыми). Для guards, парсера результата, jev-клиента, Task Package контракт зафиксирован, тест пишется до кода. Для адаптеров и демона сначала fake-CLI и тест, потом код.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - unit tests for new and modified functions, success and error scenarios
- **CRITICAL: all tests must pass before starting next task** - no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- gate-тесты не ходят в сеть и не запускают настоящие CLI; всё внешнее подменяется фикстурами и fake-CLI
- никаких секретов в репе: `.env`, `env`, `*.db`, `logs/`, `evals/reports/` в `.gitignore` с первого коммита, `detect-secrets` в pre-commit
- все таймауты и задержки в тестах инжектируются параметрами (0.05-0.2 с), никаких жёстко зашитых 5 с и 10 с в тестовых путях

## Testing Strategy

Два lane, разный бюджет:

- **Gate** (`uv run pytest tests/`): детерминированные, без сети, бюджет < 5 с суммарно (subprocess-спавны fake-CLI). Запускаются pre-commit хуком. Покрывают config, guards, Task Package (снапшот), парсер result-блока, jev-клиент на записанных фикстурах, адаптеры на fake-CLI, dispatcher end-to-end через HTTP с fake-адаптерами, MCP-прокси через in-memory клиент.
- **Evals** (`uv run python -m evals.routing`, `uv run python -m evals.smoke`): платные и медленные, запускаются перед релизом и вручную. `evals/routing/cases.jsonl` 20 размеченных задач с ожидаемым executor, метрика routing accuracy, порог 0.8. `evals/smoke/` 3 задачи на живых CLI в temp git-репе через живой демон: статус `completed`, `changed_files` непустой, тесты зелёные.
- e2e UI-тестов нет, UI отсутствует.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope

## Solution Overview

```
agent (claude / codex / opencode)
  └─ stdio MCP: `agent-dispatch mcp`        тонкий прокси, без логики, логи только в stderr
        │ HTTP 127.0.0.1:7433 + Bearer token из serve.json
        ▼
`agent-dispatch serve`                       демон, один на машину
  ├─ api/            FastAPI: /health, /executors, /route, /tasks, /tasks/{id}, /tasks/{id}/feedback, /export
  ├─ dispatch/       dispatcher (state machine задачи), task_package, escalation (v0.3)
  ├─ routing/        Router-интерфейс, jev.py, claude_local.py, guards.py, decision.py
  ├─ executors/      base.py, process.py, workspace.py, result_parser.py, claude.py, codex.py, opencode.py, registry.py
  ├─ telemetry/      SQLite ~/.local/share/agent-dispatch/dispatch.db
  └─ config.py       ~/.config/agent-dispatch/config.yaml + env
```

Ключевые решения и почему:

1. **Демон + тонкий MCP-прокси.** Задача исполнителя идёт 5-20 минут и должна пережить смерть исходного агента; `status` работает из любого агента и из CLI; телеметрия в одном месте. Демон стартует лениво: прокси делает `GET /health`, при отсутствии спавнит `agent-dispatch serve` detached (stdin `DEVNULL`, stdout+stderr в `data_dir/logs/serve.log`, `start_new_session=True`) и ждёт `/health`. Порт, pid и случайный токен в `~/.local/share/agent-dispatch/serve.json` (0600).
2. **Router за интерфейсом, два бэкенда.** `jev` основной, `claude_local` (`claude -p` с JSON-схемой) как fallback при недоступности Jev. Jev не является публичным API проекта и заменяем через `router.backend`; endpoint, модель и имя ключа настраиваются.
3. **Ровно один вызов Jev на dispatch/route.** Все вопросы (`executor` choice плюс телеметрийные `difficulty`, `task_type`, `risk`, `ambiguity` и `decomposable` noul) идут в одном запросе. `dispatch_to` и `status` Jev не зовут. Escalation берёт цепочку из конфига.
4. **Hop-протокол через окружение, инкремент ровно в одном месте.** Демон запускает исполнителя задачи с `hop = h` с env `AGENT_DISPATCH_HOP = h + 1`, `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`. Дочерний агент поднимает свой MCP-прокси, тот читает env и передаёт `hop = int(env)` без изменений (0, если env нет). Guard `hop >= max_hops` в демоне. Трассировка при `max_hops: 2`: root hop=0 → ребёнок HOP=1, может делегировать → внук HOP=2, его dispatch отклоняется.
   **Fan-out сабагентов (решение 2026-09-21).** Исполнитель может разбить задачу и отдать до `routing.max_children` (по умолчанию 2) подзадач через тот же `dispatch`; каждую роутит Jev. Guard `max_children` считает детей по `parent_task_id`. Task Package содержит инструкцию про fan-out только когда `hop + 1 < max_hops`. `exclude_source_agent` действует только на hop 0: сабагенты могут быть того же типа, что и родитель, цель fan-out это параллелизм. Сабагенты одной задачи делят рабочее дерево, поэтому инструкция требует непересекающихся `files`.
5. **Исполнитель правит прямо в cwd**, без worktree. `changed_files` через `git status --porcelain` до и после. cwd обязан быть git-репой (иначе `bad_cwd`): codex вне git не работает, а diff без git недостоверен. Корневые задачи с одинаковым `realpath(cwd)` выполняются последовательно.
6. **Structured Result, гибрид.** Codex через `--output-schema`. Claude и OpenCode получают в промпте инструкцию завершить ответ блоком ```` ```agent-dispatch-result {...} ```` ````, парсер берёт последний такой блок. Не распарсилось: `status=partial`, `summary` = хвост 2000 символов, `parse_error` в meta. Ненулевой exit или timeout: `failed`.
7. **Registry только из реально работающего**: `claude`, `codex`, `opencode/kimi`, `opencode/x5-code`. Descriptions executors (criteria для Jev) живут в config.yaml, не в коде.
8. **Guards в коде, не в Jev.** Порядок фиксирован (см. Technical Details), каждый guard пишет event в телеметрию.
9. **Ключи никогда не попадают в `os.environ`** и в env дочерних процессов. Env-файл читается в `Settings` как `SecretStr`.
10. **Долгие вызовы и MCP-таймауты.** У Codex дефолтный `tool_timeout_sec` 60 с, у Claude Code `MCP_TOOL_TIMEOUT`. Прокси по умолчанию ждёт `mcp.wait_seconds: 50`, потом отдаёт `running` + `task_id`, агент опрашивает `status`. README описывает, как поднять таймауты хоста и `wait_seconds`.
11. **v0.1 и v0.2 из спеки склеены**: структурированный результат и телеметрия нужны fake-адаптерам с первого дня.

## Technical Details

### Конфиг `~/.config/agent-dispatch/config.yaml`

```yaml
server:
  host: 127.0.0.1
  port: 7433
  max_concurrent_tasks: 2            # считаются только корневые задачи (hop == 0)
  data_dir: ~/.local/share/agent-dispatch

mcp:
  wait_seconds: 50                   # сколько прокси ждёт результата до ответа "running"

router:
  backend: jev                       # jev | claude_local
  jev:
    base_url: https://openrouter.ai/api/alpha/decisions
    model: typesafe/jev-1.13
    api_key_env: OPENROUTER_API_KEY
    retries: 2
    timeout_seconds: 15
  claude_local:
    command: claude
    model: null

routing:
  min_confidence: 0.60
  min_margin: 0.10
  fallback_executor: codex
  max_hops: 2
  max_children: 2                    # сабагентов на одну задачу
  exclude_source_agent: true         # исключает executors с adapter == source_agent, только на hop 0
  default_timeout_seconds: 1800
  availability_ttl_seconds: 60

executors:                           # merge по ключу с config_default.yaml; enabled: false выключает дефолтный
  claude:
    adapter: claude
    command: claude
    extra_args: ["--permission-mode", "acceptEdits", "--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]
    enabled: true
    description: "Claude Code: strongest reasoning, multi-file refactors, ambiguous or design-heavy tasks; most expensive"
  codex:
    adapter: codex
    command: codex
    extra_args: ["--sandbox", "workspace-write"]
    enabled: true
    description: "OpenAI Codex CLI: autonomous bug fixing and test-driven fixes in a single repo; medium cost"
  opencode/kimi:
    adapter: opencode
    command: opencode
    model: openrouter/kimi-k2.5
    extra_args: []
    enabled: true
    description: "OpenCode with Kimi K2.5: cheap, good for small well-specified edits, docs, boilerplate"
  opencode/x5-code:
    adapter: opencode
    command: opencode
    model: copilot/x5-airun-code-large-exp
    extra_args: []
    enabled: true
    description: "OpenCode with corporate x5-airun code model: cheap internal model for routine edits with corporate data constraints"

escalation:                          # принимается конфигом с Task 2, применяется с Task 19
  opencode/kimi: [codex, claude]
  opencode/x5-code: [codex, claude]
  codex: [claude]
```

Env-файл `~/.config/agent-dispatch/env` (`KEY=VALUE`, `#` комментарии) читается функцией `load_env_file -> dict[str, SecretStr]` и складывается в `Settings.secrets`; `os.environ` не трогается. Пути раскрываются через `expanduser`. Приоритет `data_dir`: env `AGENT_DISPATCH_DATA_DIR` > yaml > дефолт; `AGENT_DISPATCH_CONFIG_DIR` задаёт, где искать `config.yaml` и `env`.

### Модели (`agent_dispatch/models.py`, pydantic v2)

```python
class ContextMode(StrEnum): prompt, prompt_summary = "prompt+summary", full
class SourceAgent(StrEnum): claude, codex, opencode, cli, unknown
class TaskStatus(StrEnum): queued, routing, running, completed, partial, failed, needs_context, needs_escalation, cancelled
TerminalStatus = Literal["completed","partial","failed","needs_context","needs_escalation"]
class RouterKind(StrEnum): jev, claude_local, fallback, override
class GuardReason(StrEnum): low_confidence, low_margin, disabled, unavailable, router_unavailable, user_override, single_candidate, max_hops, max_children, bad_cwd, unknown_parent, escalated

class DispatchRequest(BaseModel):
    task: str                              # min_length=1
    cwd: str
    context: str | None = None
    files: list[str] = []                  # относительные, без ".."
    constraints: list[str] = []
    success_criteria: list[str] = []
    context_mode: ContextMode = ContextMode.prompt_summary
    source_agent: SourceAgent = SourceAgent.unknown
    executor: str | None = None            # dispatch_to
    allow_escalation: bool = True
    wait_seconds: int = 1800               # ge=0; 0 = вернуть task_id сразу
    timeout_seconds: int | None = None
    # подкладывает прокси из env, клиент руками не задаёт
    parent_task_id: str | None = None
    root_agent: SourceAgent | None = None
    hop: int = 0                           # ge=0

class Judgment(BaseModel): kind: Literal["choice","score","noul"]; value: str | float | bool; confidence: float; probabilities: dict[str, float]

class GuardEvent(BaseModel): reason: GuardReason; detail: str; executor: str | None = None

class RouteDecision(BaseModel):
    executor: str
    confidence: float
    scores: dict[str, float]
    router: RouterKind
    reason: GuardReason | None = None
    judgments: dict[str, Judgment] = {}
    latency_ms: int = 0
    cost_usd: float | None = None
    meta: dict[str, Any] = {}              # warning, jev_id

class Availability(BaseModel): available: bool; version: str | None; error: str | None; checked_at: datetime
class TestsInfo(BaseModel): command: str | None; result: Literal["passed","failed","not_run"] | None; output_tail: str | None
class Usage(BaseModel): input_tokens: int | None; output_tokens: int | None; cost_usd: float | None

class ExecutionResult(BaseModel):
    status: TerminalStatus
    executor: str
    model: str | None
    summary: str
    changed_files: list[str] = []
    tests: TestsInfo | None = None
    confidence: float | None = None
    needs_escalation: bool = False
    error: str | None = None
    usage: Usage | None = None
    meta: dict[str, Any] = {}              # parse_error, exit_code, duration_ms

class TaskRecord(BaseModel):
    task_id: str; parent_task_id: str | None; escalated_from: str | None; root_agent: SourceAgent; source_agent: SourceAgent; hop: int
    request: DispatchRequest; status: TaskStatus; decision: RouteDecision | None; result: ExecutionResult | None
    log_path: str                          # data_dir/logs/<task_id>.log, задаётся при создании
    created_at: datetime; started_at: datetime | None; finished_at: datetime | None

class TaskView(TaskRecord): log_tail: str  # ответ GET /tasks/{id}
```

Схема result-блока агента (`agent_dispatch/schemas/agent_result.schema.json`, та же используется для `codex --output-schema` и для валидации `-o` файла через `jsonschema`):

```json
{"type":"object","required":["status","summary"],
 "properties":{"status":{"enum":["completed","partial","failed","needs_context","needs_escalation"]},
  "summary":{"type":"string"},"changed_files":{"type":"array","items":{"type":"string"}},
  "tests":{"type":"object","properties":{"command":{"type":"string"},"result":{"enum":["passed","failed","not_run"]}}},
  "confidence":{"type":"number","minimum":0,"maximum":1},"needs_escalation":{"type":"boolean"}},
 "additionalProperties":false}
```

### Task Package и рендер промпта

`TaskPackage(BaseModel)`: `request: DispatchRequest`, `git_status: str | None`, `git_diff_stat: str | None` (заполняются только в режиме `full`). Это то, что видит роутер.

`render_prompt(package, adapter_kind: Literal["claude","codex","opencode"], settings) -> str` вызывается адаптером после решения. Шаблон `templates/task_package.md.j2`:

```
# Task
{{ task }}

# Workspace
cwd: {{ cwd }}
{% if context_mode != "prompt" %}
# Context
{{ context or "(none)" }}
## Relevant files
{{ files | bullets }}
## Constraints
{{ constraints | bullets }}
## Success criteria
{{ success_criteria | bullets }}
{% endif %}
{% if context_mode == "full" %}
# Repository state
$ git status --short
{{ git_status }}
$ git diff --stat
{{ git_diff_stat }}
{% endif %}
# Delegation
This task was delegated by {{ source_agent }} via AgentDispatch (hop {{ hop }} of {{ max_hops }}).
{% if hop + 1 >= max_hops %}Do NOT delegate further: the hop limit is reached.{% else %}You may split this task into at most {{ max_children }} independent subtasks and delegate each with the AgentDispatch `dispatch` tool; the router picks the best agent for each. Do not delegate the whole task as-is. Subtasks share this working tree: give each a disjoint `files` list.{% endif %}

# Result format
{{ result_instructions }}
```

`result_instructions` для claude/opencode: «End your final message with a fenced block tagged `agent-dispatch-result` containing JSON with fields status, summary, changed_files, tests, confidence, needs_escalation». Для codex: те же поля словами, блок не нужен.

### Порядок работы dispatcher (`dispatch/dispatcher.py`)

```
0. создание TaskRecord(status=queued) всегда, даже если guard откажет на шаге 1
1. pre-guards (guards.py, чистые функции)
   bad_cwd         cwd не существует, не директория или не git-репа       → failed
   max_hops        hop >= routing.max_hops                                  → failed
   max_children    parent_task_id задан и у родителя уже max_children детей   → failed
   unknown_parent  parent_task_id задан, но не найден в storage            → failed
   override        executor задан явно: disabled или unavailable → failed; иначе RouteDecision(router=override, reason=user_override, confidence=1.0), Jev не зовём, exclude_source_agent не применяется
2. кандидаты = enabled − unavailable(кэш, TTL) − {e | e.adapter == source_agent} при exclude_source_agent и hop == 0
   пусто           → failed, reason=unavailable
   один            → RouteDecision(router=fallback, reason=single_candidate, confidence=1.0)
3. decide_with_fallback(package, candidates) → RouteDecision
   jev: 1 POST; сеть/5xx/timeout → retry ×retries с backoff 0.5s, 1s → claude_local → RouteDecision(router=fallback, executor=fallback_executor, confidence=0, reason=router_unavailable)
   невалидный ответ (нет choice; choice вне кандидатов; |Σp − 1| > 0.02) → как ошибка
4. post-guards
   confidence < min_confidence                    → fallback_executor, reason=low_confidence
   top1 − top2 < min_margin − 1e-9                → fallback_executor, reason=low_margin
   fallback_executor не в кандидатах              → оставить top1, meta.warning + GuardEvent
5. семафор (только hop == 0) и lock по realpath(cwd) (только hop == 0); adapter.execute(package, RunContext) с timeout; нормализация; task → finished
6. (v0.3, Task 19) если результат failed/needs_escalation/tests.result == failed и allow_escalation: следующий из escalation[executor]; новая TaskRecord с escalated_from=task_id, тем же parent_task_id и hop, router=fallback, reason=escalated
```

Состояния задачи: `queued → routing → running → {completed, partial, failed, needs_context, needs_escalation, cancelled}`.

Env дочернего процесса: `os.environ` минус все ключи, имена которых есть в `Settings.secrets`, плюс `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP = hop + 1`.

### HTTP API (`api/`)

Все запросы, кроме `/health`, требуют `Authorization: Bearer <token>` из `serve.json`; неверный токен → 401. Middleware отклоняет `Host`, отличный от `127.0.0.1:<port>` и `localhost:<port>` (защита от DNS rebinding) → 421.

| Метод | Путь | Тело / ответ |
|---|---|---|
| GET | `/health` | `{status, version, pid, uptime_s, router_backend}` |
| GET | `/executors` | `[{name, adapter, model, enabled, available, checked_at, error}]` |
| POST | `/route` | `DispatchRequest` → `RouteDecision`; решение пишется в `routing_decisions` с `task_id = NULL` |
| POST | `/tasks` | `DispatchRequest` → `TaskView` (ждёт до `wait_seconds`, потом отдаёт текущий статус) |
| GET | `/tasks/{id}` | `TaskView` (`log_tail` последние 4 КБ `log_path`) |
| DELETE | `/tasks/{id}` | cancel → `TaskView(status=cancelled)`; завершённая → 409 |
| POST | `/tasks/{id}/feedback` | `{outcome: success|failure|manual_override|escalated|user_accepted|user_reworked, note}` |
| GET | `/export?since=7d` | JSONL, одна строка на задачу: `{task, decision, events: [...]}`; решения `/route` без задачи идут отдельными строками `{task: null, decision, events: []}` |

### Адаптеры (`executors/`)

```python
class RunContext(BaseModel): cwd: str; timeout_seconds: int; env: dict[str, str]; log_path: Path; task_id: str; prompt: str

class ExecutorAdapter(Protocol):
    name: str
    async def check(self) -> Availability: ...
    async def execute(self, ctx: RunContext) -> ExecutionResult: ...
```

`executors/process.py`: `run_cli(argv, cwd, env, stdin, timeout_seconds, log_path, grace_seconds=10.0) -> ProcessOutcome(exit_code, stdout, stderr, timed_out, duration_ms)`. Запуск через `asyncio.create_subprocess_exec(start_new_session=True)`; stdout/stderr стримятся в `log_path`; timeout или `CancelledError`: `SIGTERM` группе через `os.killpg`, через `grace_seconds` `SIGKILL`. Имя бинаря из `ExecutorSettings.command`, тесты подставляют fake-CLI полным путём.

| адаптер | argv | stdin | канал результата |
|---|---|---|---|
| claude | `<command> -p --output-format json --add-dir <cwd> <extra_args>` | prompt | поле `result` JSON-обёртки → последний блок `agent-dispatch-result`; `model` из `modelUsage`, если есть |
| codex | `<command> exec --json -C <cwd> --output-schema <schema> -o <last.json> <extra_args> -` | prompt | `<last.json>` валидируется `jsonschema` |
| opencode | `<command> run --format json --dir <cwd> --model <model> <extra_args> <prompt>` | — | события JSON, текст ассистента → последний блок |

`executors/result_parser.py`: `extract_result_block(text) -> dict | None`, `normalize(raw, outcome, changed_files, executor, model) -> ExecutionResult`. `executors/workspace.py`: `is_git_repo(cwd)`, `snapshot(cwd) -> set[str]`, `diff(before, after) -> list[str]`.

### Телеметрия (`telemetry/storage.py`, sqlite3)

```sql
tasks(task_id PK, parent_task_id, escalated_from, root_agent, source_agent, hop, cwd, status, executor, model,
      request_json, decision_json, result_json, log_path, created_at, started_at, finished_at, duration_ms)
routing_decisions(id PK, task_id NULL, router, candidates_json, choice, confidence, scores_json,
      judgments_json, guard_reason, latency_ms, cost_usd, created_at)
events(id PK, task_id FK, ts, kind, payload_json)     -- kind: guard|spawn|exit|parse|escalate|feedback|cancel
```

WAL-режим, один writer через `asyncio.Lock`. Схема создаётся `CREATE TABLE IF NOT EXISTS` из `telemetry/schema.sql`, версия в `PRAGMA user_version = 1`; отдельный раннер миграций появится со второй миграцией.

### MCP-прокси (`mcp/server.py`, FastMCP stdio)

Tools `route`, `dispatch`, `dispatch_to`, `status`. Каждый вызов: собрать `DispatchRequest`, подложить `parent_task_id`/`root_agent`/`hop` из env `AGENT_DISPATCH_*` (hop = `int(env)`, без инкремента), `source_agent` из env `AGENT_DISPATCH_SOURCE_AGENT` (задаётся в конфиге MCP каждого агента) → HTTP в демон с Bearer из `serve.json`. `wait_seconds` по умолчанию `mcp.wait_seconds`; httpx timeout = `wait_seconds + 30`. Ответ tool это текст: шапка (`task_id, executor, status, changed_files, summary`) плюс JSON. Если статус `running`: подсказка «call status with task_id». Логи прокси только в stderr, `print` запрещён. При отсутствии демона: `ensure_daemon(settings, deadline_seconds=5, sleep=asyncio.sleep)`.

### CLI (`cli.py`, typer)

`serve`, `mcp`, `route <task>`, `dispatch <task> [--executor X] [--cwd .] [--context ...] [--wait N]`, `status <task_id>`, `cancel <task_id>`, `executors`, `doctor [--online] [--json]`, `feedback <task_id> --outcome X`, `export --since 7d --format jsonl`. Все, кроме `serve`, `mcp`, `doctor`, ходят в HTTP API демона с токеном из `serve.json`.

### Что даёт измеримый outcome

`agent-dispatch export` выгружает датасет, из которого считаются: routing accuracy (после разметки feedback), доля `fallback`/`override`/`low_confidence`, success rate и медианная длительность по executor. Первая цифра после недели использования: routing accuracy ≥ 0.8 на размеченных задачах и доля `low_confidence` < 20%.

Замечание для docs/architecture.md: `state` Jev содержит `task` и `context` и уходит на OpenRouter даже для задач, которые потом пойдут в корпоративную модель `opencode/x5-code`. Если это ограничение, задачу нужно отправлять через `dispatch_to`.

## What Goes Where

- **Implementation Steps**: всё, что делается в этой репе: код, тесты, evals, документация, конфиг-пример.
- **Post-Completion**: подключение MCP к трём агентам на машине пользователя, ротация ключа, наблюдение за телеметрией.

## Implementation Steps

### Task 1: Скелет проекта, зависимости, gitignore, pre-commit

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml`, `.secrets.baseline`, `README.md` (заглушка), `agent_dispatch/__init__.py`, `agent_dispatch/version.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

- [x] `pyproject.toml`: пакет `agent_dispatch`, entrypoint `agent-dispatch = agent_dispatch.cli:app`, deps pydantic, fastapi, uvicorn, mcp, httpx, pyyaml, jinja2, typer, jsonschema; dev pytest, pytest-asyncio, respx, ruff, detect-secrets; `asyncio_mode = "auto"`
- [x] `.gitignore`: `.venv/`, `__pycache__/`, `*.db`, `env`, `.env*`, `logs/`, `dist/`, `evals/reports/`
- [x] `.pre-commit-config.yaml`: ruff check + format, detect-secrets, `uv run pytest tests/ -q` как local hook
- [x] `tests/conftest.py`: фикстура `tmp_config_dir` (изолированные `config.yaml`, `env`, `data_dir` через env `AGENT_DISPATCH_CONFIG_DIR`/`AGENT_DISPATCH_DATA_DIR`), фикстура `git_repo` (temp-репа с одним коммитом)
- [x] `tests/test_smoke.py`: импорт пакета, версия строка
- [x] `uv sync`, `uv run pytest tests/ -q` зелёный, `uv run ruff check .` чистый, pre-commit проходит на тестовом коммите

### Task 2: Config: загрузка YAML + env, валидация, дефолты

**Files:**
- Create: `agent_dispatch/config.py`, `agent_dispatch/config_default.yaml`, `tests/test_config.py`, `tests/fixtures/config/minimal.yaml`, `tests/fixtures/config/full.yaml`, `tests/fixtures/config/bad_fallback.yaml`, `tests/fixtures/config/bad_escalation.yaml`

- [x] тесты: дефолты без файла; полный файл парсится в `Settings`; `fallback_executor` не из `executors` → `ConfigError`; `adapter: opencode` без `model` → `ConfigError`; `escalation` с неизвестным именем → `ConfigError`; `command` по умолчанию равен `adapter`; `extra_args` по умолчанию из `config_default.yaml`; `executors` мержатся по ключу, `enabled: false` выключает дефолтный, новый ключ добавляется; env-файл `KEY=VALUE` парсится, `#` и пустые строки пропускаются, значение с `=` внутри сохраняется; после загрузки `os.environ` не содержит ключей; `Settings.secrets` это `SecretStr`, `repr(settings)` не содержит значения; `~` раскрывается; `AGENT_DISPATCH_DATA_DIR` побеждает yaml; `AGENT_DISPATCH_CONFIG_DIR` задаёт путь
- [x] pydantic-модели `ServerSettings`, `McpSettings`, `JevSettings`, `ClaudeLocalSettings`, `RouterSettings`, `RoutingSettings`, `ExecutorSettings` (`adapter, command, model, extra_args, enabled, description`), `Settings` c `model_validator`, `extra="forbid"`
- [x] `load_settings(config_dir: Path | None = None) -> Settings`, `load_env_file(path) -> dict[str, SecretStr]`
- [x] `Settings.enabled_executors()`, `Settings.secret(name) -> str | None`, `Settings.secret_names() -> set[str]`
- [x] run tests - must pass before next task

### Task 3: Модели домена и схема result-блока

**Files:**
- Create: `agent_dispatch/models.py`, `agent_dispatch/schemas/agent_result.schema.json`, `agent_dispatch/schemas/__init__.py`, `tests/test_models.py`

- [x] тесты: `DispatchRequest` с пустым `task` → ошибка; `wait_seconds=-1` → ошибка; `hop` по умолчанию 0; `files` с `..` → ошибка; `ContextMode("prompt+summary")` парсится; `ExecutionResult` round-trip JSON; `ExecutionResult(status="queued")` → ошибка; схема валидна по `jsonschema.Draft202012Validator.check_schema`, принимает `{status, summary}`, отвергает лишнее поле и `status="running"`
- [x] реализовать модели из Technical Details, включая `GuardEvent`, `Availability`, `TaskView`
- [x] `schemas/__init__.py`: `load_agent_result_schema()`, `validate_agent_result(obj) -> list[str]` (ошибки)
- [x] run tests - must pass before next task

### Task 4: Task Package и рендер промпта со снапшотом

**Files:**
- Create: `agent_dispatch/dispatch/__init__.py`, `agent_dispatch/dispatch/task_package.py`, `agent_dispatch/templates/task_package.md.j2`, `tests/test_task_package.py`, `tests/snapshots/prompt_claude_prompt.md`, `tests/snapshots/prompt_claude_summary.md`, `tests/snapshots/prompt_claude_full.md`, `tests/snapshots/prompt_codex_summary.md`

- [ ] тесты `build_task_package(req, settings)`: в режиме `prompt`/`prompt+summary` `git_status is None`; в `full` заполнен из `git_repo` с изменённым файлом
- [ ] тесты `render_prompt(package, adapter_kind, settings)`: четыре снапшота совпадают; `prompt` не содержит `Constraints`; `hop=1, max_hops=2` даёт `Do NOT delegate further` и не содержит `split this task`; `hop=0` даёт инструкцию fan-out с числом `max_children`; `codex` и `claude` различаются только секцией `Result format`
- [ ] `TaskPackage`, `build_task_package`, `render_prompt`; git-команды только в `full`
- [ ] run tests - must pass before next task

### Task 5: Парсер result-блока и нормализация ExecutionResult

**Files:**
- Create: `agent_dispatch/executors/__init__.py`, `agent_dispatch/executors/result_parser.py`, `agent_dispatch/executors/process.py` (только `ProcessOutcome`), `tests/test_result_parser.py`, `tests/fixtures/agent_output/*.txt`

- [ ] тесты `extract_result_block`: один блок; два блока (последний); мусор до/после; без закрывающих бэктиков → `None`; невалидный JSON → `None`; блок с языком `json` без тега → `None`; пустой текст → `None`
- [ ] тесты `normalize`: валидный блок → `completed`; `None` + exit 0 → `partial`, `summary` = хвост ≤ 2000 символов, `meta.parse_error`; exit ≠ 0 → `failed`, `error` = хвост stderr; `timed_out` → `failed`, `error="timeout"`; `changed_files` из git побеждают поле агента; `status` вне enum → `partial` с `parse_error`; лишнее поле в блоке → `partial` с `parse_error` (через `validate_agent_result`)
- [ ] реализовать `ProcessOutcome`, `extract_result_block`, `normalize`
- [ ] run tests - must pass before next task

### Task 6: Guards: чистые функции с табличными тестами

**Files:**
- Create: `agent_dispatch/routing/__init__.py`, `agent_dispatch/routing/guards.py`, `tests/test_guards.py`

- [ ] тесты `pre_guards(req, settings, unavailable: set[str], parent_exists: bool, sibling_count: int) -> GuardEvent | RouteDecision | None`: bad_cwd (нет, файл, не git); hop ≥ max_hops; hop < max_hops проходит; parent задан и не существует → unknown_parent; `sibling_count >= max_children` → max_children, `sibling_count < max_children` проходит; explicit disabled; explicit unavailable; explicit ok → `RouteDecision(router=override)` даже если executor.adapter == source_agent
- [ ] тесты `candidates(settings, unavailable, source_agent, hop) -> list[str]`: `source_agent=opencode, hop=0` исключает оба `opencode/*`; `codex` исключает только `codex`; при `hop=1` ничего не исключается даже с флагом true; флаг false ничего не исключает; unavailable и disabled вычитаются; `cli`/`unknown` ничего не исключают
- [ ] тесты `post_guards(decision, settings, candidates) -> tuple[RouteDecision, list[GuardEvent]]`: low_confidence; low_margin при `0.55/0.45` (разница 0.10 проходит с допуском 1e-9), `0.54/0.46` не проходит; fallback не в кандидатах → top1 + `meta.warning` + событие; всё ок → без изменений и без событий
- [ ] реализовать `pre_guards`, `candidates`, `post_guards`, `single_candidate_decision`
- [ ] run tests - must pass before next task

### Task 7: Router-интерфейс и Jev-клиент на фикстурах

**Files:**
- Create: `agent_dispatch/routing/base.py`, `agent_dispatch/routing/jev.py`, `agent_dispatch/routing/questions.py`, `tests/test_jev_router.py`, `tests/fixtures/jev/response_unknown_choice.json`
- Modify: `tests/fixtures/jev/request_executor.json` (убрать `cwd` из `state`, переименовать `context_summary` в `context`)

- [ ] фикстуры: `request_executor.json` это точное ожидаемое тело для тестового запроса; `response_ok.json` и `response_400_score_criteria.json` уже записаны с живой пробы 2026-09-21
- [ ] тесты (respx): тело запроса равно `request_executor.json` (state = `{task, context, files, constraints, source_agent}`, без `cwd`); `score.criteria` массив; ответ ok → `RouteDecision(router=jev, executor=codex, scores, judgments.difficulty, meta.jev_id)`; `latency_ms >= 0`; `cost_usd` из usage; 500 ×3 → `RouterError` после `retries` попыток и backoff через инжектированный `sleep`; 400 → `RouterError` без retry; 401/403 → `RouterError` без retry с текстом ошибки; choice вне кандидатов → `RouterError`; Σp ≠ 1 → `RouterError`; нет ключа → `RouterError` до сетевого вызова; `base_url`/`model` из конфига попадают в запрос
- [ ] `routing/base.py`: `Router` Protocol (`decide(package, candidates: dict[str, str]) -> RouteDecision`), `RouterError`
- [ ] `routing/questions.py`: `build_state(package)`, `build_questions(candidates)` (executor choice + difficulty/task_type/risk/ambiguity score/choice + decomposable noul); ответ noul парсится в `Judgment(kind="noul", value=bool, probabilities={"true": p, "false": 1-p})`
- [ ] `routing/jev.py`: `JevRouter(settings, client, sleep=asyncio.sleep)`
- [ ] run tests - must pass before next task

### Task 8: claude_local роутер и цепочка fallback

**Files:**
- Create: `agent_dispatch/routing/claude_local.py`, `agent_dispatch/routing/decision.py`, `tests/test_claude_local_router.py`, `tests/test_decision.py`, `tests/fakes/claude_router_ok.sh`, `tests/fakes/claude_router_bad.sh`

- [ ] тесты claude_local (fake-CLI печатает `{"result": "{\"executor\":\"codex\",\"scores\":{...}}"}`): валидный → `RouteDecision(router=claude_local)`; exit ≠ 0 → `RouterError`; невалидный JSON → `RouterError`; executor вне кандидатов → `RouterError`
- [ ] тесты `decide_with_fallback(package, candidates, settings, routers) -> tuple[RouteDecision, list[GuardEvent]]`: jev ok → jev; jev error → claude_local + событие; оба error → `router=fallback, reason=router_unavailable, confidence=0`; один кандидат → без вызовов роутеров
- [ ] `claude_local.py`: `<command> -p --output-format json` с промптом «choose executor, respond with JSON {executor, scores}»; парсинг `result`
- [ ] `decision.py`: оркестрация pre/candidates/router/post, возвращает решение и события
- [ ] run tests - must pass before next task

➕ run A (codex-flow 20260921-160148-a904): тесты флакали при первом exec новых fake-скриптов (syspolicyd на macOS), добавлен session-прогрев в conftest. Найдено и исправлено в ревью: env-парсер комментариев, YAML→ConfigError, deadlock stdin>64КБ, лимит строки 64КиБ, SIGKILL группе после выхода родителя, `git status -z`, `pgid = process.pid`.

### Task 9: Процесс-раннер и workspace: запуск CLI, лог, timeout, cancel, kill group

**Files:**
- Modify: `agent_dispatch/executors/process.py`
- Create: `agent_dispatch/executors/workspace.py`, `tests/test_process.py`, `tests/test_workspace.py`, `tests/fakes/echo_ok.sh`, `tests/fakes/sleep_forever.sh`, `tests/fakes/ignore_term.sh`, `tests/fakes/spawn_child_and_sleep.sh`, `tests/fakes/exit_3.sh`

- [x] тесты `run_cli`: exit 0 stdout собран; exit 3; timeout 0.2 с на `sleep_forever` → `timed_out`, процесс мёртв; `ignore_term` с `grace_seconds=0.2` → убит SIGKILL, `duration < 1 с`; `spawn_child_and_sleep` → в течение 1 с `os.killpg(pgid, 0)` даёт `ProcessLookupError`; отмена `asyncio.Task` во время работы → группа убита, `CancelledError` пробрасывается; stdin передаётся; лог содержит stdout и stderr; `duration_ms >= 0`
- [x] тесты workspace: `is_git_repo` true/false; правка одного файла и создание второго → `["a.py", "b.py"]` отсортировано; untracked и modified оба учитываются
- [x] `run_cli` с `grace_seconds` параметром, `killpg` в `finally` при `CancelledError`
- [x] `is_git_repo`, `snapshot`, `diff`
- [x] run tests - must pass before next task

### Task 10: База адаптеров, registry, Claude-адаптер

**Files:**
- Create: `agent_dispatch/executors/base.py`, `agent_dispatch/executors/registry.py`, `agent_dispatch/executors/claude.py`, `tests/test_adapter_claude.py`, `tests/test_registry.py`, `tests/fakes/claude_ok.sh`, `tests/fakes/claude_noblock.sh`, `tests/fakes/version_only.sh`, `tests/fixtures/agent_output/claude_print_json.txt`

- [ ] записать один живой `claude -p --output-format json` ответ на тривиальную задачу в `claude_print_json.txt` (без секретов), fake-CLI печатает ровно эту форму
- [ ] тесты: argv ровно `<fake> -p --output-format json --add-dir <cwd> <extra_args>`; промпт в stdin; `check()` парсит `--version`; `completed` с `changed_files` из git; `claude_noblock` → `partial`; timeout 0.2 с → `failed`, `error="timeout"`; `model` заполняется из `modelUsage`, если поле есть, иначе `None`; env дочернего процесса содержит `AGENT_DISPATCH_*` и не содержит `OPENROUTER_API_KEY`
- [ ] `base.py`: `ExecutorAdapter` Protocol, `RunContext`
- [ ] `registry.py`: `build_adapters(settings)`, `AvailabilityCache(ttl_seconds, clock=time.monotonic)` с `check_all()`, `unavailable() -> set[str]`; тесты кэша с инжектированными часами
- [ ] `claude.py`
- [ ] run tests - must pass before next task

### Task 11: Codex-адаптер

**Files:**
- Create: `agent_dispatch/executors/codex.py`, `tests/test_adapter_codex.py`, `tests/fakes/codex_ok.sh`, `tests/fakes/codex_bad_schema.sh`, `tests/fakes/codex_fail.sh`

- [ ] тесты: argv ровно `<fake> exec --json -C <cwd> --output-schema <schema> -o <last.json> <extra_args> -`; промпт в stdin; `codex_ok` пишет JSON в файл после `-o` → `completed`; `codex_bad_schema` (лишнее поле) → `partial` с `parse_error`; `codex_fail` exit 1 → `failed`; файл `-o` отсутствует при exit 0 → `partial`; временные файлы схемы и `-o` удаляются
- [ ] `codex.py`
- [ ] run tests - must pass before next task

### Task 12: OpenCode-адаптер

**Files:**
- Create: `agent_dispatch/executors/opencode.py`, `tests/test_adapter_opencode.py`, `tests/fakes/opencode_ok.sh`, `tests/fakes/opencode_noblock.sh`, `tests/fixtures/agent_output/opencode_run_json.txt`

- [ ] записать один живой `opencode run --format json` вывод в фикстуру (без секретов), fake печатает эту форму
- [ ] тесты: argv ровно `<fake> run --format json --dir <cwd> --model <model> <extra_args> <prompt>`; `model` из конфига попадает в результат; текст ассистента извлекается из событий → `completed`; `opencode_noblock` → `partial`; `check()` через `--version`
- [ ] `opencode.py`, парсер событий `--format json` (только тип «text assistant», остальное игнорируется)
- [ ] run tests - must pass before next task

### Task 13: Телеметрия SQLite

**Files:**
- Create: `agent_dispatch/telemetry/__init__.py`, `agent_dispatch/telemetry/storage.py`, `agent_dispatch/telemetry/schema.sql`, `tests/test_storage.py`

- [ ] тесты: создание БД в tmp, `user_version = 1`; `insert_task`/`update_task`/`get_task` round-trip `TaskRecord`; `task_exists`; `add_decision` с `task_id=None` для `/route`; `add_event`; `export(since)` отдаёт по строке на задачу с вложенными `decision` и `events`, плюс строки `{task: null, decision}`; повторный `open` идемпотентен; 20 конкурентных записей не теряются
- [ ] `count_children(parent_task_id) -> int` (все статусы, кроме `cancelled`)
- [ ] `Storage(path)`: `open()`, схема из `schema.sql`, WAL, `asyncio.Lock` на запись, методы выше
- [ ] run tests - must pass before next task

### Task 14: Dispatcher: state machine, семафор корневых задач, lock по cwd, cancel

**Files:**
- Create: `agent_dispatch/dispatch/dispatcher.py`, `tests/test_dispatcher.py`, `tests/fakes/adapters.py` (`FakeAdapter` с настраиваемым результатом, `asyncio.Event` для управления ходом, опциональным callback «сделать вложенный submit»)

- [ ] тесты: happy path `submit → completed`, decision и result записаны, события `guard/spawn/exit`; `wait_seconds=0` → `queued` сразу, потом `completed`; `max_concurrent_tasks=1`, две корневые задачи → вторая стартует только после `Event` первой; `max_concurrent_tasks=1`, корневая задача делает вложенный `submit(hop=1, parent_task_id=self)` и ждёт его → ребёнок выполняется, deadlock нет; две корневые задачи в одном `realpath(cwd)` сериализуются, в разных cwd идут параллельно; `cancel` во время `running` → `cancelled`, адаптер отменён; hop ≥ max → `failed` без вызова адаптера; explicit executor → `router=override`; неизвестный `parent_task_id` → `failed, reason=unknown_parent`; третий ребёнок одного родителя при `max_children=2` → `failed, reason=max_children`, первые два выполняются параллельно; исключение адаптера → `failed` с `error`, воркер жив; `log_path` задаётся при создании и лежит в `data_dir/logs/`; env дочернего процесса без секретов и с `AGENT_DISPATCH_HOP = hop + 1`
- [ ] `Dispatcher(settings, storage, adapters, availability, routers)`: `submit`, `wait`, `get`, `cancel`, `route_only`; семафор и per-cwd lock только для `hop == 0`
- [ ] run tests - must pass before next task

### Task 15: HTTP API демона (FastAPI), токен, Host-check, команда serve

**Files:**
- Create: `agent_dispatch/api/__init__.py`, `agent_dispatch/api/app.py`, `agent_dispatch/api/routes.py`, `agent_dispatch/api/auth.py`, `agent_dispatch/server.py`, `agent_dispatch/serve_state.py`, `tests/test_api.py`, `tests/test_serve_state.py`

- [ ] тесты API (httpx `ASGITransport`, fake-адаптеры): все эндпоинты из таблицы; без токена → 401; неверный `Host` → 421; `/health` без токена работает; `POST /tasks` невалидное тело → 422; `GET /tasks/unknown` → 404; `DELETE` завершённой → 409; `/route` пишет decision с `task_id NULL`; `/export` JSONL; `/executors` показывает `available`; `GET /tasks/{id}` у running-задачи отдаёт `log_tail` из существующего лог-файла
- [ ] тесты `serve_state`: `write_state(data_dir, pid, port, token)` создаёт файл с правами 0600; `read_state`; `is_alive(state)` false для мёртвого pid; `clear_state`
- [ ] `create_app(settings, dispatcher, storage, availability)`, lifespan открывает storage и прогревает кэш
- [ ] `server.py`: `run_server(settings)` через `uvicorn.Server`, генерирует токен `secrets.token_urlsafe(32)`, пишет state, удаляет при выходе; отказ, если state живой
- [ ] run tests - must pass before next task

### Task 16: MCP-прокси (stdio) и автостарт демона

**Files:**
- Create: `agent_dispatch/mcp/__init__.py`, `agent_dispatch/mcp/server.py`, `agent_dispatch/mcp/client.py`, `agent_dispatch/mcp/autostart.py`, `tests/test_mcp_server.py`, `tests/test_autostart.py`

- [ ] тесты MCP через in-memory клиент SDK: `list_tools` ровно `route, dispatch, dispatch_to, status` с описаниями; `dispatch` подкладывает `parent_task_id/root_agent/hop` из env `AGENT_DISPATCH_*` (hop = `int(env)`, без env = 0) и `source_agent` из `AGENT_DISPATCH_SOURCE_AGENT`; HTTP замокан respx и получает правильное тело и Bearer; `wait_seconds` по умолчанию `mcp.wait_seconds`; ответ `running` содержит подсказку про `status`; ошибка демона → `isError=True`; за время теста в `sys.stdout` не попадает ничего, кроме MCP-транспорта (перехват `capsys`)
- [ ] тесты autostart: живой `/health` → `Popen` не вызывался; мёртвый → `Popen` вызван с `stdin=DEVNULL`, `stdout` файл `serve.log`, `stderr=STDOUT`, `start_new_session=True`, затем ожидание `/health` с инжектированными `deadline_seconds=0.2, sleep`; таймаут → `DaemonUnavailable`
- [ ] `mcp/client.py`: `DispatchClient(state)` над httpx, timeout = `wait_seconds + 30`
- [ ] `mcp/server.py`: FastMCP `agent-dispatch`, логирование в stderr, четыре tool с описаниями из спеки (когда использовать, не ре-диспатчить делегированное)
- [ ] `mcp/autostart.py`: `ensure_daemon(settings, deadline_seconds=5.0, sleep=asyncio.sleep, popen=subprocess.Popen)`
- [ ] run tests - must pass before next task

### Task 17: CLI (typer): serve, mcp, route, dispatch, status, cancel, executors, feedback, export

**Files:**
- Create: `agent_dispatch/cli.py`, `tests/test_cli.py`

- [ ] тесты (`CliRunner`, HTTP замокан respx, `serve.json` в tmp): `route "fix tests"` печатает `executor:` и `confidence:`; `dispatch --executor opencode/kimi "x"` шлёт `executor`; `dispatch --wait 0` печатает `task_id`; `status <id>`; `cancel <id>`; `executors` таблица; `feedback <id> --outcome user_accepted`; `export --since 7d` пишет JSONL в stdout; демон недоступен → exit 2 с подсказкой; токен из `serve.json` уходит в Bearer
- [ ] `cli.py`: команды через `DispatchClient`, `dispatch`/`route` зовут `ensure_daemon`
- [ ] run tests - must pass before next task

### Task 18: doctor

**Files:**
- Create: `agent_dispatch/doctor.py`, `tests/test_doctor.py`
- Modify: `agent_dispatch/cli.py`

- [ ] тесты (`command` каждого executor указывает на fake или на несуществующий путь, respx для `/health` и Jev): строки «config найден», «env: OPENROUTER_API_KEY задан/не задан» без значения, «демон жив/нет», по строке на executor с версией или ошибкой, «Jev: ok» только с `--online`; `--json` отдаёт структуру; exit 1, если хоть одна проверка красная; вывод не содержит значения ключа
- [ ] `doctor.py`: `run_checks(settings, online: bool) -> list[Check]`; команда `doctor` в CLI
- [ ] run tests - must pass before next task

### Task 19: Escalation chains (v0.3) поверх dispatcher

**Files:**
- Create: `agent_dispatch/dispatch/escalation.py`, `tests/test_escalation.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`, `tests/test_dispatcher.py`

- [ ] тесты: `kimi failed → codex completed` даёт две TaskRecord, вторая с `escalated_from` первой, тем же `parent_task_id` и `hop`, событие `escalate`; `needs_escalation=True` при `completed` эскалирует; `tests.result == failed` эскалирует; `allow_escalation=False` не эскалирует; цепочка исчерпана → финальный `failed` с `meta.escalation_chain`; executor без цепочки → без эскалации; эскалация не зовёт роутер; ребёнок эскалации не эскалирует повторно к уже пройденному executor
- [ ] `escalation.next_executor(current, settings, tried) -> str | None`
- [ ] интеграция в `Dispatcher._run`, `decision.reason=escalated`
- [ ] run tests - must pass before next task

### Task 20: Evals: routing accuracy и smoke на живых CLI

**Files:**
- Create: `evals/__init__.py`, `evals/routing/__init__.py`, `evals/routing/cases.jsonl`, `evals/routing/__main__.py`, `evals/smoke/__init__.py`, `evals/smoke/__main__.py`, `evals/smoke/repo_template/` (мини-проект с pytest и одним намеренно падающим тестом), `evals/README.md`

- [ ] `cases.jsonl`: 20 задач `{task, context, files, constraints, expected_executor, rationale}`; примерно 6 claude, 7 codex, 7 opencode
- [ ] `evals/routing/__main__.py`: живой роутер через `POST /route` живого демона, accuracy, отчёт `evals/reports/routing-<date>.json`, матрица ошибок, exit 1 при accuracy < 0.8; флаг `--router claude_local`
- [ ] `evals/smoke/__main__.py`: копирует `repo_template` в tmp, `git init` + commit, три задачи через `dispatch_to` на каждом enabled executor через живой демон, проверяет `completed`, `changed_files` непустой, `pytest` зелёный; `--executors codex,claude`
- [ ] прогнать оба eval вживую, вписать результат с датой в `evals/README.md`
- [ ] run gate tests - must pass before next task

### Task 21: Verify acceptance criteria
- [ ] живая проверка headless-прав: `claude -p` с `extra_args` из дефолта выполняет `pytest` без TTY; `opencode run` выполняет команду без подтверждения; если нет, поправить дефолтные `extra_args`/README и обновить план (➕)
- [ ] happy path раздела 19 спеки руками: из Codex вызвать `dispatch` в temp-репе, получить результат, `AGENT_DISPATCH_HOP=1` в env дочернего процесса, запись в SQLite
- [ ] цикл: дочерний агент при `max_hops=1` получает `failed, reason=max_hops`
- [ ] `route` из CLI и из MCP на одном запросе дают одинаковые `executor` и `router`
- [ ] ключи не появляются в: `serve.log`, `logs/<task_id>.log`, `export`, `doctor`, `status`; `grep -r sk-or- ~/.local/share/agent-dispatch` пуст
- [ ] `uv run pytest tests/ -q` < 5 с, `uv run ruff check .` чистый, `uv run python -m evals.routing` ≥ 0.8
- [ ] pre-commit хук (включая detect-secrets) работает на тестовом коммите

### Task 22: [Final] Документация и подключение
- [ ] `README.md` на русском: что это, установка (`uv tool install .`), конфиг с примером, команды CLI, подключение MCP к Claude Code (`claude mcp add agent-dispatch -e AGENT_DISPATCH_SOURCE_AGENT=claude -- agent-dispatch mcp`), Codex (`~/.codex/config.toml` `[mcp_servers.agent-dispatch]` с `tool_timeout_sec`), OpenCode (`opencode.json` `mcp`), как поднять MCP-таймауты и `wait_seconds`, текст правила для агентов из раздела 16 спеки, как читать телеметрию, как добавить новый executor (только config.yaml)
- [ ] `docs/architecture.md`: схема, порядок guards, hop-протокол с трассировкой, контракт Jev с примером запроса и ответа, формат result-блока, замечание про уход `task/context` во внешний роутер
- [ ] тест `tests/test_config_example.py`: `config.example.yaml` в корне равен `agent_dispatch/config_default.yaml`
- [ ] `CLAUDE.md` репы: как запускать тесты и evals, где фикстуры, правило «Jev не делает side effects», правило «ключи не в os.environ»
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

**Manual verification:**
- подключить MCP ко всем трём агентам на этой машине по README и прогнать по одной делегации из каждого
- через неделю использования: `agent-dispatch export --since 7d`, разметить outcome через `feedback`, посчитать routing accuracy и долю `low_confidence`; если accuracy < 0.8, править `description` executors в config.yaml, а не код

**External system updates:**
- пробный ключ OpenRouter, переданный в чате 2026-09-21, ротировать после первого рабочего прогона (он был показан в переписке)
- при появлении DeepSeek/GLM в `~/.config/opencode/opencode.json` добавить их в `executors` и `escalation` config.yaml, код менять не нужно
- codex-lb должен быть запущен (`~/my_git_reps/codex-lb/clb status`), иначе `doctor` покажет codex недоступным
