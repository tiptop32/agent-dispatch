# Архитектура AgentDispatch

## Процессы

```text
agent (claude / omp)
  └─ stdio MCP: `agent-dispatch mcp`        прокси без логики, логи только в stderr
        │ HTTP 127.0.0.1:7433, Authorization: Bearer <token из serve.json>
        ▼
`agent-dispatch serve`                       демон, один на машину
  ├─ api/            FastAPI: /health, /executors, /route, /tasks, /tasks/{id}, /tasks/{id}/followup, /tasks/{id}/feedback, /export
  ├─ dispatch/       dispatcher (state machine задачи), task_package, escalation
  ├─ routing/        static.py (deterministic balanced/capability), Jev, claude_local, preflight, guards, decision
  ├─ executors/      base, process, workspace, result_parser, env, claude, omp, registry, health
  ├─ telemetry/      SQLite ~/.local/share/agent-dispatch/dispatch.db
  └─ config.py       ~/.config/agent-dispatch/config.yaml + env
```

Демон стартует лениво: прокси делает `GET /health`, при отсутствии спавнит `python -m agent_dispatch.cli serve` (stdin `DEVNULL`, stdout и stderr в `data_dir/logs/serve.log`, своя process group, `cwd=data_dir`) и ждёт `/health` до 5 с. Рабочий каталог исключает подхват кода из репозитория вызывающего через первый элемент `sys.path` у `python -m`: демон использует установленный пакет, а не смесь кода рабочей копии и свежих шаблонов. Состояние в `data_dir/serve.json` (pid, порт, токен, права 0600).

HTTP защищён двумя проверками: заголовок `Host` только `127.0.0.1`/`localhost` (защита от DNS rebinding, ответ 421) и `Bearer` токен из `serve.json` для всего, кроме `/health` (ответ 401).

## Жизненный цикл задачи

```text
queued → routing → running → completed | partial | failed | needs_context | needs_escalation | cancelled
```

Порядок в `Dispatcher._run`:

1. Pre-guards (`routing/guards.py`, чистые функции): `bad_cwd` (нет каталога или не git-репа), `max_hops`, `unknown_parent`, `max_children` (только для детей, ретраи эскалации не считаются), явный `executor` (disabled или unavailable → отказ, иначе `router=override`). Отказ `max_children` перечисляет `task_id` работающих соседних задач и направляет вызывающего в `status(task_id, wait_seconds)`, чтобы тот не повторял заведомо бесполезный `dispatch`.
2. Кандидаты: enabled минус unavailable (кэш `check --version` с TTL плюс остывающие после сбоя исполнителя, см. п. 10) минус исполнители с `adapter == source_agent` при `exclude_source_agent` и `hop == 0` минус исполнители, которым источник вправе отдать только ревью (`routing.review_only`, по умолчанию `omp: [claude]`), если это `kind: task`. Явный `executor` из этого списка получает отказ `review_only`; те же исполнители выпадают из цепочки эскалации задачи.
3. `prod` (свой или унаследованный) выбирается статически внутри `corporate: true`: task state не уходит роутерам. Для обычной задачи backend `static` вызывает только `capability.select` с `capability` из запроса или `balanced` по умолчанию и соблюдает `max_capability`, доступность, квоты и периметр. Legacy `jev`/`claude_local` включаются явно. Перед Jev `routing/preflight.py` проверяет realpath cwd против `router.external.trusted_roots`; неизвестный или симлинк-выходящий проект блокируется до сериализации task state. Все router failures идут в fail-closed fallback.
4. Post-guards: `confidence < min_confidence`, `top1 − top2 < min_margin`.
5. `routing.explore_share` делит пригодные решения Jev стабильным SHA-256 от `task_id`. В arm `explore` решение заменяется результатом `capability.select("balanced", candidates, corporate=...)` по уже отфильтрованному пулу. `max_capability` не меняет этот статический baseline, а `exploration_jev_choice` хранит уже ограниченный выбор Jev. В `judgments_json` обеих групп пишутся arm, выбор Jev и статический выбор; у экспериментальной группы `router=explore`. Override, escalation, follow-up, review и fallback пропускаются. Повторная проверка доступности не даёт подмене выбрать успевшего остыть исполнителя.
6. Корневая coding-задача (`hop == 0`, `kind: task`) сначала занимает семафор своей `ExecutorSettings.resolved_limit_group` ёмкостью `server.max_concurrent_per_group` (по умолчанию 2), затем общий `server.max_concurrent_tasks` (по умолчанию 4). Такой порядок не держит общий слот во время ожидания занятой группы и не создаёт head-of-line blocking для независимой квоты. После получения обоих слотов пишется событие `slot` (`limit_group`, `wait_seconds`) и повторно проверяется доступность исполнителя. Оба слота освобождаются контекстными менеджерами при успехе, отмене и исключении. Ревью (`kind: review`) сохраняет отдельный `server.max_concurrent_reviews` (по умолчанию 1), поэтому при дефолтах максимум корневых процессов равен 5. Lock по корню рабочей копии (`git rev-parse --show-toplevel`, вне git `realpath(cwd)`) на правку берут только coding-задачи; ревью идёт только на чтение. Вложенные задачи (`hop > 0`) корневых семафоров не ждут: они не могут ждать слот, который держит их родитель.
   `limit_groups.<группа>.max_concurrent` заменяет общий лимит группы. `executors.<имя>.max_concurrent` действует на время CLI, включая ревью, вложенные задачи и `queue_fallback`; слот освобождается до ожидания ревью и между раундами. Вложенная задача того же исполнителя использует слот предка. Внутренние потомки авторевью не входят в `max_children`.
