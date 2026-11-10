# Экономия токенов Claude и ускорение: сторож, раунд правки, ступенчатое ревью, метрика вызывающего

## Overview
- Цель владельца: Claude делает только сложные части задач, всё остальное уходит дешёвым и бесплатным
  исполнителям, а сквозное время задачи падает за счёт параллельности и раннего обрыва зависших.
- Данные 2026-11-02..10 (`dispatch.db`):
  - `claude/opus` как исполнитель: 35 задач, 26,6 мин в среднем, $63,7 (12 пришли эскалацией);
    ревью opus 21 шт, $15,8. Это больше, чем все остальные исполнители вместе.
  - `omp/sol`: 60 задач, 7,4 мин, $23,6 (26 эскалацией). GLM (`omp/x5-code-large`): 42 задачи,
    17,1 мин, $0, 18 провалов ревью sol, 9 таймаутов по 30 мин.
  - Ожидание слота меньше минуты: время уходит на длительность задач и последовательные волны.
  - Токены главной сессии Claude (компактные ответы MCP, чтение дифов) не измеряются вовсе.
  - Живой пример 2026-11-10: ревью этого плана на `omp/sol` упало на обрыве websocket через 236 с, и
    задача целиком ушла к `claude/opus` (`61228d4f` -> `e74ddfde`).
- Таймаут GLM на `c0121093` (повтор 2026-11-10) — не зависание: последний вывод за 22 с до убийства,
  165 ходов модели, 86 вызовов инструментов, обычный цикл TDD при ~11 с на ход. Сторож обязан
  отличать «молчит», «крутится на месте» и «медленно, но идёт».
- Ревью плана (`e74ddfde`, opus после сбоя sol): «не готов», 6 P1. Эта версия плана их учитывает,
  разбор в разделе «Решения по ревью плана».

## Acceptance criteria (заморожены до старта)
- Остывание: `429 ConcurrencyLimitModel` одной модели не выводит другие модели группы; через неделю
  в `evals.replay` нет `became unavailable` у x5-исполнителей из-за чужого `model_busy`.
- Сторож: на фикстуре c0121093 (165 ходов по ~11 с с прогрессом) убийства нет; на фикстуре молчания
  без открытого инструмента и на фикстуре петли убийство есть. Через неделю `evals.replay`:
  ни одного `watchdog` с последующим `completed` того же исполнителя в follow-up (ложное убийство).
- Раунд правки: оставляем `review_fix_rounds: 1` по умолчанию, если через неделю `fix_round.recovered`
  >= 30% раундов и средняя цена раунда меньше средней цены звена эскалации; иначе 0.
- Ступенчатое ревью: через неделю (а) бесплатная ступень останавливает >= 30% работ x5 и (б) доля
  `fail` у sol на работах, прошедших бесплатную ступень, не выше 20% против базы 43% (18 из 42 работ
  GLM, где sol был единственной ступенью, 2026-11-02..09); иначе вернуть одну ступень. Знаменатель (б)
  наблюдаем: все работы, дошедшие до sol.
- Метрика вызывающего: `caller_bytes` есть у >= 95% корневых задач после релиза.

## Context (from discovery)
- Сторож: `executors/process.py:run_cli` (`read_stream` без хука на строку, `last_output_at`),
  `executors/base.py` (`idle_timeout_seconds`, `streams_output`), `executors/omp.py:128-212`
  (разбор потока после завершения), `executors/result_parser.py:43-61` (`stalled` уже `failed`),
  `executors/health.py:160` (`classify_internal_failure` -> `stream_stalled`).
- Эскалация: `dispatch/escalation.py:7-19`, `dispatch/dispatcher.py` (`_plan_escalation` ~1500-1548,
  `_escalate` 1683-1693: без звена `failed` «escalation exhausted», `_budget_stop` ~1631,
  `_build_context` ~1355).
- Ревью: `dispatcher.py` `_review_work`, `_wants_review` ~1036, `REVIEW_SUMMARY_LIMIT`;
  `_recover_report` ~1234 — образец продолжения сессии внутри того же запуска.
- Слоты: `dispatcher.py:798-833` (`_capacity`: `hop>0` и ревью мимо групп; `_acquire_group`,
  `queue_fallback` 776-792).
- Квота детей: `telemetry/storage.py:160-171` `count_children` считает и завершённых детей.
- Follow-up: `dispatch/followup.py` только in_place и отказ `worktree_not_integrated`; для раунда
  правки не годится.
