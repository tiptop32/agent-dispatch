# AgentDispatch

Маршрутизация по умолчанию детерминированно выбирает исполнителя уровня `balanced`; Jev и `claude_local` остаются опциональными роутерами.

```text
Claude Code ─┐
omp ──────────┘──→ MCP `dispatch` ──→ демон agent-dispatch ──→ static capability selector ──→ исполнитель
                                                                      claude | omp/<model>
```


## Что внутри

- `agent-dispatch serve`: локальный демон (HTTP на `127.0.0.1:7433`, SQLite-телеметрия, очередь задач). Переживает смерть агента, который его вызвал.
- `agent-dispatch mcp`: stdio MCP-сервер, тонкий прокси в демон. Поднимает демон сам, если тот не запущен.
- Tools: `route` (только решение), `dispatch` (решение + запуск), `dispatch_to` (явный исполнитель), `status`, `executors` (кто доступен сейчас, кто на лимите и до какого времени).
- `route` и `dispatch` используют `balanced` без дополнительного LLM-вызова. Параметр `capability` позволяет явно выбрать `fast`, `balanced` или `strong`; `max_capability` остаётся верхним потолком и не меняет прежнюю семантику.
- Jev и `claude_local` остаются opt-in. Jev получает task state только для cwd под `router.external.trusted_roots`; неизвестные проекты закрыты fail-closed, `prod: true` всегда пропускает роутеры.
- Guards в коде: недоступный исполнитель, исчерпанный лимит (до объявленного сброса, на весь аккаунт), правило «только ревью» (`routing.review_only`), лимит глубины делегирования (`max_hops`), лимит сабагентов на задачу (`max_children`), низкая уверенность Jev, таймаут, отмена.
- Escalation chains из конфига: `omp/luna → omp/terra → omp/sol → claude`, `omp/x5-small → omp/x5-code-large → omp/x5-large`, `omp/x5-code-large → omp/x5-large`.
- Временный transport retry ограничен числом попыток и временем, разрешён только до первого ответа и при неизменённой рабочей копии; после исчерпания не происходит escalation только из-за transport ошибки.
- Телеметрия каждого решения и результата, экспорт в JSONL для анализа routing accuracy и outcome-aware метрик.

## Установка

Нужны Python 3.11+, `uv`, а также CLI исполнителей из конфига: `claude`, `omp`.

```bash
git clone https://github.com/tiptop32/agent-dispatch.git
cd agent-dispatch
uv sync
uv tool install .            # команда agent-dispatch появится в PATH
```

Ключ OpenRouter нужен только при явном `router.backend: jev`; для этого backend
задачи отправляются наружу лишь если `router.external.trusted_roots` содержит
реальный путь cwd (или явно включён `allow_unknown_projects`). По умолчанию
неизвестные проекты блокируются до сериализации task state.

```text
OPENROUTER_API_KEY=sk-or-v1-...
```

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
# executor: omp/sol
# confidence: 0.98
# router: jev

agent-dispatch dispatch --cwd ~/repo "tests/test_x.py fails; find the bug in src/x.py and fix it"
# task_id: ...
# status: completed
# executor: omp/sol
# changed_files: src/x.py
# summary: ...

