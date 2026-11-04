# Прод: задача с `prod: true` целиком остаётся у корпоративных исполнителей

## Overview

Решение владельца (2026-11-03): продовые системы трогает только omp с моделью x5.
Менеджер (Opus или GPT) ставит такую задачу через AgentDispatch с флагом `prod: true`,
и тогда:

- вся цепочка остаётся у исполнителей `corporate: true`: выбор, fallback, эскалация,
  переадресация, follow-up и вложенные задачи. Это действует и при
  `routing.corporate_perimeter: false`: у владельца он выключен, а прод должен быть закрыт
  всегда;
- ревью `review_by` идёт не внешнему ревьюеру, а корпоративному: новый ключ исполнителя
  `perimeter_review_by`. Он может указывать на самого исполнителя: ревью делает второй
  экземпляр x5, в режиме только чтения и с чистым контекстом;
- исполнитель отвечает выводами без сырых данных: Task Package добавляет правило данных,
  а отчёт проходит детерминированную очистку от явных секретов.

Измеримый результат: в `evals.replay` у задач с `prod` и их потомков `perimeter_violations`
равно 0. Новая секция `prod` показывает, сколько продовых задач было, у кого и с каким
ревью.

## Context

- Механизм периметра уже есть, его надо переиспользовать, а не дублировать:
  - `routing/capability.py`: `perimeter_applies`, `perimeter_required`;
  - `Dispatcher._lineage_perimeter`: требование предка по `escalated_from`, `followup_of`,
    `parent_task_id` переживает рестарт демона;
  - `Dispatcher._perimeter_refusal`: последний рубеж, отказ `guard: perimeter`;
  - `decision.meta.perimeter`: `answered` или `unknown`.

  Правила описаны в `CLAUDE.md`, пункты про периметр.
- `review_by` уже в коде (`Dispatcher._review_work`, `docs/plans/20261103-review-by.md`).
- Гейт: `uv run pytest tests -q`, `uv run ruff check . && uv run ruff format --check .`.

## Implementation Steps

### Task 1: флаг запроса `prod` закрепляет цепочку за периметром
**Files:**
- Modify: `agent_dispatch/models.py`, `agent_dispatch/dispatch/dispatcher.py`, `agent_dispatch/routing/capability.py`, `agent_dispatch/mcp/server.py`, `agent_dispatch/cli.py`
- Create: `tests/test_prod.py`
**Depends on:** none
**Complexity:** complex

- [x] `DispatchRequest.prod: bool = False`, параметр `prod` у MCP-инструментов `route`, `dispatch` и `dispatch_to`, а также флаг CLI `--prod` у команд, которые ставят задачу
- [x] Задача с `prod` требует периметра независимо от `routing.corporate_perimeter`. В `decision.meta.perimeter` пишется новое значение `prod`, и `_lineage_perimeter` его наследует всей линии: эскалации, follow-up, вложенным задачам, переадресации роутеру
- [x] Явный `executor` вне периметра у задачи с `prod` получает отказ `guard: perimeter` до запуска. Это отличие от обычной корневой задачи, где явный исполнитель — решение владельца данных: тут владелец уже решил
- [x] Если в конфиге нет ни одного включённого `corporate: true`, задача с `prod` получает отказ с понятной причиной, а не запуск снаружи
- [x] Вопрос Jev о корпоративных данных для `prod` не нужен: ответ уже известен
- [x] Тесты: роутер выбирает только корпоративных, эскалация не уходит наружу (цепочка `x5 → sol` пропускает sol), явный внешний исполнитель отказан, follow-up и вложенная задача наследуют, рестарт демона не снимает требование, `corporate_perimeter: false` не открывает прод

### Task 2: корпоративное ревью и правило данных
**Files:**
- Modify: `agent_dispatch/config.py`, `agent_dispatch/dispatch/dispatcher.py`, `agent_dispatch/dispatch/task_package.py`, `agent_dispatch/templates/task_package.md.j2`, `agent_dispatch/config_default.yaml`, `config.example.yaml`
- Create: `agent_dispatch/dispatch/redact.py`, `tests/test_redact.py`
- Modify: `tests/test_prod.py`, `tests/test_review_by.py`
**Depends on:** Task 1
**Complexity:** complex