- Ответы MCP: `mcp/server.py:172-241` (вердикт ревью уже печатается, `summary` режется до 800).
  `events.task_id NOT NULL` с FK (`telemetry/schema.sql:37-44`), API записи событий нет.
- omp: ошибка модели (`stopReason: error`) завершает процесс кодом 1 при `retry.maxRetries: 1`
  (`docs/research/omp-cli-facts.md:43-60`), отдельная «серия ошибок» в потоке не наблюдается.
- Скилл `~/.claude/skills/plan-dispatch` (`scripts/plan.py waves`, не больше 4 задач на волну).

## Development Approach
- **testing approach**: TDD — тест пишется первым и падает без правки (правило владельца).
- каждая задача закрывается целиком до следующей; маленькие сфокусированные изменения.
- **CRITICAL: каждая задача включает новые или обновлённые тесты**, успех и ошибки.
- **CRITICAL: гейт зелёный до следующей задачи**: `uv run pytest tests -q`,
  `uv run ruff check . && uv run ruff format --check .`.
- **CRITICAL: план обновляется при изменении объёма.**
- Не ломать правила `CLAUDE.md`: периметр и его наследование, `prod`, `review_only`, `max_capability`,
  остывание (слова агента не выводят исполнителя), `integration_held`, worktree-интеграция,
  `report_recovery`, transport retry, бюджеты.
- Правила в `CLAUDE.md` пишет только задача 10 (одна точка правки), чтобы задачи 2 и 3 не сталкивались
  на файле в одной волне. Конфиг (`config.py`, `config_default.yaml`, `config.example.yaml`) трогают
  задачи 1, 3, 5, 6, 7 — они идут по очереди.
- Порядок: задача 1 (сбой остывания, блокирует эксперименты с x5); волна — задачи 2 и 3; затем 4–9 по
  одной (все трогают `dispatcher.py`).

## Testing Strategy
- unit-тесты на каждое поведение; диспетчер — через фейковые CLI `tests/fakes/` и фикстуры
  `tests/fixtures/omp/`; новые фикстуры потока: c0121093 (медленно с прогрессом), молчание, петля.
- `evals.replay` получает счётчики (задача 8).

## Progress Tracking
- `[x]` сразу по выполнении; новые задачи с ➕; блокеры с ⚠️.

## Решения по ревью плана (`e74ddfde`)
- P1-1 раунд правки через `followup.py` невозможен (in_place, `worktree_not_integrated`, слот группы
  у родителя -> взаимная блокировка): раунд делается внутри того же запуска, как `_recover_report`.
- P1-2 `count_children` съедает квоту ступенями ревью: ревью-потомки `_review_work` больше не входят
  в `max_children` (у них свой предел: ступени × (раунды + 1)); правило `CLAUDE.md` меняется.
- P1-3 «без следующего звена остаётся partial» неверно: передача включается только при наличии
  звена в плане эскалации, иначе путь не меняется вовсе (прежний `partial`).
- P1-4 in_place: продолжение видит начатую работу в рабочей копии, патч не нужен — только пометка.
- P1-5 разбор omp после завершения: нужен хук на строку в `run_cli` и инкрементальный разбор.
- P1-6 «серия ошибок модели» в потоке omp не наблюдается (код 1): причина `model_errors` убрана.
- P2: idle не считается при открытом `tool_execution` (свой предел `watchdog_tool_idle_seconds`);
  петля по хешу «инструмент + аргументы + результат», без снимка рабочей копии; раунд сбрасывает
  verification/review, объединяет `changed_files`, складывает `usage`, проверяет `_budget_stop`;
  раунды общие на все ступени; передаётся патч только непосредственного предка; `max_concurrent`
  и для ревью, `hop>0` и `queue_fallback`; метрика MCP пишется JSONL из процесса MCP; в **Files:**
  добавлены пропущенные файлы; критерии приёмки заморожены выше.
- Не принято: P2 «задача 12 (бывшая 10) без согласия владельца» — владелец утвердил все рекомендации
  2026-11-10; P3 «счётчики replay в каждой задаче» — `harness.py` один, раздельные правки ломали бы
  параллельную волну 2 и 3; счётчики идут задачей 7 до релиза.

## Solution Overview
Номера ниже — пункты решения; номер задачи = пункт + 1.