agent-dispatch dispatch --executor omp/x5-code-large --cwd ~/repo "add a docstring to parse_args in cli.py"
agent-dispatch status <task_id>
agent-dispatch executors
```

Демон при первом `route`/`dispatch` стартует автоматически; лог в `~/.local/share/agent-dispatch/logs/serve.log`.

## Подключение к агентам

Один и тот же MCP-сервер подключается к Claude Code и omp. Переменная `AGENT_DISPATCH_SOURCE_AGENT` говорит демону, откуда пришла задача, чтобы не делегировать omp обратно в omp.

Claude Code:

```bash
claude mcp add agent-dispatch -e AGENT_DISPATCH_SOURCE_AGENT=claude -- agent-dispatch mcp
```

omp (`~/.omp/agent/mcp.json`):

```json
{
  "mcpServers": {
    "agent-dispatch": {
      "command": ["agent-dispatch", "mcp"],
      "env": {
        "AGENT_DISPATCH_SOURCE_AGENT": "omp",
        "AGENT_DISPATCH_HOP": "AGENT_DISPATCH_HOP",
        "AGENT_DISPATCH_TASK_ID": "AGENT_DISPATCH_TASK_ID",
        "AGENT_DISPATCH_ROOT_AGENT": "AGENT_DISPATCH_ROOT_AGENT"
      }
    }
  }
}
```

`omp` передаёт переменные `AGENT_DISPATCH_*` MCP-серверу, поэтому вложенный `dispatch` сохраняет hop, родителя и worktree. Проверка `agent-dispatch doctor` называется `omp-mcp-env`.

Ключ балансера omp получает сам через `apiKey: "!…"`; команда `omp-setup/bin/lb-key` читает его из `~/.config/agent-lb/env`. Диспетчер вырезает `AGENT_LB_API_KEY` из env omp, потому что окружение запускающего процесса доходит до bash-агента.

Конфиг omp поддерживается в `~/my_git_reps/omp-setup`. После перехода старый пользовательский конфиг переносится командой:

```bash
agent-dispatch migrate-config
```

Для корпоративных исполнителей используйте профиль `x5`: он ограничивает провайдеры и роли omp корпоративным периметром. Корпоративный RootCA bun читает из системного Keychain при `NODE_USE_SYSTEM_CA=1`.

Правило для агентов (добавьте в CLAUDE.md, AGENTS.md или системный промпт):

```text
When a task could benefit from another coding agent or model, use the AgentDispatch
`dispatch` tool instead of manually choosing an executor. Do not re-dispatch a task
that is already marked as delegated unless AgentDispatch explicitly allows escalation.
```

Правило для агентов (добавьте в CLAUDE.md, AGENTS.md или системный промпт):

```text
When a task could benefit from another coding agent or model, use the AgentDispatch
`dispatch` tool instead of manually choosing an executor. Do not re-dispatch a task
that is already marked as delegated unless AgentDispatch explicitly allows escalation.
```

AgentDispatch не обязателен для каждого запроса: это общая возможность, а не прокси.

### Таймауты MCP

Задача исполнителя идёт минуты, а tool call в агенте живёт секунды. Поэтому прокси по умолчанию ждёт `mcp.wait_seconds` (50 с) и, если задача не закончилась, возвращает `running` и `task_id`; агент вызывает `status`. Чтобы ждать дольше, поднимите таймаут у хоста omp или Claude Code и передавайте `wait_seconds` в `dispatch`. Сервер в любом случае отдаёт ответ не позже чем через 600 с.

Ответы MCP по умолчанию компактные и не дублируют текст задачи и полный JSON. Параметр `verbose: true` возвращает полный JSON, а `status` с `wait_seconds` блокируется до 600 секунд вместо цикла опроса.

В компактном ответе `summary` задачи `kind: task` ограничен 800 символами, `kind: review` — 8000: список находок можно прочитать без `verbose`. При обрезке добавляется ` …`.

Размеры ответов `dispatch`, `dispatch_to`, `status`, `followup` и `route` записываются в `<data_dir>/mcp_responses.jsonl`: время, имя инструмента, `task_id` (у `route` — `null`), число байтов текста ответа в UTF-8 и признак `verbose`. Текст задачи и ответа туда не пишется; ошибка записи не мешает ответу. `evals.replay` связывает записи с корневыми задачами и показывает `caller_bytes`, а также счётчики `watchdog`, `handoff`, `fix_round` и `review_stages`. Недельные критерии приёмки плана проверяются после релиза.

## Как принимается решение

1. Task Package: `task`, `cwd`, `context`, `files`, `constraints`, `success_criteria`; история чата не передаётся.
2. Кандидаты: включённые исполнители минус недоступные, source-agent и review-only ограничения.
3. Static router выбирает capability `balanced` через `routing.capability.select`, учитывая доступность, квоты, корпоративный периметр и `max_capability`; явный `capability` меняет baseline, не меняя смысл потолка `max_capability`.
4. Перед внешним Jev/OpenRouter вызовом локальный preflight требует realpath cwd внутри `router.external.trusted_roots`; неизвестные проекты блокируются по умолчанию. `prod` всегда блокирует внешние роутеры.
5. Post-guards применяют confidence/margin и `fallback_executor`.
6. `routing.explore_share` опционально сравнивает Jev со статическим balanced arm.
7. Запускается выбранный адаптер в рабочем каталоге; `changed_files` считаются по снимкам git.
8. Результат нормализуется в `ExecutionResult`.

При явном `router.backend: jev` сначала вызывается Jev, затем при отказе `claude_local`, затем `fallback_executor`.

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
  idle_timeout_seconds: 300
  watchdog_interval_seconds: 300
  watchdog_tool_idle_seconds: 900
  watchdog_repeat_limit: 6
  review_fix_rounds: 1
```