- [ ] `ExecutorSettings.perimeter_review_by: str | None`. Проверка ссылки в `validate_references`; указать самого исполнителя разрешено, ревьюер обязан быть `corporate: true`
- [ ] `_review_work`: у задачи под периметром (своим или предка, в том числе `prod`) ревьюер — `perimeter_review_by`. Если его нет, итог `skipped` с причиной `perimeter: no corporate reviewer`, и внешний `review_by` не зовётся вовсе. Отказ guard'а тут не должен быть единственной защитой
- [ ] Task Package задачи под `prod`: раздел «Prod data rule» — отвечать выводами и цифрами, без строк таблиц, логов целиком, токенов, паролей, ключей и персональных данных. Раздел есть и у ревьюера
- [ ] `redact.py`: детерминированная очистка `summary` и `error` результата задачи под `prod` перед записью. Маскируются bearer и JWT-токены, `password=`/`token=`/`secret=` со значением, приватные ключи PEM, строки подключения с паролем (`схема://логин:пароль@хост`). Факт очистки и число замен пишутся в `meta.redacted`, без значений
- [ ] Task Package любой задачи (не только `prod`): правило «отказ инструмента — не препятствие для обхода». Если hook, guard или песочница отказали в вызове, исполнитель не ищет другой путь к тому же действию (другой инструмент, eval, скрипт в файле), а пишет отказ и его причину в отчёт. Живой случай 2026-11-04: omp, которому prod-guard запретил bash, выполнил те же команды через `eval`
- [ ] Тесты: ревьюер корпоративный (тот же x5), внешний не вызывается, `skipped` без корпоративного ревьюера, правило данных в промпте, правило об отказах инструментов в промпте задачи и ревью, очистка каждого вида секрета и отсутствие ложных срабатываний на обычном тексте и sha коммитов

### Task 3: replay, документация, конфиг
**Files:**
- Modify: `evals/replay/harness.py`, `tests/test_replay.py`, `CLAUDE.md`, `docs/architecture.md`, `evals/README.md`
**Depends on:** Task 2
**Complexity:** normal

- [ ] `evals.replay`: `perimeter_violations` учитывает требование `prod`. Новая секция `prod`: число продовых задач, исполнители, вердикты ревью, сработавшие очистки
- [ ] `CLAUDE.md`: правило про `prod` (всегда периметр, независимо от `corporate_perimeter`; корпоративное ревью; правило данных и очистка)
- [ ] `docs/architecture.md` и `evals/README.md`: то же для читателя

### Task 4: разбор отчёта omp не теряет выполненную работу
**Files:**
- Modify: `agent_dispatch/executors/result_parser.py`, `agent_dispatch/executors/omp.py`, `tests/test_result_parser.py`, `tests/test_adapter_omp.py`
**Depends on:** none
**Complexity:** normal

- [ ] `normalize`: поле `summary` блока результата, которое не строка (список, объект, число), приводится к строке (список строк через перевод строки, остальное через `json.dumps`), а не роняет разбор `ValidationError`. Живой случай 2026-11-04: ревью omp/sol вернуло `summary` списком и упало целиком
- [ ] То же для других строковых полей блока, которые модель может прислать не строкой (`error`, элементы `tests`), если их разбор устроен так же; без изменения схемы `ExecutionResult`
- [ ] `omp._parse_result`: ход с `stopReason: error` (обрыв транспорта, например `Codex websocket transport error`) при непустом `changed_files` даёт `partial` с той же строкой `error`, а не `failed`: работа в дереве есть, судит вызывающий по дифу (как таймаут с изменениями). Без изменений остаётся `failed`
- [ ] Остывание по такой ошибке не меняется: `health.classify_failure` по-прежнему видит строку CLI в `error`
- [ ] Тесты: `summary` списком и объектом, ошибка транспорта с изменениями и без

## Post-Completion

- Владелец ставит у `omp/x5-code-large` в своём конфиге `perimeter_review_by: omp/x5-code-large`.
- Переустановить tool и перезапустить демон.
