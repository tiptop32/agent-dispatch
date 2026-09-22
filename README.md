# AgentDispatch

Маршрутизация coding-задач между Claude Code, Codex и OpenCode. Start anywhere. Dispatch anywhere.

Агент, в котором вы работаете, вызывает один MCP tool `dispatch`. AgentDispatch собирает компактный Task Package, спрашивает у Jev (judgment-модель TypeSafe), какой исполнитель подходит, запускает выбранный CLI в вашей рабочей копии и возвращает структурированный результат. Jev только принимает решение; запуск, guards, таймауты и телеметрия живут в обычном коде.

```text
Claude Code ─┐
Codex ───────┼──→ MCP `dispatch` ──→ демон agent-dispatch ──→ Jev (choice) ──→ guards ──→ исполнитель
OpenCode ────┘                                                                   claude | codex | opencode/<model>
```

## Что внутри

- `agent-dispatch serve`: локальный демон (HTTP на `127.0.0.1:7433`, SQLite-телеметрия, очередь задач). Переживает смерть агента, который его вызвал.
- `agent-dispatch mcp`: stdio MCP-сервер, тонкий прокси в демон. Поднимает демон сам, если тот не запущен.
- Tools: `route` (только решение), `dispatch` (решение + запуск), `dispatch_to` (явный исполнитель), `status`.
- Guards в коде: недоступный исполнитель, лимит глубины делегирования (`max_hops`), лимит сабагентов на задачу (`max_children`), низкая уверенность Jev, таймаут, отмена.
- Escalation chains из конфига: `opencode/kimi → codex → claude`.
- Телеметрия каждого решения и результата, экспорт в JSONL для анализа routing accuracy.

## Установка

Нужны Python 3.11+, `uv`, а также CLI тех исполнителей, которые включены в конфиге: `claude`, `codex`, `opencode`.

```bash
git clone https://github.com/tiptop32/agent-dispatch.git
cd agent-dispatch
uv sync
uv tool install .            # команда agent-dispatch появится в PATH
```

Ключ OpenRouter для Jev положите в `~/.config/agent-dispatch/env` (права 600):

```text
OPENROUTER_API_KEY=sk-or-v1-...
```

Ключи читаются только из этого файла или из окружения демона. В окружение дочерних CLI они не попадают.

Проверьте окружение:

```bash
agent-dispatch doctor            # config, ключ, демон, версии CLI
agent-dispatch doctor --online   # плюс живой вызов Jev
```

## Первый запуск

```bash
agent-dispatch route "fix failing test in tests/test_x.py"
# executor: codex
# confidence: 0.98
# router: jev

agent-dispatch dispatch --cwd ~/repo "tests/test_x.py fails; find the bug in src/x.py and fix it"
# task_id: ...
# status: completed
# executor: codex
# changed_files: src/x.py
# summary: ...

agent-dispatch dispatch --executor opencode/x5-code --cwd ~/repo "add a docstring to parse_args in cli.py"
agent-dispatch status <task_id>
agent-dispatch executors
```

Демон при первом `route`/`dispatch` стартует автоматически; лог в `~/.local/share/agent-dispatch/logs/serve.log`.

## Подключение к агентам

Один и тот же MCP-сервер подключается ко всем трём агентам. Переменная `AGENT_DISPATCH_SOURCE_AGENT` говорит демону, откуда пришла задача, чтобы не делегировать Codex обратно в Codex.

Claude Code:

```bash
claude mcp add agent-dispatch -e AGENT_DISPATCH_SOURCE_AGENT=claude -- agent-dispatch mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.agent-dispatch]
command = "agent-dispatch"
args = ["mcp"]
env = { AGENT_DISPATCH_SOURCE_AGENT = "codex" }
tool_timeout_sec = 120
```