Сторож omp проверяет поток событий каждые 300 с: без открытого инструмента предел молчания 300 с, с открытым — 900 с. Шесть одинаковых подряд комбинаций инструмента, аргументов и результата означают петлю. Причина (`idle` или `looping`) записывается в `meta.watchdog`. Ноль `idle_timeout_seconds` отключает предел молчания без открытого инструмента, но не остальные проверки. Жёсткий таймаут остаётся. У нестримингового Claude сторож не работает.

При таймауте или остановке сторожем с изменёнными файлами работа передаётся только допустимому следующему звену эскалации. В worktree оно получает патч непосредственного предка, а in_place продолжает работу на месте. Если звена нет из-за настроек, периметра или бюджета, прежний результат сохраняется; при ошибке применения патча наследник стартует с HEAD и сообщает об этом в `meta.handoff`.

```bash
agent-dispatch worktrees                      # деревья и ветки без дерева (от integrate: branch)
agent-dispatch worktrees --clean              # убрать деревья, ветки сохранить
agent-dispatch worktrees --clean --delete-branches
agent-dispatch worktrees --all                # деревья всех репозиториев, возраст и статус задачи
agent-dispatch worktrees --all --clean        # убрать только безопасные завершённые деревья
```
Из домашнего каталога или другой не-git директории используйте `agent-dispatch worktrees --all --clean`; обычный режим требует `--cwd` внутри конкретного репозитория и подсказывает этот вариант при ошибке.
`--all` обходит `execution.worktree_dir` или `<data_dir>/worktrees` и группирует
деревья по общей git-директории. Очистка не трогает задачи без финального
статуса и деревья, внутри которых находится `cwd` другой незавершённой задачи.
Если исходный репозиторий удалён, `--all --clean` удаляет оставшийся каталог
дерева напрямую и сообщает об этом. `agent-dispatch doctor` показывает число
деревьев, общий размер и число деревьев старше семи дней; старые помечаются
как `WARN` и не меняют код выхода.

Настоящий гейт качества — ваш коммит после того, как вы прочитали диф: `changed_files` в результате берётся из `git status`, а не со слов исполнителя.

`meta.diff_stat` показывает число файлов и добавленных/удалённых строк фактической дельты задачи, не включая исходные незакоммиченные изменения вызывающего. Бинарные файлы считаются только в числе файлов.

### Параллельные задачи

Корневая coding-задача (`hop == 0`, `kind: task`) сначала занимает слот своей `limit_group` (`server.max_concurrent_per_group`, по умолчанию 2), затем общий слот (`server.max_concurrent_tasks`, по умолчанию 4). Поэтому два долгих omp не мешают Claude использовать собственную квоту, но одновременно работают не больше четырёх корневых задач на правку. Порядок group → global не даёт ожидающей группе держать общий слот.

Ревью (`kind: review`) сохраняет отдельный пул `server.max_concurrent_reviews` (по умолчанию 1). При дефолтах максимум одновременно работающих корневых CLI-процессов равен 5: четыре задачи плюс одно ревью. Вложенные задачи (`hop > 0`) от корневых семафоров не зависят, как и раньше, — правило hop-протокола не меняется. Получение task-слотов пишет событие `slot` с `limit_group` и `wait_seconds`; replay показывает по этим событиям число ожиданий дольше 60 секунд, среднее и максимум каждой группы. Старые задачи без события в расчёт не входят.

