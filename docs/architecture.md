# Архитектура AgentDispatch

## Процессы

```text
agent (claude / codex / opencode)
  └─ stdio MCP: `agent-dispatch mcp`        прокси без логики, логи только в stderr
        │ HTTP 127.0.0.1:7433, Authorization: Bearer <token из serve.json>
        ▼
`agent-dispatch serve`                       демон, один на машину
  ├─ api/            FastAPI: /health, /executors, /route, /tasks, /tasks/{id}, /tasks/{id}/followup, /tasks/{id}/feedback, /export
  ├─ dispatch/       dispatcher (state machine задачи), task_package, escalation
  ├─ routing/        Router-интерфейс, jev.py, claude_local.py, guards.py, decision.py
  ├─ executors/      base, process, workspace, result_parser, env, claude, codex, opencode, registry
  ├─ telemetry/      SQLite ~/.local/share/agent-dispatch/dispatch.db
  └─ config.py       ~/.config/agent-dispatch/config.yaml + env
```

Демон стартует лениво: прокси делает `GET /health`, при отсутствии спавнит `python -m agent_dispatch.cli serve` (stdin `DEVNULL`, stdout и stderr в `data_dir/logs/serve.log`, своя process group) и ждёт `/health` до 5 с. Состояние в `data_dir/serve.json` (pid, порт, токен, права 0600).

HTTP защищён двумя проверками: заголовок `Host` только `127.0.0.1`/`localhost` (защита от DNS rebinding, ответ 421) и `Bearer` токен из `serve.json` для всего, кроме `/health` (ответ 401).

## Жизненный цикл задачи

```text
queued → routing → running → completed | partial | failed | needs_context | needs_escalation | cancelled
```

Порядок в `Dispatcher._run`:

1. Pre-guards (`routing/guards.py`, чистые функции): `bad_cwd` (нет каталога или не git-репа), `max_hops`, `unknown_parent`, `max_children` (только для детей, ретраи эскалации не считаются), явный `executor` (disabled или unavailable → отказ, иначе `router=override`).
2. Кандидаты: enabled минус unavailable (кэш `check --version` с TTL плюс остывающие после сбоя исполнителя, см. п. 9) минус исполнители с `adapter == source_agent` при `exclude_source_agent` и `hop == 0` минус исполнители, которым источник вправе отдать только ревью (`routing.review_only`, по умолчанию `codex: [claude]`), если это `kind: task`. Явный `executor` из этого списка получает отказ `review_only`; те же исполнители выпадают из цепочки эскалации задачи.
3. Один кандидат → решение без роутера. Иначе `decide_with_fallback`: `JevRouter` → при `RouterError` `ClaudeLocalRouter` → при обоих отказах первый доступный из `fallback_executor` (имя или список, `guards.pick_fallback`), а если недоступны все, первый кандидат.
4. Post-guards: `confidence < min_confidence`, `top1 − top2 < min_margin`.
5. `TaskPackage` → `render_prompt(package, adapter_kind)` (jinja2, снапшоты в тестах). Промпт для codex просит structured output, для claude и opencode блок ```` ```agent-dispatch-result ````.
6. Корневые задачи (`hop == 0`) ограничены двумя независимыми семафорами: coding-задачи (`kind: task`) — `server.max_concurrent_tasks` (по умолчанию 2), ревью (`kind: review`) — отдельным `server.max_concurrent_reviews` (по умолчанию 1). Максимум корневых процессов — сумма этих лимитов: ревью живёт в своём пуле и не занимает coding-слот. Lock по `realpath(cwd)` на правку берут только coding-задачи — ревью идёт только на чтение и lock правки не претендует. Вложенные задачи (`hop > 0`) корневых семафоров не ждут, как и раньше: они не могут ждать слот, который держит их родитель.
7. Адаптер получает `RunContext` с env из `child_env(settings, extra)`: `os.environ` без секретов из `Settings.secret_names()`, без маркеров сессии Claude Code (`CLAUDECODE`, `CLAUDE_CODE_*`, иначе `claude -p` отказывается работать вложенно), плюс `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP = hop + 1`.
8. Результат нормализуется в `ExecutionResult`; `changed_files` из `git status --porcelain -z` до и после; исключение адаптера → `failed`; отмена → `cancelled`.

   Если в запросе есть `verify`, а результат `completed` или `partial` (не ревью), демон сам выполняет команды через `/bin/sh -c` в каталоге исполнителя (`dispatch/verify.py`): по порядку, до первой упавшей, каждая не дольше `execution.verify_timeout_seconds`, вывод идёт в лог задачи с префиксом `[verify]`. Итог в `result.verification` (`passed | failed`, команды с кодом выхода и хвостом вывода, `tests_changed` — изменённые файлы, похожие на тесты, `contradicts_report` — исполнитель отчитался `tests: passed`). Упавшая проверка переводит `completed` в `partial` с `error: verification failed (...)` и даёт причину эскалации `verification_failed`. В worktree проверка идёт в подкаталоге дерева, соответствующем `cwd`, с путями рабочей копии, переписанными на дерево; перед ней работа коммитится на ветку задачи, после неё дерево откатывается к этому коммиту, чтобы артефакты проверки не попали в патч. Task Package перечисляет команды проверки исполнителю.

   Итоговые argv OpenCode-адаптера: coding-задача получает `--auto` по умолчанию (headless `run` без него прерывается на первом же запросе разрешения); если в `extra_args` уже есть `--no-auto` или `--auto=false`/`--auto false`, флаг не добавляется — явный отказ приоритетнее. Ревью всегда идёт read-only с `--agent plan`: включённые формы auto-approval (`--auto`, `--auto=true`) убираются, явные отказы сохраняются.
9. Эскалация: при `failed`, `needs_escalation`, упавшей проверке `verify` или `tests.result == failed` и `allow_escalation` берётся следующий из `escalation[executor]`, не пройденный раньше по цепочке `escalated_from`; создаётся дочерняя задача с явным executor и `escalated_from`. `wait()` следует за `meta.escalated_to`, поэтому вызывающий получает результат последнего звена. Исчерпанная цепочка → `failed`.

   Сбой исполнителя, а не задачи (`executors/health.py:classify_failure`: лимит расходов, `at capacity`, отказ авторизации, таймаут или молчание после событий переподключения codex без изменённых файлов), выводит исполнителя из ротации на `routing.failure_cooldown_seconds` (по умолчанию 900, `0` выключает), а исчерпанный лимит до момента сброса из сообщения CLI (`health.py:parse_reset`: `resets 5am (TZ)`, `resets Sep 26 at 5am`, `Try again at 5:02 PM`, `try again in 2 hours`) плюс 60 секунд, или на `quota_cooldown_seconds` (3600), если момент не назван. Лимит расходов и исполнители с `limit_reset: recheck` (пул за балансировщиком) перепроверяются не реже `quota_cooldown_seconds`. Лимит, авторизация и сеть выводят всю `limit_group` (по умолчанию адаптер у claude и codex, провайдер у opencode), перегрузка и лимит конкретной модели только её (`failure_scope`): событие `cooldown`, `meta.executor_failure`, причина эскалации `executor_<kind>`. Остывающий исполнитель выпадает из кандидатов, из цепочек эскалации и отвечает отказом с временем окончания на явный `executor`. Остывание пишется в таблицу `cooldowns` и восстанавливается при старте демона (`Dispatcher.restore_cooldowns`); `check --version` у CLI с исчерпанным лимитом проходит, поэтому снимают его только часы или человек (`agent-dispatch cooldown-clear`, `DELETE /executors/cooldowns`). Более короткое остывание не перебивает действующее длинное. MCP-инструмент `executors` показывает вызывающей модели, кто остывает и до какого времени. Если цепочка за упавшим исполнителем пуста, задача уходит роутеру заново (`escalate.to = "router"`), без всех, кто уже брал её по цепочке; цикл конечен, потому что каждый такой шаг выводит из ротации ещё одного исполнителя.

   Работа звена, которое эскалируется, в рабочую копию не переносится (`meta.integration_held = "escalated"`): она остаётся коммитом на ветке задачи и файлом патча. Следующее звено стартует с HEAD, и его патч ложится на чистую копию. Раньше недоделка применялась первой, и готовый патч следующего звена конфликтовал с ней. Без эскалации упавшая работа интегрируется как прежде, о ней судит вызывающий.

Потолки расходов (по умолчанию выключены). `routing.max_chain_cost_usd`: перед эскалацией суммируется цена звеньев цепочки `escalated_from` и всех их подзадач по `parent_task_id` (`Storage.tree_cost`) плюс цена текущего результата; дошла до потолка — следующее звено не создаётся, цепочка считается исчерпанной, в `meta.budget` цена и лимит. `routing.daily_cost_limit_usd`: в `_prepare` до роутера суммируются цена задач, завершённых с местной полуночи, и решений роутера (`Storage.cost_since`); дошла до лимита — отказ `guard: budget`. Считается только цена, которую сообщает CLI (`usage.cost_usd` у claude и opencode).

Любое исключение вне адаптера тоже переводит задачу в `failed` и ставит событие: задача не может остаться `running` навсегда. После рестарта демона `recover_stale()` переводит осиротевшие `queued/routing/running` в `failed` с `error="daemon restarted"`.

## Follow-up

`Dispatcher.followup(task_id, FollowupRequest)` продолжает готовую задачу: берётся последнее звено её цепочки эскалации (как в `wait`), новая задача копирует его запрос с `task = message`, `executor` = тот же исполнитель (или роутер, если он остывает), `followup_of`, `workspace_mode: in_place` и `verify` прошлой задачи, если не задан новый. Отказ (`FollowupError`, HTTP 409): задача ещё идёт, не дошла до исполнителя или её работа не в рабочей копии (`integrated: false` с патчем или веткой).

Адаптеры кладут id сессии CLI в `result.meta.session_id` (claude: заданный демоном `--session-id` или `session_id` отчёта; codex: `thread.started.thread_id`; opencode: `sessionID`). `_build_context` передаёт его в `RunContext.resume_session`, если прошлый запуск шёл in_place в том же `cwd` тем же исполнителем; адаптеры продолжают сессию (`claude --resume`, `codex exec … resume <id> -`, `opencode run --session`), а промпт короткий (`followup_resumed.md.j2`). Иначе полный Task Package с разделом «Previous attempt»: запрос, изменённые файлы, ошибка, упавшая проверка, хвост отчёта. Продолжение, упавшее до работы (не сбой исполнителя, не таймаут, без изменений), один раз повторяется с нуля: событие `resume_failed`, `meta.resume_failed`.

## Hop-протокол и сабагенты

Инкремент глубины делается ровно в одном месте: демон запускает исполнителя с `AGENT_DISPATCH_HOP = hop + 1`. MCP-прокси в дочернем агенте читает env и передаёт `hop` без изменений. Трассировка при `max_hops: 2`:

```text
root agent (hop 0) ──dispatch──► задача A, исполнитель получает HOP=1
исполнитель A ──dispatch──► задача B (hop 1 < 2, разрешено), исполнитель получает HOP=2
исполнитель B ──dispatch──► отказ: hop 2 >= max_hops
```

Task Package говорит исполнителю, может ли он делить задачу («split into at most N subtasks») или нет («Do NOT delegate further»). Дети одной задачи ограничены `max_children` и делят рабочее дерево, поэтому инструкция требует непересекающихся `files`.

## Контракт Jev

Endpoint `POST https://openrouter.ai/api/alpha/decisions`, модель `typesafe/jev-1.13`. Один запрос на решение:

```json
{
  "model": "typesafe/jev-1.13",
  "state": {"task": "...", "context": "...", "files": [], "constraints": [], "source_agent": "codex"},
  "questions": {
    "capability": {"type": "choice", "instructions": "What level of coding capability does this task demand...",
                   "criteria": {"fast": "...", "balanced": "...", "strong": "..."}},
    "judgment":   {"type": "noul", "instructions": "Does this task require resolving trade-offs..."},
    "corporate_data": {"type": "noul", "instructions": "...must not be sent to an external model provider?"},
    "difficulty": {"type": "score", "instructions": "How hard is this coding task?",
                   "criteria": [{"label": "trivial", "description": "..."}, {"label": "moderate", "description": "..."}, {"label": "hard", "description": "..."}]},
    "task_type":  {"type": "choice", "instructions": "...", "criteria": {"bugfix": "...", "feature": "...", "refactor": "...", "docs": "...", "test": "...", "ops": "..."}},
    "risk":       {"type": "score", "instructions": "...", "criteria": [...]},
    "ambiguity":  {"type": "score", "instructions": "...", "criteria": [...]},
    "decomposable": {"type": "noul", "instructions": "Can this task be split into independent subtasks handled in parallel?"}
  }
}
```

Что проверено вживую и зафиксировано тестами на записанных ответах (`tests/fixtures/jev/`):