7. `TaskPackage` → `render_prompt(package, adapter_kind)` (jinja2, снапшоты в тестах). Промпт для omp и claude использует блок ```` ```agent-dispatch-result ````.
   Для любой задачи и ревью Task Package запрещает обход отказа инструмента: если hook, guard или песочница отклонили вызов, исполнитель прекращает это действие, не переносит его в другой инструмент, eval или скрипт и сообщает отказ с причиной.
8. Адаптер получает `RunContext` с env из `child_env(settings, extra)`: `os.environ` без секретов из `Settings.secret_names()`, без маркеров сессии Claude Code (`CLAUDECODE`, `CLAUDE_CODE_*`, иначе `claude -p` отказывается работать вложенно), плюс `AGENT_DISPATCH_TASK_ID`, `AGENT_DISPATCH_ROOT_AGENT`, `AGENT_DISPATCH_HOP = hop + 1`. Секрет, который CLI ищет в своём env, туда не попадёт: `doctor` в этом случае дописывает к упавшей проверке `executor:<имя>`, что ключ лежит в env-файле agent-dispatch.
9. Результат нормализуется в `ExecutionResult`; `changed_files` из `git status --porcelain -z` до и после с отпечатками содержимого и режима файлов (`workspace.snapshot`, пути от корня репозитория); исключение адаптера → `failed`; отмена → `cancelled`.
   Нестроковые `summary`, `tests.command`, `tests.result` и элементы `changed_files` приводятся к строкам с отметкой `meta.coerced`. Ход omp с `stopReason: error` после изменений даёт `partial` с `meta.cli_error`, без изменений `failed`; классификатор сбоев учитывает такой `partial`, не теряя остывание по ошибке CLI.
   Внутренняя taxonomy `failure_class` различает `transport_error`, `rate_limit`, `quota_exceeded`, `timeout`, `stream_stalled`, `verification_failed`, `task_failed`, `cancelled`, `configuration_error`. `sequence_number regressed`, reset/closed connections и websocket transport failures получают `transport_error`; до первого ответа допускается ограниченный retry только после повторной проверки fingerprint рабочей копии. Исчерпанный transport retry не эскалируется на более дорогую модель.

   Сторож omp проверяет события каждые `watchdog_interval_seconds: 300`: предел молчания `idle_timeout_seconds: 300` без открытого инструмента и `watchdog_tool_idle_seconds: 900` с открытым. Шесть одинаковых подряд комбинаций инструмента, аргументов и результата (`watchdog_repeat_limit: 6`) дают `stalled: looping`; молчание — `stalled: idle`. Подробности в `meta.watchdog`; остывания нет. Ноль idle отключает только предел без открытого инструмента; жёсткий таймаут остаётся. Нестриминговый Claude сторожа не использует.

   Если в запросе есть `verify`, а результат `completed` или `partial` (не ревью), демон сам выполняет команды через `/bin/sh -c` в каталоге исполнителя (`dispatch/verify.py`): по порядку, до первой упавшей, каждая не дольше `execution.verify_timeout_seconds`, вывод идёт в лог задачи с префиксом `[verify]`. Итог в `result.verification` (`passed | failed`, команды с кодом выхода и хвостом вывода, `tests_changed` — изменённые файлы, похожие на тесты, `contradicts_report` — исполнитель отчитался `tests: passed`). Упавшая проверка переводит `completed` в `partial` с `error: verification failed (...)` и даёт причину эскалации `verification_failed`. В worktree проверка идёт в подкаталоге дерева, соответствующем `cwd`, с путями рабочей копии, переписанными на дерево; перед ней работа коммитится на ветку задачи, после неё дерево откатывается к этому коммиту, чтобы артефакты проверки не попали в патч. Task Package перечисляет команды проверки исполнителю.

   Автоматическое ревью (`Dispatcher._review_work`) использует `review_by` вне периметра и `perimeter_review_by` внутри, включая `prod`. Каждое поле принимает имя или упорядоченный список; конфиг запрещает неизвестные имена, себя в `review_by` и некорпоративных ревьюеров в `perimeter_review_by`. При отсутствии корпоративного ревьюера результат `skipped`, внешний не вызывается. После verify результат `completed` с непустым `changed_files` проверяют до интеграции задачи-потомки `kind: review`, `hop + 1`, без эскалации и вне `max_children`. Они видят задачу и диф, но не контекст вызывающего. В worktree работа сначала коммитится, диф строится от `review_base`; in_place ревью ограничивается `changed_files`. Ошибка базы не маскируется пустым дифом. Артефакты ревью в worktree откатываются; отмена родителя отменяет ревьюера. Ступени идут по порядку: `pass`, `skipped` и `unknown` переходят к следующей, `fail` останавливает цепочку. Итоги в `meta.review` и `meta.review_stages`; замечания ограничены 1500 символами и передаются следующему звену в context, не в error.

   После `verification_failed` или `fail` ревью доступен ограниченный раунд правки: `execution.review_fix_rounds` по умолчанию 1, общий на все ступени звена; 0 отключает. После проверки бюджета тот же исполнитель продолжает сессию в том же cwd внутри запуска. Демон повторяет verify и только проваленную ступень ревью, а не пройденные. `changed_files` объединяются, usage и cost суммируются, отдельный расход раунда записывается в `meta.fix_rounds`; после исчерпания бюджета раундов действует обычная эскалация.

   Итоговые argv omp-адаптера: coding-задача получает `--approval-mode yolo`, а ревью — `--tools read,grep,glob`; инструмента `find` нет.
10. Эскалация: при `failed`, `needs_escalation`, упавшей проверке `verify`, вердикте автоматического ревью `fail` (`review_failed`) или `tests.result == failed` (последнее не считается, если `verify` прошёл) и `allow_escalation` берётся следующий из `escalation[executor]`, не пройденный раньше по цепочке `escalated_from`; создаётся дочерняя задача с явным executor и `escalated_from`. `wait()` следует за `meta.escalated_to`, поэтому вызывающий получает результат последнего звена; `GET /tasks/{id}?wait=N` идёт через `wait()` и для уже завершённого звена, так что ответ не зависит от момента опроса. Исчерпанная цепочка → `failed`. Требование периметра предка (`Dispatcher._lineage_perimeter` читает `prod` запроса и `meta.perimeter` по `escalated_from`, `followup_of` и `parent_task_id` из базы, поэтому требование переживает рестарт демона) действует на каждом звене, на переадресации роутеру и на follow-up: внешние звенья пропускаются, роутеру внешние исполнители не предлагаются, а когда корпоративных звеньев не осталось и переадресации нет, цепочка останавливается с `result.meta.escalation_stopped: perimeter`. Внешний исполнитель, доставшийся задаче под периметром (своё решение или предок), даёт отказ `guard: perimeter` до запуска (`Dispatcher._perimeter_refusal`); явный `executor` корневой задачи остаётся решением владельца данных только без `prod` и без такого предка.

   Таймаут или `stalled` с правками передаёт начатую работу лишь при наличии допустимого следующего звена с учётом настроек, периметра и бюджета. В worktree наследнику применяется патч непосредственного предка, не всей цепочки; in_place правки уже на месте. При ошибке патча наследник стартует с HEAD и фиксирует `meta.handoff.apply_failed`. Без допустимого звена прежний результат не меняется.

   Сбой исполнителя, а не задачи (`executors/health.py:classify_failure`: лимит расходов, `at capacity`, отказ авторизации, `Missing environment variable: <...>_API_KEY` в начале строки (не у MCP-сервера), таймаут или `omp: no model response` без изменённых файлов), выводит исполнителя из ротации на `routing.failure_cooldown_seconds` (по умолчанию 900, `0` выключает), а исчерпанный лимит до момента сброса из сообщения CLI (`health.py:parse_reset`: `resets 5am (TZ)`, `resets Sep 26 at 5am`, `Try again at 5:02 PM`, `try again in 2 hours`) плюс 60 секунд, или на `quota_cooldown_seconds` (3600), если момент не назван. Классификатор ищет строки про ключ только в `ExecutionResult.error`: текст задачи и stdout агента не выводят исполнителя из ротации. Лимит расходов и исполнители с `limit_reset: recheck` (пул за балансировщиком) перепроверяются не реже `quota_cooldown_seconds`. Лимит, авторизация и сеть выводят всю `limit_group` (по умолчанию группы omp и claude), перегрузка и лимит конкретной модели только её (`failure_scope`): событие `cooldown`, `meta.executor_failure`, причина эскалации `executor_<kind>`. Остывающий исполнитель выпадает из кандидатов, из цепочек эскалации и отвечает отказом с временем окончания на явный `executor`. Остывание пишется в таблицу `cooldowns` и восстанавливается при старте демона (`Dispatcher.restore_cooldowns`); `check --version` у CLI с исчерпанным лимитом проходит, поэтому снимают его только часы или человек (`agent-dispatch cooldown-clear`, `DELETE /executors/cooldowns`). Более короткое остывание не перебивает действующее длинное. MCP-инструмент `executors` показывает вызывающей модели, кто остывает и до какого времени. Если цепочка за упавшим исполнителем пуста, задача уходит роутеру заново (`escalate.to = "router"`), без всех, кто уже брал её по цепочке; цикл конечен, потому что каждый такой шаг выводит из ротации ещё одного исполнителя.

   `ConcurrencyLimitModel` и `Parallel processed requests limit exceed` классифицируются как `model_busy`: остывает только этот исполнитель на `routing.busy_cooldown_seconds` (120 с), не вся группа. Другие ошибки квоты сохраняют прежние правила.

   Обычно работа эскалируемого звена не переносится в рабочую копию (`meta.integration_held = "escalated"`): она остаётся коммитом и патчем, следующее звено стартует с HEAD. Исключение — handoff после timeout/stalled с правками: worktree-наследнику передаётся патч непосредственного предка, in_place работа уже в копии. Без эскалации работа интегрируется как прежде.

Потолки расходов (по умолчанию выключены). `routing.max_chain_cost_usd`: перед эскалацией суммируется цена звеньев цепочки `escalated_from` и всех их подзадач по `parent_task_id` (`Storage.tree_cost`) плюс цена текущего результата; дошла до потолка — следующее звено не создаётся, цепочка считается исчерпанной, в `meta.budget` цена и лимит. `routing.daily_cost_limit_usd`: в `_prepare` до роутера суммируются цена задач, завершённых с местной полуночи, и решений роутера (`Storage.cost_since`); дошла до лимита — отказ `guard: budget`. Оба потолка проверяет и `_budget_stop` при планировании эскалации (с ценой текущего результата): звено не создаётся, работа не удерживается. `usage` и `cost_usd` суммируются по всем ассистентским `message_end`, потому что каждый ход omp имеет собственный usage.

Любое исключение вне адаптера тоже переводит задачу в `failed` и ставит событие: задача не может остаться `running` навсегда. После рестарта демона `recover_stale()` переводит осиротевшие `queued/routing/running` в `failed` с `error="daemon restarted"`.

## Продовый периметр и данные

`DispatchRequest.prod: true` (MCP `route`, `dispatch`, `dispatch_to`; CLI `--prod`) всегда закрывает задачу внутри `corporate: true`, даже при `routing.corporate_perimeter: false`. Без включённого корпоративного исполнителя запрос получает `guard: perimeter` до запуска. Роутеры не вызываются вовсе: требование уже известно, а текст продовой задачи нельзя отдавать внешнему Jev и `claude_local`; исполнитель выбирается статически внутри периметра, `decision.meta.perimeter = "prod"`. Вложенный MCP `route` наследует периметр родителя так же, как `dispatch`. Оно сильнее `answered`/`unknown` и действует на всю линию: fallback, эскалации, переадресацию, follow-up, вложенные задачи и ревью. Явный внешний исполнитель корневой prod-задачи также получает отказ.

`TaskPackage.prod` добавляет «Prod data rule» (`templates/prod_data_rule.md.j2`) исполнителю, ревьюеру и возобновлённому follow-up (у `followup` нет своего флага `prod`, но follow-up, вызванный из prod-задачи, наследует его через `parent_task_id` и мог получить его впервые). Разрешены выводы и цифры: количества, статусы, итоги проверок и причины. Строки таблиц, целые логи, секреты, строки подключения и персональные данные запрещены в сообщениях, отчётах и файлах. Допустима одна строка доказательства с замаскированными секретами и персональными данными.

Перед записью результата `_finalize` вызывает `dispatch/redact.py` для prod-линии: в `summary` и `error` маскируются приватные ключи PEM, пароли строк подключения, JWT, Bearer и пары ключ-значение с секретами. `result.meta.redacted` содержит число замен (`replacements`) и имена очищенных полей (`fields`), без значений. Очистка узкая: она не обезличивает произвольные данные и не чистит файлы, логи или все поля результата, поэтому не заменяет правило Task Package.

Бесплатный `evals.replay` проверяет `perimeter_violations` (должно быть 0) и показывает секцию `prod`: число задач, включая отказы до решения роутера и потомков, распределение запусков по исполнителям, вердикты ревью и сработавшие очистки. Требование восстанавливается из `request_json.prod`, метки решения и предков; для prod оно сохраняется и при фильтрации `--since`, когда предок вне окна.

## Follow-up

`Dispatcher.followup(task_id, FollowupRequest)` продолжает готовую задачу: берётся последнее звено её цепочки эскалации (как в `wait`), новая задача копирует его запрос с `task = message`, `executor` = тот же исполнитель (или роутер, если он остывает), `followup_of`, `workspace_mode: in_place` и `verify` прошлой задачи, если не задан новый. Звено, которому guard отказал до запуска, пропускается. Отказ (`FollowupError`, HTTP 409): задача ещё идёт, не дошла до исполнителя или её работа не в рабочей копии (`integrated: false` с патчем, веткой или оставшимся деревом). MCP передаёт в `FollowupRequest` источник, hop и родителя вызывающего, и они заменяют данные исходной задачи. Звено эскалации follow-up получает `followup_of` упавшей попытки, а промпт без сессии несёт и исходную задачу цепочки (`Original task`). Выбор звена, запрос и сводка — `dispatch/followup.py`.

Адаптеры кладут id сессии CLI в `result.meta.session_id` (claude: заданный демоном `--session-id` или `session_id` отчёта; omp: id из первой строки `{"type":"session"}`). `_build_context` передаёт его в `RunContext.resume_session`, если прошлый запуск шёл in_place в том же `cwd` тем же исполнителем; адаптеры продолжают сессию (`claude --resume`, omp `--resume <session id>` в том же cwd, session-dir и профиле), а промпт короткий (`followup_resumed.md.j2`). Иначе полный Task Package с разделом «Previous attempt»: запрос, изменённые файлы, ошибка, упавшая проверка, хвост отчёта. Продолжение, упавшее до работы (не сбой исполнителя, не таймаут, без изменений), один раз повторяется с нуля: событие `resume_failed`, `meta.resume_failed`.

## Hop-протокол и сабагенты

Инкремент глубины делается ровно в одном месте: демон запускает исполнителя с `AGENT_DISPATCH_HOP = hop + 1`. MCP-прокси в дочернем агенте читает env и передаёт `hop` без изменений. Трассировка при `max_hops: 2`:

```text
root agent (hop 0) ──dispatch──► задача A, исполнитель получает HOP=1
исполнитель A ──dispatch──► задача B (hop 1 < 2, разрешено), исполнитель получает HOP=2
исполнитель B ──dispatch──► отказ: hop 2 >= max_hops
```

Task Package говорит исполнителю, может ли он делить задачу («split into at most N subtasks») или нет («Do NOT delegate further»). Дети одной задачи ограничены `max_children` и делят рабочее дерево, поэтому инструкция требует непересекающихся `files`.

## Инвентаризация worktree

`agent-dispatch worktrees --all` обходит каталоги первого уровня в
`execution.worktree_dir` или `<data_dir>/worktrees`. Файл `.git` каждого дерева
указывает на служебный git-каталог, а его `commondir` связывает дерево с исходным
репозиторием. Имя каталога равно `task_id`, поэтому CLI добавляет статус из
`dispatch.db`, возраст по времени изменения `.git` и размер каталога.

`--all --clean` удаляет дерево только при финальном статусе задачи и после
проверки всех незавершённых `cwd`, как `Dispatcher._drop_worktree`. Перед
самим удалением база читается заново: демон мог принять задачу с `cwd` в
дереве после первого снимка. Симлинки и каталоги вне `worktrees/` в обход не
попадают, а у каждого удаляемого пути это перепроверяется ещё раз прямо
перед удалением (`worktree.ensure_stored_path`): каталог могли подменить
симлинком после инвентаризации. Остаётся окно в микросекунды между этой
проверкой и удалением: подменить каталог в нём может только тот, кто и так
пишет в каталог данных пользователя, поэтому удаление через fd не строится. Неизвестный
статус считается незавершённым. Если общий git-каталог исчез вместе с
репозиторием, выполнить `git worktree remove` уже нельзя: CLI удаляет оставшийся
каталог напрямую и помечает причину в выводе. `doctor` использует тот же обход,
показывает число и общий размер деревьев и печатает `WARN`, если хотя бы одному
дереву семь дней. Предупреждение не меняет код выхода `doctor`.

## Контракт Jev

Endpoint `POST https://openrouter.ai/api/alpha/decisions`, модель `typesafe/jev-1.13`. Один запрос на решение:

```json
{
  "model": "typesafe/jev-1.13",
  "state": {"task": "...", "context": "...", "files": [], "constraints": [], "source_agent": "omp"},
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

Замечание: legacy Jev получает `state` (`task`, `context`, `files`, `constraints`) на OpenRouter только после локального preflight trusted roots. Для обычных задач безопасный выбор — `router.backend: static`; `prod` и неизвестные проекты не передают task state внешнему роутеру.

## Формат результата

Схема `agent_dispatch/schemas/agent_result.schema.json`:

```json
{"status": "completed", "summary": "...", "changed_files": ["src/x.py"],
 "tests": {"command": "pytest -q", "result": "passed"}, "confidence": 0.9, "needs_escalation": false}
```

- omp получает результат из последнего ассистентского `message_end` NDJSON; usage и cost берутся из каждого такого события.
- Claude и omp пишут тот же JSON в fenced-блоке с тегом `agent-dispatch-result` в конце ответа; парсер берёт последний блок.
- `tests.result` вроде «1 passed» мягко приводится к enum, приведение фиксируется в `meta.coerced`.
- Нет блока или он невалиден → `partial` с `meta.parse_error`. Если блока нет, а файлы изменены и есть `meta.session_id`, демон один раз продолжает ту же сессию промптом `report_only.md.j2` (предел `execution.report_recovery_timeout_seconds`, по умолчанию 300, 0 выключает): удачный отчёт заменяет статус, итог и тесты, файлы объединяются, расходы складываются, `meta.report_recovery` = `recovered` или `failed: ...`, событие `report_recovery`; `verify` судит итог после дозапроса. Восстановленный отчёт стоит того же доверия, что обычный (модель пишет его в той же сессии), поэтому без `verify` `partial` может стать `completed`. Сбой самого дозапроса (лимит, пустой ответ) включает остывание исполнителя, файлы, тронутые дозапросом, остаются в `changed_files`. Запуск omp, в котором модель не ответила ни разу (только `step_start`, без изменений), это `failed` с `omp: no model response ...` и остывание `network` группы лимита. ненулевой exit → `failed`. Таймаут с непустым `changed_files` → `partial` (`error: timeout`, `meta.timed_out`) — работа есть, судит вызывающий по дифу; таймаут без изменений → `failed` и эскалируется. На жёстком таймауте `meta.last_output_age_s` — сколько секунд CLI молчал перед убийством (`null` у адаптеров без `streams_output`, то есть у claude): работавшего до конца агента отличают от зависшего. Таймаут запуска: `timeout_seconds` запроса, иначе `executors.<имя>.timeout_seconds`, иначе `routing.default_timeout_seconds` (`Settings.timeout_for`); тот же срок идёт в Task Package как бюджет времени.

## Телеметрия

Четыре таблицы в SQLite (`tasks`, `routing_decisions`, `events`, `cooldowns`), WAL. Решения `route` без запуска пишутся с `task_id = NULL`. `GET /export?since=7d` отдаёт JSONL: одна строка на задачу с вложенными `decision` и `events`, затем строки безадресных решений. При `submit` демон пишет событие `git` с абсолютным общим git dir и веткой `cwd` (`worktree.git_location`), если `cwd` в git. `feedback` это событие с `outcome` из `success | failure | manual_override | escalated | user_accepted | user_reworked`. Arm эксперимента хранится без миграции схемы: `router=explore` для подмены, а `judgments_json` обеих групп содержит `exploration_arm`, `exploration_jev_choice` и `exploration_static_choice`.

Процесс MCP дописывает `<data_dir>/mcp_responses.jsonl` для `dispatch`, `dispatch_to`, `status`, `followup` и `route`: `ts`, `tool`, `task_id` (`null` у route), `bytes` текста ответа в UTF-8 и `verbose`. Текст задачи и ответа не записывается; ошибка записи не влияет на ответ. `meta.diff_stat` считает файлы и добавленные/удалённые строки фактической дельты задачи без исходных грязных изменений вызывающего; бинарные файлы учитываются только в числе файлов.

`uv run python -m evals.replay` прогоняет правила диспатчера по этой базе без сети и CLI: сколько запусков ушло к уже лежавшему исполнителю, сколько недоделок легло в рабочую копию перед эскалацией, сколько `partial` дал только формат отчёта, приняли ли вызывающие дифы (удалённый feature-worktree судится по общему git dir из события `git` или по соседним checkouts) и работали ли агенты до жёсткого таймаута. Секция `queue wait by limit group` считает по событиям `slot` число ожиданий дольше 60 секунд, среднее и максимум каждой группы; задачи до появления события в эту секцию не входят.

Replay связывает ответы с корнем в `caller_bytes` и показывает `watchdog`, `handoff`, `fix_round`, `review_stages` по записанным метаданным. Отсутствующие данные не означают нулевой расход. Недельные критерии приёмки проверяются после релиза, не считаются уже достигнутыми.

Метрики, ради которых всё это собирается: routing accuracy на размеченных задачах (порог 0.8), доля `low_confidence`/`fallback`/`override`, success rate и медианная длительность по исполнителям, стоимость решения и исполнения.


Профиль x5 запускает корпоративных исполнителей omp с `--profile x5` и `NODE_USE_SYSTEM_CA=1` (корпоративный RootCA читается из Keychain); он ограничивает провайдеры и роли также для сабагентов `task`, `prewalk` и `advisor`. Каждый запуск получает `--config ~/.omp/agent/dispatch-overlay.yml` с `retry.maxRetries: 1`, потому что повторами занимается эскалация диспетчера. omp запускается со stdin=DEVNULL, иначе без TTY ждёт EOF и висит.

Исторические `codex/*` и `opencode/*` replay судит по настройкам omp-преемника (`evals.replay.harness.successor`, таблица из `migrate.py`); эти источники остаются валидными для истории. Старый пользовательский конфиг с adapter codex/opencode не загружается и сообщает о необходимости `agent-dispatch migrate-config`.