Для отдельной группы `limit_groups.<группа>.max_concurrent` заменяет общий лимит группы. `executors.<имя>.max_concurrent` ограничивает запуск CLI этого исполнителя, в том числе ревью и вложенных задач; `queue_fallback` учитывает занятые слоты. Слот освобождается до ожидания ревью и между раундами. Вложенная задача того же исполнителя использует слот предка, чтобы не заблокировать его.

Ревью остаётся режимом только для чтения и не берёт lock правки по корню рабочей копии (`git rev-parse --show-toplevel`): пока ревью идёт по репозиторию, coding-задача в него стартует как обычно, и наоборот.

### Проверка результата

Самоотчёт `tests: passed` пишет сама модель. Чтобы не верить ей на слово, передайте команды, которые демон выполнит сам:

```bash
agent-dispatch dispatch "Почини парсер дат" --verify "uv run pytest tests/test_dates.py -q" --verify "uv run ruff check ."
```

В MCP это аргумент `verify` у `dispatch` и `dispatch_to`. Команды пишутся для `cwd` запроса и идут через `/bin/sh -c` по порядку до первой упавшей, каждая не дольше `execution.verify_timeout_seconds` (600). Ответ показывает строку `verified: passed` или `verified: failed (команда: exit 1) хвост вывода`; если исполнитель при этом отчитался зелёными тестами, добавляется `executor reported tests passed`, а изменённые тестовые файлы перечисляются в `tests changed` — проверку можно «пройти», ослабив тесты, и смотреть на них стоит вам.

- Упавшая проверка запускает ограниченный раунд исправления, если он доступен по бюджету: тот же исполнитель продолжает сессию в том же каталоге, после чего демон повторяет verify и, при провале ревью, только проваленную ступень. Общий для звена `execution.review_fix_rounds` по умолчанию равен 1; ноль отключает раунд. Расходы суммируются. После исчерпания раундов возможна эскалация с теми же командами; без неё результат остаётся `partial`.
- В режиме worktree проверка идёт в дереве задачи; кэши и отчёты, которые она оставила, в патч не попадают. Работа, проверку не прошедшая и уходящая на эскалацию, в рабочую копию не переносится (`integration_held`).
- Ревью и упавшие запуски не проверяются.

`review_by` и `perimeter_review_by` у исполнителя принимают имя ревьюера или упорядоченный список. Ступени проходят по порядку: `pass`, `skipped` и `unknown` переходят дальше, `fail` останавливает цепочку и может запустить раунд исправления. Ревью внутри периметра использует только корпоративных ревьюеров. Внутренние задачи авторевью не занимают `max_children`. Ревьюер получает `files` и `constraints` исходной задачи. Находки, исправление которых требует выйти за разрешённый список файлов или нарушить ограничения, идут отдельным блоком «follow-up для вызывающего» в summary ревью и сами по себе не дают `verdict: fail`. Без `files` и `constraints` правило не добавляется. Полный summary хранится у задачи ревью; у родителя в `meta.review` и `meta.review_stages` сохраняется до 2000 символов, а компактная строка `review: fail` показывает до 1500.

### Продолжение задачи (follow-up)

Поправить или доделать готовую задачу дешевле, чем отправить её заново: исполнитель уже прочитал код.

```bash
agent-dispatch followup <task_id> "Ещё обработай пустую строку"
```

В MCP это инструмент `followup(task_id, message)`. Продолжается последнее звено цепочки эскалации тем же исполнителем и в той же рабочей копии; `verify` по умолчанию тот же, что у прошлой задачи. Если прошлый запуск шёл в этом же каталоге, исполнитель продолжает сессию omp через `--resume <session id>` из первой строки `{"type":"session"}` в том же `cwd`, `--session-dir` и профиле. Иначе он получает полный Task Package с итогом прошлой попытки. Если сессию продолжить нельзя, демон один раз запускает задачу с нуля.

Follow-up всегда идёт in_place. Если работа прошлой задачи не в рабочей копии (`integrate: branch`/`manual` или конфликт интеграции), демон отказывает и называет патч или ветку: сначала перенесите её.

### Сабагенты и hop-протокол