0. **Занятость модели не квота**: `429 ConcurrencyLimitModel` и лимит параллельности ключа — класс
   `model_busy`, короткое остывание только этого исполнителя, без всей `limit_group`.
1. **Метрика вызывающего**: процесс MCP дописывает в `<data_dir>/mcp_responses.jsonl` строку на
   каждый ответ (`ts`, инструмент, `task_id` или `null` у `route`, байты, `verbose`); replay
   связывает строки с корневой задачей.
2. **Сторож**: `run_cli` получает необязательный `on_line` для stdout; адаптер omp разбирает события
   на лету. Раз в `watchdog_interval_seconds` (300): молчание дольше `idle_timeout_seconds` (300, было
   900) без открытого инструмента или дольше `watchdog_tool_idle_seconds` (900) при открытом —
   `stalled: idle`; `watchdog_repeat_limit` (6) подряд одинаковых хешей «инструмент + аргументы +
   результат» — `stalled: looping`. Медленный прогресс не трогается. Остывания нет (как у `stalled`).
3. **Передача начатой работы**: таймаут или `stalled` с непустым `changed_files` при наличии
   следующего звена эскалирует; worktree: звено стартует с патчем непосредственного предка; in_place:
   работа уже в копии. Task Package звена: «продолжи начатое» + итог попытки. Нет звена — путь прежний.
4. **Раунд правки внутри запуска**: после ревью `fail` или `verification_failed` — продолжение сессии
   того же исполнителя в том же рабочем каталоге (как `_recover_report`), с замечаниями; затем verify
   и та же ступень ревью. `execution.review_fix_rounds` (1) — общий бюджет на звено.
5. **Лимит по исполнителю**: `executors.<имя>.max_concurrent`; семафор исполнителя берётся для всех
   запусков, включая ревью и `hop>0` (без слота группы у них), и проверяется в `queue_fallback`.
6. **Ступенчатое ревью**: `review_by`/`perimeter_review_by` — строка или список; ступени по порядку,
   к старшей только после `pass`; ревью-потомки вне `max_children`.
7. **Счётчики replay** для 1–6.
8. **`diff_stat` в ответе**: файлы и +/− только по `changed_files` (в in_place без правок вызывающего).
9. Документация и правила `CLAUDE.md`.
11–12. Ведущая сессия: скилл plan-dispatch, `~/.claude/CLAUDE.md`, пользовательский конфиг.

## Technical Details
- `execution`: `watchdog_interval_seconds: 300`, `watchdog_tool_idle_seconds: 900`,
  `watchdog_repeat_limit: 6`, `review_fix_rounds: 1`, `idle_timeout_seconds: 300`.
- исполнитель: `max_concurrent: int | null`, `review_by` / `perimeter_review_by`: строка или список.
- `meta.watchdog = {reason: idle|looping, idle_s|repeats, tool_open}`; `meta.handoff = {from_task,
  mode: patch|in_place, apply_failed}`; `meta.fix_rounds = [{round, trigger, stage, verdict, usage}]`;
  `meta.review_stages = [{by, verdict, task_id, round}]`; `meta.diff_stat = {files, added, removed}`.
- `mcp_responses.jsonl`: `{ts, tool, task_id, bytes, verbose}`.

## What Goes Where
- Implementation Steps 1–9: код, тесты, `CLAUDE.md`, `README.md`, `docs/architecture.md`, конфиг пакета.
- 10–11 делает ведущая сессия: файлы вне репо (`~/.claude/skills/plan-dispatch`, `~/.claude/CLAUDE.md`,
  `~/.config/agent-dispatch/config.yaml`), закрытые песочницей исполнителей.

## Implementation Steps

### Task 1: 429 ConcurrencyLimitModel не выводит всю группу copilot-x5 на час

