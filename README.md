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
- Tools: `route` (только решение), `dispatch` (решение + запуск), `dispatch_to` (явный исполнитель), `status`, `executors` (кто доступен сейчас, кто на лимите и до какого времени).
- Guards в коде: недоступный исполнитель, исчерпанный лимит (до объявленного сброса, на весь аккаунт), правило «только ревью» (`routing.review_only`), лимит глубины делегирования (`max_hops`), лимит сабагентов на задачу (`max_children`), низкая уверенность Jev, таймаут, отмена.
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
env_vars = ["AGENT_DISPATCH_HOP", "AGENT_DISPATCH_TASK_ID", "AGENT_DISPATCH_ROOT_AGENT"]
tool_timeout_sec = 120
```

`env_vars` обязателен: Codex не передаёт родительское окружение MCP-серверам по
умолчанию, и без него вложенный `dispatch` исполнителя приходит в демон с
`hop=0` и без родителя — исполнитель встаёт в очередь за собственной задачей и
теряет worktree. `agent-dispatch doctor` проверяет эту строку (проверка
`codex-env-vars`) и печатает её, если чего-то не хватает.

For daemon or GUI launches, install `scripts/codex-agent-lb` as
`~/.local/bin/codex`. It loads only `AGENT_LB_API_KEY` from
`~/.config/agent-lb/env` when the variable is missing, then runs the current
native Codex binary. Keep one `KEY=value` (or `export KEY=value`) entry per
line and protect the file. The launcher uses the first nonempty matching key,
returns exit `78` when none is available, and follows the unpinned
`standalone/current` native binary. Reinstalling Codex may replace a global
launcher, so configure the explicit launcher path in the executor command for
durable daemon behavior. Make the script executable with `chmod +x`.

Run the deterministic launcher eval with `uv run python -m evals.launcher`.

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

Ответы MCP по умолчанию компактные и не дублируют текст задачи и полный JSON. Параметр `verbose: true` возвращает полный JSON, а `status` с `wait_seconds` блокируется до 600 секунд вместо цикла опроса.

## Как принимается решение

1. Task Package: `task`, `cwd`, `context`, `files`, `constraints`, `success_criteria`. Режим `context_mode` (`prompt`, `prompt+summary`, `full`) задаёт, сколько контекста уходит исполнителю. История чата не передаётся никогда.
2. Кандидаты: включённые исполнители минус недоступные минус исполнитель того же типа, что и вызывающий агент (только на hop 0).
3. Один запрос к Jev. Несущий вопрос это `capability`: какого уровня работы требует задача (`fast`, `balanced`, `strong`). Имена моделей Jev не показываются. Рядом идут `judgment` (нужно ли разрешать компромиссы, а не просто исполнять), `corporate_data` (данные не должны покидать периметр; спрашивается, только если есть исполнитель с `corporate: true`), `difficulty`, `task_type`, `risk`, `ambiguity`, `decomposable`.
4. Исполнителя подбирает код: `corporate` сужает пул до внутреннего периметра, `judgment` отдаёт задачу рассуждающему агенту, дальше выбирается кандидат нужного `tier` (при отсутствии такого берётся ближайший вверх, затем вниз), при равенстве побеждает тот, кто идёт в конфиге выше.
5. Post-guards: `confidence >= autonomous_confidence` это `autonomous`, между ним и `min_confidence` это `advisory` (решение выполняется, но помечено как слабое), ниже `min_confidence` или отрыв меньше `min_margin` → `fallback_executor`. Это имя или упорядоченный список: берётся первый доступный кандидат из списка, остальные стоят в резерве; если недоступны все, остаётся лучший кандидат роутера с предупреждением.
6. Запуск адаптера в рабочем каталоге. `changed_files` считаются по `git status` до и после, а не со слов агента.
   Если вызывающий передал `verify` (команды проверки, обычно тесты затронутой области), демон запускает их сам после исполнителя. Упавшая команда значит «не сделано», какой бы `tests` ни написал исполнитель: результат `partial`, задача эскалируется (`verification_failed`). Подробнее в разделе «Проверка результата».
7. Результат нормализуется в `ExecutionResult`: `status` (`completed | partial | failed | needs_context | needs_escalation`), `summary`, `changed_files`, `tests`, `confidence`, `usage`.

Если Jev недоступен, работает `claude_local` (выбор исполнителя по описаниям через `claude -p`), а если и он недоступен, `fallback_executor`.

Почему не спрашивать у Jev имя модели: вендорские описания моделей неразличимы, и распределение получается плоским. На одних и тех же 20 размеченных задачах выбор по имени исполнителя давал среднюю уверенность 0.93 при 4 кандидатах и 0.35-0.60 при 10, а вопрос о capability даёт 0.975 и не зависит от числа моделей (проверено на 3 и на 11 кандидатах: 0.932 против 0.934). Добавление модели больше не ухудшает маршрутизацию.

### Рабочий каталог исполнителя

По умолчанию (`execution.workspace_mode: in_place`) исполнитель правит вашу рабочую копию, и coding-задачи в один репозиторий выстраиваются в очередь по `cwd`.

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

### Параллельные задачи

Корневые задачи (`hop == 0`) ограничены двумя независимыми лимитами. Coding-задачи (`kind: task`) занимают пул `server.max_concurrent_tasks` (по умолчанию 2), ревью (`kind: review`) — отдельный пул `server.max_concurrent_reviews` (по умолчанию 1). Максимум одновременно работающих корневых процессов — сумма этих лимитов, а не один общий пул: ревью не съедает coding-слот и наоборот. Вложенные задачи (`hop > 0`) от корневых семафоров не зависят, как и раньше, — правило hop-протокола не меняется.

Ревью остаётся режимом только для чтения и не берёт lock правки по `realpath(cwd)`: пока ревью идёт по репозиторию, coding-задача в него стартует как обычно, и наоборот.

### Проверка результата

Самоотчёт `tests: passed` пишет сама модель. Чтобы не верить ей на слово, передайте команды, которые демон выполнит сам:

```bash
agent-dispatch dispatch "Почини парсер дат" --verify "uv run pytest tests/test_dates.py -q" --verify "uv run ruff check ."
```

В MCP это аргумент `verify` у `dispatch` и `dispatch_to`. Команды пишутся для `cwd` запроса и идут через `/bin/sh -c` по порядку до первой упавшей, каждая не дольше `execution.verify_timeout_seconds` (600). Ответ показывает строку `verified: passed` или `verified: failed (команда: exit 1) хвост вывода`; если исполнитель при этом отчитался зелёными тестами, добавляется `executor reported tests passed`, а изменённые тестовые файлы перечисляются в `tests changed` — проверку можно «пройти», ослабив тесты, и смотреть на них стоит вам.

- Упавшая проверка поднимает цепочку эскалации, следующее звено получает те же команды. Без эскалации (`allow_escalation: false`) результат остаётся `partial`, работа на месте, решаете вы.
- В режиме worktree проверка идёт в дереве задачи; кэши и отчёты, которые она оставила, в патч не попадают. Работа, проверку не прошедшая и уходящая на эскалацию, в рабочую копию не переносится (`integration_held`).
- Ревью и упавшие запуски не проверяются.

### Продолжение задачи (follow-up)

Поправить или доделать готовую задачу дешевле, чем отправить её заново: исполнитель уже прочитал код.

```bash
agent-dispatch followup <task_id> "Ещё обработай пустую строку"
```

В MCP это инструмент `followup(task_id, message)`. Продолжается последнее звено цепочки эскалации тем же исполнителем и в той же рабочей копии; `verify` по умолчанию тот же, что у прошлой задачи. Если прошлый запуск шёл в этом же каталоге, исполнитель продолжает свою сессию CLI (`claude --resume`, `codex exec resume`, `opencode run --session`) и получает только новое сообщение. Иначе (прошлый запуск был в worktree, сессии нет, исполнитель остывает и задачу взял другой) он получает полный Task Package с итогом прошлой попытки: изменённые файлы, ошибка, упавшая проверка, хвост отчёта. Если продолжить сессию не вышло (другая версия CLI, сессия удалена), демон один раз запускает задачу с нуля, в ответе `resume_failed: True`.

Follow-up всегда идёт in_place. Если работа прошлой задачи не в рабочей копии (`integrate: branch`/`manual` или конфликт интеграции), демон отказывает и называет патч или ветку: сначала перенесите её.

### Сабагенты и hop-протокол

Исполнитель получает в окружении `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP`. Его собственный MCP-прокси читает их и передаёт в демон, поэтому глубина делегирования известна демону, а не модели. При `max_hops: 2` исполнитель может разбить задачу и отдать до `max_children` подзадач через тот же `dispatch` (каждую роутит Jev), а его сабагенты уже делегировать не могут. В Codex эти переменные должен пересылать сам MCP-сервер (`env_vars` в `[mcp_servers.agent-dispatch]`, см. выше) — это проверяет `agent-dispatch doctor`.

### Лимиты исполнителей

Когда CLI отвечает исчерпанным лимитом (`You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)`, `You’ve hit your usage limit. Try again at 5:02 PM.`), демон выводит исполнителя из ротации до названного момента плюс минута запаса. Время без часового пояса считается местным временем машины демона. Если момент не назван, исполнитель перепроверяется через `routing.quota_cooldown_seconds` (3600).

- **Весь аккаунт.** Лимит, авторизация и сеть общие для аккаунта, поэтому вместе с упавшим исполнителем остывают все с тем же `limit_group`: по умолчанию все `claude/*`, все `codex/*`, у `opencode` все модели одного провайдера. Перегрузка модели (`at capacity`) и лимит конкретной модели (`hit your Opus limit`) выводят только её.
- **Лимит расходов** (`monthly spend limit · raise it at …`) человек поднимает сам в любую минуту, поэтому он перепроверяется не реже `quota_cooldown_seconds`, даже если сброс назван через три дня.
- **Пул аккаунтов за балансировщиком** (codex через codex-lb): сброс в сообщении относится к одному аккаунту, следующий запрос может уйти на свободный. Для таких исполнителей `limit_reset: recheck`:

  ```yaml
  executors:
    codex/sol:
      adapter: codex
      model: gpt-5.6-sol
      limit_reset: recheck   # announced (по умолчанию): ждать названный сброс
  ```

- **Переживает перезапуск.** Остывание хранится в таблице `cooldowns` в `dispatch.db` и восстанавливается при старте демона.
- **Видно вызывающей модели.** MCP-инструмент `executors` отвечает в несколько строк:

  ```text
  available: codex/luna, codex/sol, opencode/x5-code-large
  out until 2026-10-03T05:01+05:00 (quota): claude/opus, claude/sonnet, claude/haiku; You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)
  review only from codex (kind='review'): claude/opus, claude/sonnet, claude/haiku
  ```

  Роутер такого исполнителя не предлагает, цепочка эскалации его пропускает, а `dispatch_to` к нему сразу получает отказ с временем окончания, не запуская CLI. В ответе на упавшую задачу есть строка `cooldown_until`.
- **Снять вручную**, если лимит подняли раньше: `agent-dispatch cooldown-clear claude/opus` (без имён снимает все). `agent-dispatch executors` показывает остывание в колонке ошибки.

### Только ревью

Задача бывает двух видов: `kind: task` (по умолчанию) меняет код, `kind: review` читает рабочую копию и возвращает замечания. Правило `routing.review_only` задаёт, кому источник может отдать только ревью:

```yaml
routing:
  review_only:
    codex: [claude]    # по умолчанию: Codex не тратит токены Claude на работу, а просит ревью
```

Для `source_agent: codex` и `kind: task` исполнители `claude/*` выпадают из кандидатов роутера и из цепочек эскалации, а `dispatch_to` к ним получает отказ `review_only` с подсказкой. С `kind: review` Codex может обратиться к Claude напрямую или через роутер. Выключить правило: `review_only: {codex: []}`.

Ревью идёт в режиме только чтения и всегда в самой рабочей копии, даже при `workspace_mode: worktree`: worktree от HEAD не увидел бы незакоммиченного диффа, который и просят проверить.

| адаптер | режим ревью |
|---|---|
| `claude` | `--tools Read,Glob,Grep --strict-mcp-config --permission-mode dontAsk`: других встроенных инструментов и MCP-серверов в сессии нет |
| `codex` | `--sandbox read-only` |
| `opencode` | `--agent plan` (встроенный агент без права правки) |

Почему у Claude `--tools`, а не `--allowedTools`/`--disallowedTools`: правила `allow` из `~/.claude/settings.json` складываются с флагами, и живая проверка 2026-10-03 с `--disallowedTools Write` создала файл. `--tools` убирает инструменты из сессии целиком. Bash ревьюеру не нужен: `git status --short` и `git diff HEAD` (до 100 000 символов) приходят в Task Package.

Конфликтующие флаги из `extra_args` (`--permission-mode`, `--allowedTools`, `--tools`, `--sandbox`, `--agent`) в режиме ревью убираются, а не перекрываются: codex повтор флага отвергает. OpenCode в ревью всегда получает `--agent plan`: включённые формы auto-approval (`--auto`, `--auto=true`) из `extra_args` убираются, явные отказы (`--no-auto`, `--auto=false`) сохраняются. Демон сравнивает содержимое рабочей копии до и после ревью; если оно изменилось, в результате `warning: review changed files: ...` или `warning: working copy changed during the review` (ревью идёт без lock, изменение мог внести и параллельный запуск). Правки не откатываются, решает вызывающий. Сверка содержимого не смотрит файлы из `.gitignore` (`.env`, `.venv`): хешировать их на каждое ревью дорого, от записи туда защищает режим CLI.

```bash
agent-dispatch dispatch --kind review "Проверь незакоммиченный дифф на ошибки"
```

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

`escalation` поднимает задачу по цепочке, когда исполнитель не справился: результат `failed`, явный `needs_escalation`, упавшая проверка `verify`, красные тесты в отчёте — или `partial`, в котором исполнитель не отдал блок отчёта **и** не изменил ни одного файла (`no_result`). Последний случай иначе был бы тупиком: `partial` цепочку не поднимал, и задача застревала, сколько её ни переспрашивай. Если файлы изменены, а отчёт не разобрался, эскалации нет: работа есть, и судить о ней по дифу должен вызывающий, а повторный запуск лёг бы поверх неё.

Потолки расходов по умолчанию выключены:

```yaml
routing:
  max_chain_cost_usd: 3.0      # цепочка эскалации вместе с подзадачами её звеньев
  daily_cost_limit_usd: 20.0   # всё, что завершилось с местной полуночи, плюс решения роутера
```

Дошла цепочка до `max_chain_cost_usd` — следующее звено не запускается, в ответе `budget: {...}`. Дошли сутки до `daily_cost_limit_usd` — новые задачи, подзадачи и звенья эскалации получают отказ `budget` ещё до роутера. Считается только цена, которую сообщает CLI: claude (`total_cost_usd`) и opencode; codex цену не сообщает, его запуски в сумму не входят.

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

OpenCode в headless-режиме `run` прерывает сессию, когда агент отклоняет запрос разрешения, — адаптер возвращает `failed` с `opencode permission rejected: ...`. Поэтому для неинтерактивных задач адаптер по умолчанию добавляет флаг `--auto` (auto-approve permissions that are not explicitly denied, помечен как dangerous): он подходит для доверенных задач без присмотра. Явный отказ приоритетнее дефолта: если в `extra_args` уже есть `--no-auto` или `--auto=false` (`--auto false`), флаг не добавляется, и каждое разрешение снова будет спрашивать человек или агент.

Правила валидации — кто и когда в задаче гоняет тесты и линт — собраны в [`docs/workflow.md`](docs/workflow.md): один владелец валидации на задачу, неизменённый код второй раз зелёным не прогоняется; обязательные pre-commit гейты (ruff, detect-secrets, pytest) сохраняются.

## Телеметрия

```bash
agent-dispatch export --since 7d > dataset.jsonl
agent-dispatch feedback <task_id> --outcome user_accepted
```

Одна строка JSONL на задачу: запрос, решение Jev со всеми вероятностями, результат, события guards и feedback. По этому датасету считаются routing accuracy, доля fallback и override, стоимость и латентность по исполнителям.

## Evals

Два платных прогона, см. [`evals/README.md`](evals/README.md): `routing` меряет accuracy на 20 размеченных задачах через живой демон (порог 0.8), `smoke` гоняет три реальные задачи на каждом исполнителе во временной репе. Рядом живут бесплатные детерминированные сьюты: `evals.executors` (argv и парсинг результатов OpenCode-адаптера на корпусе), `evals.scheduling` (инварианты ёмкости задач и ревью in-process) и `evals.workflow` (контракты в отрендеренном TaskPackage).

## Разработка

```bash
uv sync
uv run pytest tests -q                 # gate-тесты, без сети и без настоящих CLI
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install              # ruff, detect-secrets, pytest перед каждым коммитом
```

Структура: `agent_dispatch/routing` (guards, Jev, claude_local, decision), `dispatch` (dispatcher, task package, escalation), `executors` (адаптеры, process runner, registry), `telemetry` (SQLite), `api` (FastAPI), `mcp` (прокси, автостарт), `cli.py`, `doctor.py`. Подробности в [`docs/architecture.md`](docs/architecture.md), план и история решений в [`docs/plans/`](docs/plans/).