Исполнитель получает в окружении `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP`. Его собственный MCP-прокси читает их и передаёт в демон, поэтому глубина делегирования известна демону, а не модели. При `max_hops: 2` исполнитель может разбить задачу и отдать до `max_children` подзадач через тот же `dispatch` (каждую роутит Jev), а его сабагенты уже делегировать не могут. Если лимит уже занят, отказ перечисляет `task_id` работающих соседних задач и предлагает вызвать `status(task_id, wait_seconds)`, а не повторять `dispatch`. Компактный MCP-ответ сохраняет эту подсказку. MCP-прокси omp передаёт эти переменные в демон; `agent-dispatch doctor` проверяет их в `omp-mcp-env`. Роли `task`, `prewalk` и `advisor` профиля x5 идут через корпоративные модели.

### Лимиты исполнителей

Когда CLI отвечает исчерпанным лимитом (`You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)`, `You’ve hit your usage limit. Try again at 5:02 PM.`), демон выводит исполнителя из ротации до названного момента плюс минута запаса. Время без часового пояса считается местным временем машины демона. Если момент не назван, исполнитель перепроверяется через `routing.quota_cooldown_seconds` (3600).

- **Весь аккаунт.** Лимит, авторизация и сеть общие для аккаунта, поэтому вместе с упавшим исполнителем остывают все с тем же `limit_group`: по умолчанию все `claude/*`, а у `omp/*` — все исполнители одного провайдера (`agent-lb` или `omp:copilot-x5`). Перегрузка модели (`at capacity`) и лимит конкретной модели (`hit your Opus limit`) выводят только её.
- **Занятая модель.** `ConcurrencyLimitModel` и `Parallel processed requests limit exceed` дают `model_busy`: остывает только этот исполнитель на `routing.busy_cooldown_seconds` (120 с), а не вся группа.
- **Лимит расходов** (`monthly spend limit · raise it at …`) человек поднимает сам в любую минуту, поэтому он перепроверяется не реже `quota_cooldown_seconds`, даже если сброс назван через три дня.
- **Пул аккаунтов за балансировщиком.** Для `omp/*` через agent-lb сброс относится к одному аккаунту, следующий запрос может уйти на свободный. Поэтому используется `limit_reset: recheck`.

  ```yaml
  executors:
    omp/sol:
      adapter: omp
      model: agent-lb/gpt-5.6-sol
      limit_reset: recheck   # announced (по умолчанию): ждать названный сброс
  ```

- **Переживает перезапуск.** Остывание хранится в таблице `cooldowns` в `dispatch.db` и восстанавливается при старте демона.
- **Видно вызывающей модели.** MCP-инструмент `executors` отвечает в несколько строк:

  ```text
  available: omp/luna, omp/sol, omp/x5-code-large
  out until 2026-10-03T05:01+05:00 (quota): claude/opus, claude/sonnet, claude/haiku; You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)
  review only from omp (kind='review'): claude/opus, claude/sonnet, claude/haiku
  ```

  Роутер такого исполнителя не предлагает, цепочка эскалации его пропускает, а `dispatch_to` к нему сразу получает отказ с временем окончания, не запуская CLI. В ответе на упавшую задачу есть строка `cooldown_until`.
- **Снять вручную**, если лимит подняли раньше: `agent-dispatch cooldown-clear claude/opus` (без имён снимает все). `agent-dispatch executors` показывает остывание в колонке ошибки.

### Только ревью

Задача бывает двух видов: `kind: task` (по умолчанию) меняет код, `kind: review` читает рабочую копию и возвращает замечания. Правило `routing.review_only` задаёт, кому источник может отдать только ревью:

```yaml
routing:
  review_only:
    omp: [claude]    # omp получает ревью от Claude, но не отдаёт Claude coding-задачи
```

Для `source_agent: omp` и `kind: task` исполнители `claude/*` выпадают из кандидатов роутера и из цепочек эскалации, а `dispatch_to` к ним получает отказ `review_only` с подсказкой. С `kind: review` omp может обратиться к Claude напрямую или через роутер. Выключить правило: `review_only: {omp: []}`.