- `instructions` обязателен у каждого вопроса, иначе 400 с zod-путём.
- У `choice` `criteria` это map `label -> description`, у `score` упорядоченный массив `{label, description}`.
- Ответ: `answers.capability = {choice, probabilities, confidence}`, `answers.<score> = {score, legend, probabilities, confidence}`, `answers.<noul> = {noul: p}`, `usage = {input_tokens, output_tokens, cost}`.
- Стоимость решения около $0.00003, латентность около 1 с. Jev не виден в `GET /api/v1/models`.

На маршрут влияют `capability`, `judgment` и `corporate_data`; конкретного исполнителя по ним подбирает `routing/capability.py` из `tier` и `corporate` в конфиге. Остальные judgments пишутся в телеметрию. Jev не делает side effects: он никогда не запускает исполнителя.

Вопрос об уровне работы вместо имени модели держит уверенность независимой от размера реестра: на 8 задачах 3 и 11 кандидатов дали среднюю 0.932 и 0.934, тогда как выбор по имени при 10 кандидатах падал до 0.35-0.60.

Уверенность делится на три полосы: `autonomous` (>= `routing.autonomous_confidence`), `advisory` (>= `min_confidence`) и `fallback` (ниже). Полоса лежит в `RouteDecision.confidence_tier`, подмена исполнителя происходит только в `fallback`.

Замечание: `state` (`task`, `context`, `files`, `constraints`) уходит на OpenRouter даже для задач, которые потом пойдут в корпоративную модель, подключённую через OpenCode. Если это ограничение, отправляйте такие задачи через `dispatch_to`, где Jev не вызывается.

## Формат результата

Схема `agent_dispatch/schemas/agent_result.schema.json`:

```json
{"status": "completed", "summary": "...", "changed_files": ["src/x.py"],
 "tests": {"command": "pytest -q", "result": "passed"}, "confidence": 0.9, "needs_escalation": false}
```

- Codex получает её через `--output-schema` в strict-варианте (`agent_result.strict.schema.json`: все ключи в `required`, `additionalProperties: false` на каждом уровне, опциональные поля nullable). Обычную схему Codex отвергает.
- Claude и OpenCode пишут тот же JSON в fenced-блоке с тегом `agent-dispatch-result` в конце ответа; парсер берёт последний блок.
- `tests.result` вроде «1 passed» мягко приводится к enum, приведение фиксируется в `meta.coerced`.
- Нет блока или он невалиден → `partial` с `meta.parse_error`; ненулевой exit → `failed`. Таймаут с непустым `changed_files` → `partial` (`error: timeout`, `meta.timed_out`) — работа есть, судит вызывающий по дифу; таймаут без изменений → `failed` и эскалируется.

## Телеметрия

Три таблицы в SQLite (`tasks`, `routing_decisions`, `events`), WAL. Решения `route` без запуска пишутся с `task_id = NULL`. `GET /export?since=7d` отдаёт JSONL: одна строка на задачу с вложенными `decision` и `events`, затем строки безадресных решений. `feedback` это событие с `outcome` из `success | failure | manual_override | escalated | user_accepted | user_reworked`.

`uv run python -m evals.replay` прогоняет правила диспатчера по этой базе без сети и CLI: сколько запусков ушло к уже лежавшему исполнителю, сколько недоделок легло в рабочую копию перед эскалацией, сколько `partial` дал только формат отчёта.

Метрики, ради которых всё это собирается: routing accuracy на размеченных задачах (порог 0.8), доля `low_confidence`/`fallback`/`override`, success rate и медианная длительность по исполнителям, стоимость решения и исполнения.
