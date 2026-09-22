# Архитектура AgentDispatch

## Процессы

```text
agent (claude / codex / opencode)
  └─ stdio MCP: `agent-dispatch mcp`        прокси без логики, логи только в stderr
        │ HTTP 127.0.0.1:7433, Authorization: Bearer <token из serve.json>
        ▼
`agent-dispatch serve`                       демон, один на машину
  ├─ api/            FastAPI: /health, /executors, /route, /tasks, /tasks/{id}, /tasks/{id}/feedback, /export
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
2. Кандидаты: enabled минус unavailable (кэш `check --version` с TTL) минус исполнители с `adapter == source_agent` при `exclude_source_agent` и `hop == 0`.
3. Один кандидат → решение без роутера. Иначе `decide_with_fallback`: `JevRouter` → при `RouterError` `ClaudeLocalRouter` → при обоих отказах `fallback_executor` (или первый кандидат, если fallback не в кандидатах).
4. Post-guards: `confidence < min_confidence`, `top1 − top2 < min_margin`.
5. `TaskPackage` → `render_prompt(package, adapter_kind)` (jinja2, снапшоты в тестах). Промпт для codex просит structured output, для claude и opencode блок ```` ```agent-dispatch-result ````.
6. Семафор `max_concurrent_tasks` и lock по `realpath(cwd)` только для корневых задач (`hop == 0`): вложенные задачи не могут ждать слот, который держит их родитель.
7. Адаптер получает `RunContext` с env из `child_env(settings, extra)`: `os.environ` без секретов из `Settings.secret_names()`, без маркеров сессии Claude Code (`CLAUDECODE`, `CLAUDE_CODE_*`, иначе `claude -p` отказывается работать вложенно), плюс `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP = hop + 1`.
8. Результат нормализуется в `ExecutionResult`; `changed_files` из `git status --porcelain -z` до и после; исключение адаптера → `failed`; отмена → `cancelled`.
9. Эскалация: при `failed`, `needs_escalation` или `tests.result == failed` и `allow_escalation` берётся следующий из `escalation[executor]`, не пройденный раньше по цепочке `escalated_from`; создаётся дочерняя задача с явным executor и `escalated_from`. `wait()` следует за `meta.escalated_to`, поэтому вызывающий получает результат последнего звена. Исчерпанная цепочка → `failed`.

Любое исключение вне адаптера тоже переводит задачу в `failed` и ставит событие: задача не может остаться `running` навсегда. После рестарта демона `recover_stale()` переводит осиротевшие `queued/routing/running` в `failed` с `error="daemon restarted"`.

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
    "executor":   {"type": "choice", "instructions": "Which coding agent should execute this task?",
                   "criteria": {"claude": "<description из конфига>", "codex": "...", "opencode/kimi": "..."}},
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
- Ответ: `answers.executor = {choice, probabilities, confidence}`, `answers.<score> = {score, legend, probabilities, confidence}`, `answers.<noul> = {noul: p}`, `usage = {input_tokens, output_tokens, cost}`.
- Стоимость решения около $0.00003, латентность около 1 с. Jev не виден в `GET /api/v1/models`.

Только `executor` влияет на маршрут. Остальные judgments пишутся в телеметрию для анализа routing accuracy. Jev не делает side effects: он никогда не запускает исполнителя.

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
- Нет блока или он невалиден → `partial` с `meta.parse_error`; ненулевой exit или таймаут → `failed`.

## Телеметрия

Три таблицы в SQLite (`tasks`, `routing_decisions`, `events`), WAL. Решения `route` без запуска пишутся с `task_id = NULL`. `GET /export?since=7d` отдаёт JSONL: одна строка на задачу с вложенными `decision` и `events`, затем строки безадресных решений. `feedback` это событие с `outcome` из `success | failure | manual_override | escalated | user_accepted | user_reworked`.

Метрики, ради которых всё это собирается: routing accuracy на размеченных задачах (порог 0.8), доля `low_confidence`/`fallback`/`override`, success rate и медианная длительность по исполнителям, стоимость решения и исполнения.