Ревью идёт в режиме только чтения и всегда в самой рабочей копии, даже при `workspace_mode: worktree`: worktree от HEAD не увидел бы незакоммиченного диффа, который и просят проверить.

| адаптер | режим ревью |
|---|---|
| `claude` | `--tools Read,Glob,Grep --strict-mcp-config --permission-mode dontAsk`: других встроенных инструментов и MCP-серверов в сессии нет |
| `omp` | `--tools read,grep,glob` (только чтение; инструмента `find` нет) |

Почему у Claude `--tools`, а не `--allowedTools`/`--disallowedTools`: правила `allow` из `~/.claude/settings.json` складываются с флагами, и живая проверка 2026-10-03 с `--disallowedTools Write` создала файл. `--tools` убирает инструменты из сессии целиком. Bash ревьюеру не нужен: `git status --short` и `git diff HEAD` (до 100 000 символов) приходят в Task Package.

Конфликтующие флаги ревью из `extra_args` убираются, чтобы read-only режим omp не был ослаблен. Демон сравнивает содержимое рабочей копии до и после ревью; если оно изменилось, в результате `warning: review changed files: ...` или `warning: working copy changed during the review` (ревью идёт без lock, изменение мог внести и параллельный запуск). Правки не откатываются, решает вызывающий. Сверка содержимого не смотрит файлы из `.gitignore` (`.env`, `.venv`): хешировать их на каждое ревью дорого, от записи туда защищает режим CLI.

```bash
agent-dispatch dispatch --kind review "Проверь незакоммиченный дифф на ошибки"
```

## Конфигурация

`~/.config/agent-dispatch/config.yaml` мержится с [`agent_dispatch/config_default.yaml`](agent_dispatch/config_default.yaml) по ключам. Порядок исполнителей внутри тира задаёт пользовательский файл, а не `config_default.yaml`: `load_settings` сохраняет этот порядок. Старый конфиг с adapter codex/opencode не загружается и сообщает о необходимости `agent-dispatch migrate-config`. Пример полного файла: [`config.example.yaml`](config.example.yaml).
По умолчанию `router.backend: static`: внешний роутер не вызывается. Для Jev задайте явный trusted root:

```yaml
router:
  backend: jev
  external:
    trusted_roots: [~/src]
    allow_unknown_projects: false
```

Перед отправкой task state обе стороны realpath-проверяются на containment; симлинк cwd наружу не проходит. `prod: true` всегда блокирует внешние роутеры.

Добавить новую модель omp это только конфиг:

```yaml
executors:
  omp/deepseek:
    adapter: omp
    model: openrouter/deepseek-v3
    tier: fast
    description: "omp with DeepSeek: cheap, good for well-specified edits"
escalation:
  omp/deepseek: [omp/sol, claude]
```

`tier` (`fast`, `balanced`, `strong`) это то, по чему исполнитель находится: Jev называет нужный уровень, код выбирает кандидата этого уровня. Внутри одного `tier` побеждает тот, кто идёт в конфиге выше, поэтому дешёвые варианты ставьте первыми.

Для несмещённого сравнения Jev со статическим правилом включите эксперимент:

```yaml
routing:
  explore_share: 0.1
```

Рекомендуемое значение `0.1` отправляет 10% только пригодных задач статическому исполнителю тира `balanced`. Jev всё равно вызывается, а его выбор записывается рядом. Override (`dispatch_to`), эскалации, follow-up, review, `low_confidence`, `low_margin`, `router_unavailable` и решения, остановленные guards, в эксперимент не входят. При `0.0` поведение не меняется.

`escalation` поднимает задачу по цепочке, когда исполнитель не справился: результат `failed`, явный `needs_escalation`, упавшая проверка `verify`, красные тесты в отчёте — или `partial`, в котором исполнитель не отдал блок отчёта **и** не изменил ни одного файла (`no_result`). Последний случай иначе был бы тупиком: `partial` цепочку не поднимал, и задача застревала, сколько её ни переспрашивай. Если файлы изменены, а отчёт не разобрался, эскалации нет: работа есть, и судить о ней по дифу должен вызывающий, а повторный запуск лёг бы поверх неё. Перед этим демон один раз продолжает ту же сессию коротким «дай отчёт» (`execution.report_recovery_timeout_seconds`, 0 выключает): удачный отчёт превращает `partial` в настоящий статус исполнителя, неудачный оставляет `partial` с `meta.report_recovery: failed: ...`.