OpenCode (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "agent-dispatch": {
      "type": "local",
      "command": ["agent-dispatch", "mcp"],
      "environment": { "AGENT_DISPATCH_SOURCE_AGENT": "opencode" }
    }
  }
}
```

Правило для агентов (добавьте в CLAUDE.md, AGENTS.md или системный промпт):

```text
When a task could benefit from another coding agent or model, use the AgentDispatch
`dispatch` tool instead of manually choosing an executor. Do not re-dispatch a task
that is already marked as delegated unless AgentDispatch explicitly allows escalation.
```

AgentDispatch не обязателен для каждого запроса: это общая возможность, а не прокси.

### Таймауты MCP

Задача исполнителя идёт минуты, а tool call в агенте живёт секунды. Поэтому прокси по умолчанию ждёт `mcp.wait_seconds` (50 с) и, если задача не закончилась, возвращает `running` и `task_id`; агент вызывает `status`. Чтобы ждать дольше, поднимите таймаут у хоста (`tool_timeout_sec` в Codex, `MCP_TOOL_TIMEOUT` в Claude Code) и передавайте `wait_seconds` в `dispatch`. Сервер в любом случае отдаёт ответ не позже чем через 600 с.

## Как принимается решение

1. Task Package: `task`, `cwd`, `context`, `files`, `constraints`, `success_criteria`. Режим `context_mode` (`prompt`, `prompt+summary`, `full`) задаёт, сколько контекста уходит исполнителю. История чата не передаётся никогда.
2. Кандидаты: включённые исполнители минус недоступные минус исполнитель того же типа, что и вызывающий агент (только на hop 0).
3. Один запрос к Jev с вопросами `executor` (choice по описаниям из конфига), `difficulty`, `task_type`, `risk`, `ambiguity`, `decomposable`. Ответ содержит вероятности и confidence.
4. Post-guards: `confidence < min_confidence` или отрыв от второго кандидата меньше `min_margin` → `fallback_executor`.
5. Запуск адаптера в `cwd`. `changed_files` считаются по `git status` до и после, а не со слов агента.
6. Результат нормализуется в `ExecutionResult`: `status` (`completed | partial | failed | needs_context | needs_escalation`), `summary`, `changed_files`, `tests`, `confidence`, `usage`.

Если Jev недоступен, работает `claude_local` (тот же выбор через `claude -p`), а если и он недоступен, `fallback_executor`.

### Сабагенты и hop-протокол

Исполнитель получает в окружении `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP`. Его собственный MCP-прокси читает их и передаёт в демон, поэтому глубина делегирования известна демону, а не модели. При `max_hops: 2` исполнитель может разбить задачу и отдать до `max_children` подзадач через тот же `dispatch` (каждую роутит Jev), а его сабагенты уже делегировать не могут.

## Конфигурация

`~/.config/agent-dispatch/config.yaml` мержится с [`agent_dispatch/config_default.yaml`](agent_dispatch/config_default.yaml) по ключам. Пример полного файла: [`config.example.yaml`](config.example.yaml).

Добавить новую модель OpenCode это только конфиг:

```yaml
executors:
  opencode/deepseek:
    adapter: opencode
    model: openrouter/deepseek-v3
    description: "OpenCode with DeepSeek: cheap, good for well-specified edits"
escalation:
  opencode/deepseek: [codex, claude]
```

`description` это и есть то, по чему Jev выбирает. Если routing accuracy падает, правьте описания, а не код.

Права исполнителей задаются `extra_args`. У `claude` по умолчанию `--permission-mode acceptEdits --allowedTools Bash,Edit,Write,Read,Glob,Grep`, иначе он не сможет запустить тесты в headless-режиме. Кодекс идёт с `--sandbox workspace-write`.

## Телеметрия

```bash
agent-dispatch export --since 7d > dataset.jsonl
agent-dispatch feedback <task_id> --outcome user_accepted
```

Одна строка JSONL на задачу: запрос, решение Jev со всеми вероятностями, результат, события guards и feedback. По этому датасету считаются routing accuracy, доля fallback и override, стоимость и латентность по исполнителям.

## Evals

Два платных прогона, см. [`evals/README.md`](evals/README.md): `routing` меряет accuracy на 20 размеченных задачах через живой демон (порог 0.8), `smoke` гоняет три реальные задачи на каждом исполнителе во временной репе.

## Разработка

```bash
uv sync
uv run pytest tests -q                 # gate-тесты, без сети и без настоящих CLI
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install              # ruff, detect-secrets, pytest перед каждым коммитом
```

Структура: `agent_dispatch/routing` (guards, Jev, claude_local, decision), `dispatch` (dispatcher, task package, escalation), `executors` (адаптеры, process runner, registry), `telemetry` (SQLite), `api` (FastAPI), `mcp` (прокси, автостарт), `cli.py`, `doctor.py`. Подробности в [`docs/architecture.md`](docs/architecture.md), план и история решений в [`docs/plans/`](docs/plans/).
