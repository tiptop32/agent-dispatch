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

Проверка `install` сравнивает установленную копию с исходниками и находит устаревшую
установку. После изменения кода запустите `uv tool install --reinstall <repo>`.

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

agent-dispatch dispatch --executor opencode/kimi --cwd ~/repo "add a docstring to parse_args in cli.py"
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
3. Один запрос к Jev. Несущий вопрос это `capability`: какого уровня работы требует задача (`fast`, `balanced`, `strong`). Имена моделей Jev не показываются. Рядом идут `judgment` (нужно ли разрешать компромиссы, а не просто исполнять), `corporate_data` (данные не должны покидать периметр; спрашивается, только если есть исполнитель с `corporate: true`), `difficulty`, `task_type`, `risk`, `ambiguity`, `decomposable`.
4. Исполнителя подбирает код: `corporate` сужает пул до внутреннего периметра, `judgment` отдаёт задачу рассуждающему агенту, дальше выбирается кандидат нужного `tier` (при отсутствии такого берётся ближайший вверх, затем вниз), при равенстве побеждает тот, кто идёт в конфиге выше.
5. Post-guards: `confidence >= autonomous_confidence` это `autonomous`, между ним и `min_confidence` это `advisory` (решение выполняется, но помечено как слабое), ниже `min_confidence` или отрыв меньше `min_margin` → `fallback_executor`.
6. Запуск адаптера в рабочем каталоге. `changed_files` считаются по `git status` до и после, а не со слов агента.
7. Результат нормализуется в `ExecutionResult`: `status` (`completed | partial | failed | needs_context | needs_escalation`), `summary`, `changed_files`, `tests`, `confidence`, `usage`.

Если Jev недоступен, работает `claude_local` (выбор исполнителя по описаниям через `claude -p`), а если и он недоступен, `fallback_executor`.

Почему не спрашивать у Jev имя модели: вендорские описания моделей неразличимы, и распределение получается плоским. На одних и тех же 20 размеченных задачах выбор по имени исполнителя давал среднюю уверенность 0.93 при 4 кандидатах и 0.35-0.60 при 10, а вопрос о capability даёт 0.975 и не зависит от числа моделей (проверено на 3 и на 11 кандидатах: 0.932 против 0.934). Добавление модели больше не ухудшает маршрутизацию.

### Рабочий каталог исполнителя

По умолчанию (`execution.workspace_mode: in_place`) исполнитель правит вашу рабочую копию, и задачи в один репозиторий выстраиваются в очередь по `cwd`.

В режиме `worktree` каждая задача получает свой `git worktree` от HEAD на ветке `<branch_prefix>/<task_id>`: параллельные сабагенты не затаптывают друг друга и не видят незакоммиченных правок вызывающего агента.

Отработав, исполнитель ничего не коммитит — это ему запрещено Task Package. Вместо него результат коммитит демон, на ветке задачи и без запуска хуков репозитория: служебный коммит на черновой ветке не должен зависеть от pre-commit, который гоняет тесты и падал бы на любой честно недоделанной задаче. Коммит делает работу долговечной — она переживает и неудачную интеграцию, и уборку каталога. SHA лежит в `result.meta.commit`.

Что происходит дальше, решает `integrate`:

| `integrate` | рабочая копия | worktree | ветка | `meta.patch` |
|---|---|---|---|---|
| `apply` (по умолчанию) | патч применяется `git apply --3way` | удаляется | удаляется | есть |
| `branch` | не трогается | удаляется | **остаётся** | есть |
| `manual` | не трогается | остаётся | остаётся | есть |

Если патч не лёг (вы правили те же строки), worktree и ветка остаются при любом режиме, `result.meta.integration_error` объясняет причину, а `result.meta.integrated` равно `false`.

```yaml
execution:
  workspace_mode: worktree
  integrate: apply      # branch: отдать результат веткой; manual: ничего не трогать
  keep_worktrees: false
  idle_timeout_seconds: 900  # остановить executor без вывода; 0 отключает
```

`execution.idle_timeout_seconds` завершает executor, если он не пишет в stdout/stderr указанное число секунд. Ноль отключает watchdog, но жёсткий `routing.default_timeout_seconds` всё равно действует.

```bash
agent-dispatch worktrees                      # деревья и ветки без дерева (от integrate: branch)
agent-dispatch worktrees --clean              # убрать деревья, ветки сохранить
agent-dispatch worktrees --clean --delete-branches
```

Настоящий гейт качества — ваш коммит после того, как вы прочитали диф: `changed_files` в результате берётся из `git status`, а не со слов исполнителя.

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
    tier: fast
    description: "OpenCode with DeepSeek: cheap, good for well-specified edits"
escalation:
  opencode/deepseek: [codex, claude]
```

`tier` (`fast`, `balanced`, `strong`) это то, по чему исполнитель находится: Jev называет нужный уровень, код выбирает кандидата этого уровня. Внутри одного `tier` побеждает тот, кто идёт в конфиге выше, поэтому дешёвые варианты ставьте первыми.

`escalation` поднимает задачу по цепочке, когда исполнитель не справился: результат `failed`, явный `needs_escalation`, красные тесты — или `partial`, в котором исполнитель не отдал блок отчёта **и** не изменил ни одного файла (`no_result`). Последний случай иначе был бы тупиком: `partial` цепочку не поднимал, и задача застревала, сколько её ни переспрашивай. Если файлы изменены, а отчёт не разобрался, эскалации нет: работа есть, и судить о ней по дифу должен вызывающий, а повторный запуск лёг бы поверх неё.

`corporate: true` помечает исполнителя внутри периметра. Если Jev отвечает, что задача касается корпоративных данных, выбор сужается до таких исполнителей; если ни одного нет, это попадает в `meta.selection_notes` решения, а не замалчивается. Туда же попадает случай, когда внутри периметра нет исполнителя нужного уровня: задача уедет к соседнему тиру, но молча это не произойдёт.

Вопрос о корпоративных данных это `noul`, и его уверенность (`|p - 0.5| * 2`) бывает около нуля — модель просто не знает. Такой ответ всё равно отсекал бы весь пул, поэтому есть порог:

```yaml
routing:
  corporate_min_confidence: 0.0   # 0 = доверять любому ответу
```

Ответ с уверенностью ниже порога периметр не сужает, и причина пишется в `selection_notes`. По умолчанию 0, то есть поведение прежнее: поднятие порога разрешает отправлять наружу задачи, которые Jev счёл корпоративными неуверенно, и это осознанное решение владельца данных, а не дефолт.

Периметр можно отключить целиком: `routing.corporate_perimeter: false`. Тогда вопрос о корпоративных данных Jev не задаётся вовсе, и исполнитель выбирается из всех включённых без сужения до `corporate: true`. Корпоративный код при этом может уехать за периметр, поэтому выключать его вправе только человек, по умолчанию стоит `true`.

`description` больше не влияет на выбор Jev: он используется резервным роутером `claude_local` и выводится в `agent-dispatch executors`.

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