Потолки расходов по умолчанию выключены:

```yaml
routing:
  max_chain_cost_usd: 3.0      # цепочка эскалации вместе с подзадачами её звеньев
  daily_cost_limit_usd: 20.0   # всё, что завершилось с местной полуночи, плюс решения роутера
```

Дошла цепочка до `max_chain_cost_usd` — следующее звено не запускается, в ответе `budget: {...}`. Дошли сутки до `daily_cost_limit_usd` — новые задачи, подзадачи и звенья эскалации получают отказ `budget` ещё до роутера. Считаются `usage` и `cost_usd` всех ассистентских `message_end`, включая все ходы omp.

`corporate: true` помечает исполнителя внутри периметра. Если Jev отвечает, что задача касается корпоративных данных, выбор сужается до таких исполнителей; если ни одного нет, это попадает в `meta.selection_notes` решения, а не замалчивается. Туда же попадает случай, когда внутри периметра нет исполнителя нужного уровня: задача уедет к соседнему тиру, но молча это не произойдёт.

Fallback и эскалация периметра не покидают. Пока периметр применим к решению, слабое решение (`low_confidence`, `low_margin`) и путь «все роутеры упали» выбирают замену только среди `corporate: true`; если в `fallback_chain` исполнителя периметра нет, задача остаётся у представителя периметра с предупреждением. Звенья эскалации, переадресация роутеру после сбоя исполнителя и follow-up наследуют требование предка (флаг в `decision.meta.perimeter` читается из базы по `escalated_from`, `followup_of` и `parent_task_id` и переживает рестарт демона): внешние звенья цепочки пропускаются, а роутер на переадресации и follow-up выбирает только из `corporate: true`, даже если Jev теперь ответил «не корпоративное». Когда корпоративных звеньев не осталось и переадресации нет, цепочка останавливается, и в `result.meta` появляется `escalation_stopped: perimeter`. Если задаче под периметром всё же достался внешний исполнитель (все корпоративные остывают, вложенный dispatch назвал внешний `executor`), она получает отказ `guard: perimeter` до запуска. Явный `executor` корневой задачи остаётся решением владельца данных и требования периметра не ставит: его эскалация и вложенные задачи могут уйти наружу. Бэкенд `claude_local` вопрос о корп. данных не задаёт, поэтому с ним задачи при конфиге с `corporate: true` всегда идут внутрь периметра. Запуски вне требуемого периметра по живой телеметрии считает `uv run python -m evals.replay` (строка `runs outside a required corporate perimeter`, порог 0). Fallback при этом уважает `max_capability`: исполнитель цепочки берётся не выше потолка, а если таких нет, остаётся выбор роутера с заметкой; периметр и здесь сильнее потолка.

Ответ о корпоративных данных нужен всегда, пока периметр включён и в конфиге есть включённый `corporate: true` (по конфигу, а не по доступным сейчас кандидатам: остывание корпоративных исполнителей периметр не открывает). Если ответа нет (роутеры упали, `claude_local` вопроса не задаёт), это «неизвестно»: пул сужается до периметра — fail closed, исход пишется в `decision.meta.perimeter` (`answered` — по ответу Jev, `unknown` — без него). Неполный ответ — ошибка роутера: `noul` без конечной вероятности из [0, 1] (`invalid noul answer`), ответ `corporate_data` не типа `noul` и незаданный вопрос `corporate_data` (`missing answer: corporate_data`) дают `RouterError`, дальше работает обычная fallback-цепочка роутеров, и периметр всё равно применяется.

Вопрос о корпоративных данных это `noul`, и его уверенность (`|p - 0.5| * 2`) бывает около нуля — модель просто не знает. Такой ответ всё равно отсекал бы весь пул, поэтому есть порог:

```yaml
routing:
  corporate_min_confidence: 0.0   # 0 = доверять любому ответу
```