**Files:**
- Modify: `agent_dispatch/executors/health.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Create: `tests/fixtures/omp/x5_concurrency_429.ndjson`
- Create: `tests/test_concurrency_cooldown.py`

Живой случай 2026-11-10 12:19 (второй демон, задача `bb0d5030`): GLM получила
`429 {"detail":{"error":"ConcurrencyLimitModel",...}}`, `health.py:99` (`\b429\b`) счёл это квотой,
и `limit_group` `omp:copilot-x5` (x5-code-large, x5-large, x5-small) ушла на `quota_cooldown_seconds`
(3600 с). Пять задач x5-large упали «became unavailable while waiting for a slot», хотя у x5-large свой
лимит модели. Так одна загруженная GLM на час выключает весь корпоративный периметр, и работа идёт к
sol и opus.

- [x] тест: `ConcurrencyLimitModel` -> новый класс `model_busy`: остывает только этот исполнитель
      (не `limit_group`) на `routing.busy_cooldown_seconds` (по умолчанию 120)
- [x] тест: `Parallel processed requests limit exceed` (лимит на ключ) -> `model_busy` того же вида
- [x] тест: прочие 429 и квоты — прежнее поведение (группа, `parse_reset`, `recheck`)
- [x] тест: задачи других исполнителей группы, ждущие слот, не падают «became unavailable»
- [x] тест: `model_busy` без изменений эскалирует, с изменениями — `partial` (как сейчас для сбоя CLI)
- [x] реализовать; фикстура из живого текста ошибки
- [x] гейт зелёный

### Task 2: Метрика байтов ответа MCP для вызывающего

**Files:**
- Modify: `agent_dispatch/mcp/server.py`
- Create: `tests/test_mcp_response_bytes.py`

- [x] тест: `dispatch`, `dispatch_to`, `status`, `followup`, `route` пишут строку в
      `<data_dir>/mcp_responses.jsonl` (инструмент, `task_id`, байты текста ответа, `verbose`)
- [x] тест: ошибка записи (каталог только для чтения) не ломает ответ MCP
- [x] реализовать запись в процессе MCP
- [x] гейт зелёный

### Task 3: Сторож исполнителя: молчание и петли по потоку omp

**Files:**
- Modify: `agent_dispatch/executors/process.py`
- Modify: `agent_dispatch/executors/base.py`
- Modify: `agent_dispatch/executors/omp.py`
- Modify: `agent_dispatch/executors/result_parser.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Create: `tests/fixtures/omp/slow_progress.ndjson`
- Create: `tests/fixtures/omp/looping.ndjson`
- Create: `tests/fakes/omp_stream.sh`
- Create: `tests/test_watchdog.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_result_parser.py`

- [x] тест: `run_cli` вызывает `on_line` на каждую строку stdout, в том числе разрезанную чанками
- [x] тест: молчание дольше `idle_timeout_seconds` без открытого инструмента -> `stalled: idle`;
      при открытом `tool_execution` убийство только после `watchdog_tool_idle_seconds`
- [x] тест: `watchdog_repeat_limit` одинаковых «инструмент + аргументы + результат» подряд ->
      `stalled: looping`; одинаковая команда с разным результатом — не петля
- [x] тест (регрессия c0121093): поток `slow_progress.ndjson` с ходами по ~11 с не убивается
- [x] тест: `classify_internal_failure` даёт `stream_stalled` для обеих причин,
      `classify_failure` остывания не включает
- [x] тест: настроенные `watchdog_interval_seconds`, `watchdog_tool_idle_seconds`,
      `watchdog_repeat_limit` доходят до запуска через `Dispatcher._build_context` -> `RunContext`
      (сквозной dispatch с фейковым omp, а не только адаптер)
- [x] обновить `tests/test_config.py` (дефолт idle 300) и `tests/test_result_parser.py` (текст
      `stalled: idle|looping`)
- [x] реализовать: хук `on_line`, инкрементальный разбор omp, `meta.watchdog`, ключи конфига,
      проводка в `_build_context`
- [x] гейт зелёный

### Task 4: Передача начатой работы следующему звену

**Files:**
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/dispatch/escalation.py`
- Modify: `agent_dispatch/dispatch/task_package.py`
- Modify: `agent_dispatch/executors/worktree.py`
- Create: `agent_dispatch/templates/handoff_continue.md.j2`
- Create: `tests/test_handoff.py`

- [x] тест: worktree, таймаут с непустым `changed_files` и следующим звеном -> звено стартует с
      патчем предка (`meta.handoff.mode: patch`), Task Package с «продолжи начатое» и итогом попытки
- [x] тест: in_place — то же без патча (`mode: in_place`), начатая работа на месте
- [x] тест: `stalled` с изменениями ведёт себя так же
- [x] тест: цепочка из трёх звеньев — каждое получает патч только непосредственного предка
- [x] тест: нет следующего звена (`allow_escalation: false`, конец цепочки, периметр, бюджет) —
      результат прежний: `partial`, `error: timeout`
- [x] тест: патч не лёг -> звено с HEAD и `meta.handoff.apply_failed`
- [x] тест: наследник принял патч и ничего не правил -> `changed_files` = файлы предка (снимок
      адаптера видит только правки после старта, `base.py:111-137`), поэтому `_wants_review` и
      ступени ревью срабатывают; наследник правил часть файлов -> объединение
- [x] тест: требование периметра и `prod` линии сохраняются
- [x] реализовать; работа интегрируется один раз
- [x] гейт зелёный

### Task 5: Раунд правки внутри запуска

**Files:**
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/dispatch/escalation.py`
- Modify: `agent_dispatch/dispatch/task_package.py`
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Create: `agent_dispatch/templates/review_fix.md.j2`
- Create: `tests/test_review_fix_round.py`