Ответ с уверенностью ниже порога периметр не сужает, и причина пишется в `selection_notes`. По умолчанию 0, то есть поведение прежнее: поднятие порога разрешает отправлять наружу задачи, которые Jev счёл корпоративными неуверенно, и это осознанное решение владельца данных, а не дефолт.

Периметр можно отключить целиком: `routing.corporate_perimeter: false`. Тогда вопрос о корпоративных данных Jev не задаётся вовсе, и исполнитель выбирается из всех включённых без сужения до `corporate: true`. Корпоративный код при этом может уехать за периметр, поэтому выключать его вправе только человек, по умолчанию стоит `true`.

`description` больше не влияет на выбор Jev: он используется резервным роутером `claude_local` и выводится в `agent-dispatch executors`.

Права исполнителей задаются `extra_args`. У `claude` по умолчанию `--permission-mode acceptEdits --allowedTools Bash,Edit,Write,Read,Glob,Grep`, иначе он не сможет запустить тесты в headless-режиме. omp получает `--approval-mode yolo` для coding-задач и `--tools read,grep,glob` для ревью.

omp получает разрешения через `--approval-mode yolo`; отклонённое разрешение адаптер сообщает как `failed`, а не как `partial`.

Корпоративные исполнители omp запускаются с `--profile x5` и `NODE_USE_SYSTEM_CA=1` (корпоративный RootCA читается из Keychain): профиль x5 держит периметр и для сабагентов omp. Каждый запуск получает `--config ~/.omp/agent/dispatch-overlay.yml` с `retry.maxRetries: 1`; повторами занимается эскалация. Код выхода 3 (`--max-cost`) означает бюджет, а не сбой исполнителя.

Правила валидации — кто и когда в задаче гоняет тесты и линт — собраны в [`docs/workflow.md`](docs/workflow.md): один владелец валидации на задачу, неизменённый код второй раз зелёным не прогоняется; обязательные pre-commit гейты (ruff, detect-secrets, pytest) сохраняются.

## Телеметрия

```bash
agent-dispatch export --since 7d > dataset.jsonl
agent-dispatch feedback <task_id> --outcome user_accepted
```

Одна строка JSONL на задачу: запрос, решение Jev со всеми вероятностями, результат, события guards и feedback. Для routing-эксперимента `router=explore`, а `judgments` содержат `exploration_arm`, `exploration_jev_choice` и `exploration_static_choice`; контрольный arm остаётся `router=jev` с теми же полями. По этому датасету считаются routing accuracy, доля fallback и override, стоимость и латентность по исполнителям.

## Evals

Два платных прогона, см. [`evals/README.md`](evals/README.md): `routing` меряет accuracy на 20 размеченных задачах через живой демон (порог 0.8), `smoke` гоняет три реальные задачи на каждом исполнителе во временной репе. Рядом живут бесплатные детерминированные сьюты: `evals.executors` (argv и парсинг результатов omp-адаптера на корпусе), `evals.scheduling` (инварианты ёмкости задач и ревью in-process) и `evals.workflow` (контракты в отрендеренном TaskPackage).

## Разработка

```bash
uv sync
uv run pytest tests -q                 # gate-тесты, без сети и без настоящих CLI
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install              # ruff, detect-secrets, pytest перед каждым коммитом
```

Структура: `agent_dispatch/routing` (guards, Jev, claude_local, decision), `dispatch` (dispatcher, task package, escalation), `executors` (адаптеры, process runner, registry), `telemetry` (SQLite), `api` (FastAPI), `mcp` (прокси, автостарт), `cli.py`, `doctor.py`. Подробности в [`docs/architecture.md`](docs/architecture.md), план и история решений в [`docs/plans/`](docs/plans/).


Ключ балансера omp получает сам через `apiKey: "!…"`; `omp-setup/bin/lb-key` читает его из `~/.config/agent-lb/env`. Диспетчер вырезает `AGENT_LB_API_KEY` из env omp, потому что окружение запускающего процесса доходит до bash-агента. Конфиг omp поддерживается в `~/my_git_reps/omp-setup`; перенос старого конфига выполняется командой `agent-dispatch migrate-config`. Корпоративные исполнители используют профиль x5.