- [x] тест: ревью `fail` -> продолжение сессии того же исполнителя в том же каталоге с замечаниями,
      затем verify и повторное ревью; `pass` -> `completed`, `meta.fix_rounds`, без эскалации
- [x] тест: после раунда сброшены `verification` и `meta.review`, `changed_files` объединены,
      `usage` сложен; `meta.fix_rounds[i].usage` хранит расход самого раунда до сложения
- [x] тест: повторный провал -> эскалация `review_failed`, замечания обоих раундов в `context`
- [x] тест: `verification_failed` -> раунд с выводом упавшей команды (вывод не в `error`)
- [x] тест: `review_fix_rounds: 0` — прежнее поведение; `skipped`/`unknown` раунда не дают
- [x] тест: `_budget_stop` перед раундом; `prod` добавляет «Prod data rule» в шаблон; отмена родителя
      прерывает раунд
- [x] реализовать по образцу `_recover_report`
- [x] гейт зелёный

### Task 6: Лимит параллельности по исполнителю

**Files:**
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Create: `tests/test_executor_concurrency.py`

- [x] время жизни слота исполнителя: только на время запуска CLI (`_run_executor`), освобождается
      до ожидания ревью-потомков и раундов; вложенная задача (`hop>0`) того же исполнителя идёт в
      счёт слота предка и свой не берёт, иначе `max_concurrent: 1` взаимно блокирует родителя и
      ребёнка; ревью берёт слот своего ревьюера
- [x] лимит группы настраивается по группе: `limit_groups.<группа>.max_concurrent` поверх
      `server.max_concurrent_per_group` (для `omp:copilot-x5` у пользователя 6), иначе x5-large с
      `max_concurrent: 4` упрётся в общий лимит группы 2
- [x] тест: саморевью при `max_concurrent: 1` запускается (не `skipped` по сроку)
- [x] тест: `limit_groups.<группа>.max_concurrent` заменяет общий лимит группы
- [x] тест: `max_concurrent: 1` держит вторую корневую задачу в очереди при свободной группе
- [x] тест: лимит действует и на ревью, и на `hop>0` (без слота группы у них, без взаимной блокировки
      родитель/ребёнок)
- [x] тест: `queue_fallback` не выбирает исполнителя с занятым `max_concurrent`
- [x] тест: без `max_concurrent` — прежнее поведение; событие `slot` пишет ожидание исполнителя;
      валидация `>= 1`
- [x] реализовать
- [x] гейт зелёный

### Task 7: Ступенчатое ревью: бесплатная модель до старшей

**Files:**
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/telemetry/storage.py`
- Modify: `agent_dispatch/routing/guards.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Create: `tests/test_review_stages.py`

- [x] тест: `review_by: [A, B]` — A, при `pass` B; `meta.review_stages`, `meta.review` = итог последней
- [x] тест: `fail` A -> раунд правки (задача 5) -> повтор A; B не вызывается до `pass` A
- [x] тест: раунды исчерпаны на A -> эскалация `review_failed`, B не вызывался
- [x] тест: `fail` B -> раунд, повтор только B (A не перепроверяет)
- [x] тест: `skipped`/`unknown` на A передаёт работу на B
- [x] тест: ревью-потомки не входят в `max_children` (`count_children` их исключает); прочие дети как
      раньше
- [x] тест (интеграционный): у родителя `max_children` обычных детей, автоматическое ревью всё равно
      допускается (исключение в `routing/guards.py:55` для `_review_work`)
- [x] тест: периметр — ступени из `perimeter_review_by`, внешние не вызываются и не называются;
      `validate_references` требует corporate у каждой ступени периметра; строка — прежнее
      поведение; список без повторов и без себя в `review_by`
- [x] реализовать
- [x] гейт зелёный

### Task 8: Счётчики replay

**Files:**
- Modify: `evals/replay/harness.py`
- Modify: `evals/replay/__main__.py`
- Modify: `tests/test_replay.py`

- [x] `caller_bytes`: байты MCP на корневую задачу (медиана, p90, по исполнителю) из
      `mcp_responses.jsonl`
- [x] `watchdog` (idle / looping, с открытым инструментом или без) и `handoff` (передано, дошло до
      `completed`)
- [x] `fix_round` (recovered / failed / skipped, цена раунда против цены звена эскалации)
- [x] `review_stages` (pass/fail по ступени; доля работ, остановленных бесплатной ступенью; доля `fail`
      у платной ступени среди дошедших до неё — против базы 43%)
- [x] тесты на каждый счётчик; гейт зелёный

### Task 9: diff_stat в компактном ответе

**Files:**
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/executors/workspace.py`
- Modify: `agent_dispatch/mcp/server.py`
- Create: `tests/test_diff_stat.py`

- [x] тест: `meta.diff_stat` по `changed_files` в worktree и in_place (правки вызывающего не в счёт)
- [x] тест: компактный ответ печатает `diff_stat` одной строкой; замечания ревью до 1500 символов
- [x] реализовать; гейт зелёный

### Task 10: Документация и правила
- [x] `CLAUDE.md`: сторож, передача работы, раунд правки, лимит исполнителя, ступенчатое ревью,
      ревью-потомки вне `max_children`, `mcp_responses.jsonl`, `diff_stat`; заменить правило
      «Таймаут с непустым changed_files — partial, эскалации нет»
- [x] `README.md`, `docs/architecture.md`
- [x] гейт зелёный

### Task 11: plan-dispatch: Claude только на сложных частях (ведущая сессия)
- [x] strong-задачи делятся на «скелет» (claude/opus: контракты, сигнатуры, тесты-заготовки) и
      «реализацию по скелету» (sol / x5 со ступенчатым ревью)
- [x] claude/opus как исполнитель только для задач `judgment` (это решение ведущей сессии; гарантию
      для эскалаций даёт задача 12)
- [x] финальное ревью плана: sol всегда, opus только при изменении контрактов
- [x] ширина волны из `max_concurrent`; `**Files:**` обязателен
- [x] тесты `scripts/plan.py`

### Task 12: Правило чтения дифа и пользовательский конфиг (ведущая сессия)
- [x] сначала переустановить uv tool (`uv tool install --reinstall .`) и перезапустить демон: без этого
      установленная копия отвергнет новые ключи конфига (`review_by` списком, `max_concurrent`,
      `limit_groups`), правило `CLAUDE.md`
- [x] `~/.claude/CLAUDE.md` (утверждено владельцем 2026-11-10): полный диф для strong, контрактов,
      ревью fail / unknown / skipped, задач без verify; иначе `diff_stat` + вердикт + замечания
- [x] цепочки эскалации пользователя: `claude/opus` убрать из хвоста обычных цепочек (sol, x5);
      последним звеном — sol с высоким уровнем рассуждений (новый исполнитель `omp/sol-high`,
      `--thinking high`); opus остаётся только для задач, которые роутер или ведущая сессия явно
      отдают на strong/judgment
- [x] `~/.config/agent-dispatch/config.yaml`: ступени `review_by` (x5-code-large ->
      `[omp/x5-large, omp/sol]`, x5-large -> `[omp/x5-code-large, omp/sol]`), `max_concurrent`
      (x5-large 4, x5-code-large 2), включение x5 по итогам повтора

### Task 13: Verify acceptance criteria
- [ ] критерии из раздела Acceptance criteria проверены на фикстурах
- [ ] полный гейт; smoke на втором демоне; повтор задач с провалом GLM с новыми механизмами
- [ ] `uv run python -m evals.replay` показывает новые счётчики
- [ ] план в `docs/plans/completed/`

## Post-Completion
- Переустановка и перезапуск — первый шаг задачи 12 (до правки пользовательского конфига).
- Через неделю: `evals.replay` по критериям приёмки; решения по `review_fix_rounds`, ступеням ревью.
