# Доска задач Kaiten в agterm (задачи 4 и 5 из 20261115-sadensmol-adoption.md)

Заменяет `20261115-task-entry.md` (`/task`). Ревью 2026-11-15: plan-review, omp/terra (2 раунда),
Fable (2 раунда). Исполнение: задачи сгруппированы по репам, и перед стартом каждая группа
переносится в `docs/plans/` своей репы: plan-dispatch работает в одной репе и принимает
пути только относительно её корня.

## Overview

Поток работы с задачами, где пользователь отвечает в Kaiten:

1. Ручной `scan` локально собирает контекст и черновик плана по каждой карточке в TO DO.
2. Доска в agterm показывает готовность.
3. Клик открывает сессию планирования, в ней закрываются вопросы.
4. Утверждённый план загружается в карточку Kaiten (только он и только после
   утверждения), карточка переходит в «Готово к выполнению».
5. Пользователь сам двигает задачу на исполнение: создаётся worktree с планом, карточка в
   Kaiten переходит в IN PROGRESS, запускается plan-dispatch.
6. Успешно созданный MR переводит карточку в READY FOR REVIEW и там, и там.

Агент автономно выполняет только код в репах пользователя. Шаги с внешним
взаимодействием (вики, «спросить у кого-то», согласования) делает пользователь: они
помечены `[я]` и не исполняются.

## Context (живые данные и код 2026-11-15)

- Клиент Kaiten `~/x5/git_reps/salt-qa-agent-kit/skills/kaiten_cards/client.py`. Есть
  `make_client` (:698), `my_tasks` (:645), `column_cards` (:678), `_paginate_cards` (:626),
  `get_card` (:411), `add_comment` (:419). Запись разрешена только после того, как человек
  увидел текст (:419-425), сброс кеша (:428-432). `_get`/`_post` (:326/:375): `_post` назван
  единственным путём записи. Фильтры `board_id`, `column_id`, `member_ids` есть в allow-list.
  `compact()` (:261) выбрасывает `members[].type`, `properties`, `created`, `comments_total`,
  `uid`, а `card()` (:471) не отдаёт files, children, parents. Поэтому новые функции
  работают с сырыми карточками. `files` приходит только в одиночном GET.
- Токен: `salt-qa-agent-kit/config.yaml` (gitignored), загружает `skills.config.apply_to_environ`.
- Ответственный: `members[].type == 2`, сравнение `user_id` с `current_user_id()` (6156).
  Приоритет: `properties.id_3` — список (например `[7]`), значение берётся из первого
  элемента: 5 Критический, 6 Высокий, 7 Средний, 8 Низкий. Сервис: `properties.id_2723`
  (строка, у части карточек отсутствует; тогда репу выбирает пользователь в сессии
  планирования, а готовность показывает «репа не найдена»).
- Доска «QA Automation Board» (`board_id` 10857), колонки: 53672 TO DO, 53673 IN PROGRESS,
  72352 REVIEW, 72346 READY FOR REVIEW, 53674 DONE, 130467 BACKLOG, 131976 CANCELED.
  Дорожка одна (22977).
- **Спайк файлов на тестовой карточке 3920127 (2026-11-15):**
  - `POST /api/latest/cards/{uid}/files` вернул 404: защищённого роута на X5 нет.
  - `PUT /api/latest/cards/{id}/files` (multipart `file`, этим же роутом пользуется
    веб-интерфейс X5) вернул 200 с заголовками `Deprecation`,
    `Sunset: Tue, 02 Feb 2027` и `Link` на миграцию.
  - В ответе пришли `id`, `uid`, `name`, `url` (`https://kaiten-files.x5.ru/<uuid>.md`),
    `author_id`, `size`, `deleted`.
  - Ссылка `url` открывается **без авторизации**. Так устроены все файлы, которые
    загружают через UI X5.
  - `DELETE /api/latest/cards/{id}/files/{file_id}` вернул 200, после него ссылка отдаёт 404.
  - Тестовый файл удалён, карточка пуста.
- `PATCH /cards/{id}` с `column_id` подтверждён документацией.
- agterm:
  - Страница с `--js` может вызвать `session.new` (`~/.claude/skills/agterm/reference.md:1036-1046`).
  - Файлы страница читает только с `--cwd DIR`, и html должен лежать внутри DIR (:998-1001).
  - `--command` выполняется без shell, с GUI PATH (:463-469): нужен абсолютный путь, как
    в `agterm-stage:358`.
  - Есть `session overlay reload` (:1113).
  - PostToolUse-хук `agterm-stage` распознаёт `glab mr create` (`agterm-stage:66`,
    `tests/stages.bats:91`).
  - `wt` создаёт worktree от базовой ветки и копирует только `.venv`, `node_modules`,
    `.claude/settings.local.json` (`wt/wt.zsh:452-477`).
- plan-dispatch перенесён в git-репу `~/my_git_reps/plan-dispatch`, а
  `~/.claude/skills/plan-dispatch` теперь симлинк на неё (коммит dc759a3, 64 теста зелёные,
  бэкап в `/tmp/plan-dispatch-backup/`).
  - `plan.py` разбирает `## Implementation Steps` + `### Task N:` и отдельной строкой
    `**Files:**` со строками `- Create|Modify|Delete: <путь от корня>`; абсолютные пути
    отвергает (`scripts/plan.py:30-36,87`).
  - `pending`/`remaining` (:564) и `unparsed_pending` считают все `[ ]`.
  - `plan.py branch` выводит ветку из имени файла (:428).
- `claude -p`: флаги `--permission-mode dontAsk`, `--strict-mcp-config`, `--tools`,
  `--json-schema`, `--output-format json`, `--max-budget-usd` существуют; `--max-turns` нет
  (`claude --help`, 2026-11-15).
- agterm-kit работает на системном `python3` (Makefile:12). `make test` запускает только
  bats (Makefile:3), Python-тесты оборачиваются в bats (`tests/themes.bats:4`), страница
  тестируется через node (`tests/stages.bats:182`).

## Колонки доски

| Колонка agterm | Kaiten | Кто двигает |
|---|---|---|
| TO DO ⇄ | TO DO | `scan` |
| Сбор данных | (TO DO) | `scan`, пока идёт сбор |
| Оценка готовности | (TO DO) | `scan` после сбора |
| Готово к выполнению | (TO DO) | `approve` |
| IN PROGRESS ⇄ | IN PROGRESS | пользователь, кнопка «На исполнение» (`start`) |
| READY FOR REVIEW ⇄ | READY FOR REVIEW | хук после успешного `glab mr create` |

Состояние отслеживаемых карточек задаёт Kaiten. Каждый `scan` перечитывает их через GET,
при этом **в Kaiten `scan` не пишет никогда**:
- колонка ⇄ в Kaiten задаёт колонку на доске, ручной перенос в Kaiten подхватывается;
- карточка, которая ушла с доски 10857, оказалась в REVIEW, DONE, CANCELED или BACKLOG,
  сменила ответственного или архивирована, удаляется с доски;
- возврат IN PROGRESS → TO DO возвращает карточку в «Оценку готовности» с сохранённым планом.

## Решения

- Запись в Kaiten выполняют только явные действия пользователя: `approve`, `start`,
  успешный MR, кнопка «Повторить». `scan` в Kaiten не пишет.
- `context.md` в Kaiten не выгружается, в нём код сервиса.
- Kaiten читает код. Claude получает готовые данные в промпте и доступа к Kaiten не имеет.
  Файлы пишет код из JSON-ответа.
- План загружается роутом веб-интерфейса (`PUT /api/latest/cards/{id}/files`). Ссылка на
  файл открывается без авторизации, как у любых файлов, загруженных через UI X5. Поэтому в
  план идут только шаги и вопросы, без кода, логов и секретов; перед загрузкой текст проходит
  маскирование секретов. Роут отключат 2027-02-02 (`docs/backlog/kaiten-files-sunset.md`).
- У плана в каждый момент один изменяемый файл. До `start` это
  `state/<card>/plan.approved.md`: его пишет сессия планирования, и только его загружает
  `approve`, а в основную копию репы ничего не пишется. После `start` это
  `<worktree>/docs/plans/<дата>-kaiten-<card>.md`: `start` переносит файл туда, и state
  хранит путь. Версия в Kaiten — неизменяемый снимок утверждения (`v<N>`), прогресс
  чекбоксов туда не синхронизируется. Повторное утверждение (вернули в TO DO) загружает `v<N+1>`.
- Формат плана проверяет `plan.py lint`, своего валидатора нет. Метки `[агент]`/`[я]`
  разбирает `plan.py`.
- Оценка лимитов в v1: число задач `[агент]` × `cost_per_verified_success` исполнителя тира
  balanced из `evals.replay` плюс остаток agent-lb из `lb-limits`. Токены Claude появятся
  после retro (задача 3).
- Пачки (решение пользователя 2026-11-16). Карточки, которые правят одно место, делаются
  одной задачей. Живой пример: 3821478 и 2744704 имеют общего родителя 3821764 и сервис
  `notification-manager`.
  - Кандидатов находит код среди карточек пользователя в TO DO (не среди всех
    отслеживаемых): одинаковый `[Salt] Service`, то есть одна репа. Общий родитель лишь
    усиливает связь; карточки разных сервисов не склеиваются, потому что задача на
    несколько реп запрещена. Карточки без сервиса в пачки не попадают.
  - Правят ли они одно место, решает один вызов Claude на всю пачку (это ещё и экономит
    токены). Его ответ `bundle: {primary, members, reason}` — только предложение.
  - Пачку подтверждает пользователь в сессии планирования.
  - Главная карточка: выше приоритет, при равенстве та, что раньше создана.
  - План один и загружается в главную карточку. В остальные идёт комментарий
    `add_comment` «делается вместе с #<главная>, план там».
  - Приоритет пачки — максимальный из участников.
  - Колонки ⇄ двигают в Kaiten все карточки пачки, MR ссылается на все.

## Implementation Steps

### Task 1: kaiten-клиент и API для taskboard (salt-qa-agent-kit)
**Files:**
- Modify: `skills/kaiten_cards/client.py`
- Create: `skills/kaiten_cards/taskboard_api.py`
- Create: `tests/test_kaiten_taskboard.py`

- [ ] `my_todo(board_id, column_id)`: работает поверх `_paginate_cards`, серверный фильтр `member_ids`, отдаёт сырые карточки, где пользователь ответственный. Сортировка: Критический → Высокий → Средний → Низкий → без приоритета, затем `created`
- [ ] `move_card(card_id, column_id, board_id)`: сначала проверка, что карточка на `board_id`, затем PATCH и сброс кеша `/cards/{id}`
- [ ] `upload_plan(card_id, path, version)`: имя `taskboard-plan-<card>-v<N>-<hash8>.md`, PUT, после успеха удаление прежних файлов `taskboard-plan-<card>-*`, у которых `author_id` совпадает с текущим пользователем. Остальные файлы не трогаются. Заголовки `Deprecation`/`Sunset` пишутся в результат, ошибкой не считаются. Сброс кеша
- [ ] Неизвестный исход (таймаут PUT или DELETE): повторный GET. После таймаута PUT новый файл ищется по уникальному имени с хешем: есть — загрузка состоялась, нет — ошибка, прежняя версия не тронута. После таймаута DELETE старая версия ищется по её имени: нет — удалена, есть — остаётся в `pending_write` и удаляется при `retry` или следующем `approve`
- [ ] `_patch`, `_delete` и multipart в том же узком месте записи, что `_post`
- [ ] `taskboard_api.py`: CLI с JSON на stdout (`todo`, `card <id>` с parents/children/files, `move`, `upload-plan`, `comment`) для подпроцесса agterm-kit
- [ ] `comment <card> <текст>`: в конце текста скрытый маркер `taskboard:<ключ>`; перед записью и после таймаута GET комментариев ищет маркер, поэтому повтор не создаёт дубль; согласие пользователя даёт вызывающий (approve), API это документирует
- [ ] Тесты с замоканным HTTP: ответственный, сортировка, без приоритета, пагинация, move на чужую доску отвергается, upload → delete старых только своих `taskboard-plan-*`, сбой PUT сохраняет прежнюю версию, таймаут PUT с файлом на сервере → успех без дубля, чужой файл и ручной `plan.md` пользователя не удаляются
- [ ] Прогнать тесты кита, должны пройти

### Task 2: plan.py, метки [агент]/[я] и docs последними (plan-dispatch)
**Files:**
- Modify: `scripts/plan.py`
- Modify: `scripts/test_plan.py`
- Modify: `SKILL.md`

- [ ] Заголовок `### Task N: [я] …` разбирается как задача `manual`: она не раздаётся и не входит в `pending`, `remaining` и `unparsed_pending`. `**Files:**` для неё необязателен, `plan.py lint` его не требует (внешний шаг путей в репе не имеет)
- [ ] Задача `[агент]`, которая зависит от открытой `[я]`, не раздаётся, причина видна: «ждёт [я] Task N»
- [ ] План считается выполненным, когда закрыты все задачи `[агент]`. Открытые `[я]` перечисляются в итоговом отчёте
- [ ] Задачи, у которых в `**Files:**` только docs (`*.md`, `docs/`), откладываются в последнюю волну (задача 5). Исключение: если от docs-задачи зависит кодовая задача, порядок задают зависимости и docs-задача не откладывается. Тест на этот случай
- [ ] Тесты: смешанный план, зависимость `[агент]` от `[я]`, план без `[агент]`, docs последними, старые планы без меток не меняют поведение
- [ ] Прогнать `python3 -m pytest scripts/test_plan.py`, должны пройти

### Task 3: состояние и CLI (agterm-kit)
**Files:**
- Create: `taskboard/agterm-taskboard`
- Create: `taskboard/taskboard.conf.example`
- Create: `tests/taskboard.bats`
- Create: `tests/taskboard_test.py`
- Modify: `Makefile`
- Modify: `install.sh`
- Modify: `README.md`

- [ ] Конфиг `~/.config/agterm-kit/taskboard.conf`: путь к киту, `board_id`, id колонок, корни реп для поиска сервиса. При старте id колонок сверяются с `GET /boards/{id}`
- [ ] Kaiten вызывается подпроцессом `<kit>/.venv/bin/python -m skills.kaiten_cards.taskboard_api`, сам agterm-kit остаётся на stdlib
- [ ] Состояние `~/.local/state/agterm-kit/taskboard/<card>/`: `state.json` (колонка, `fingerprint`, `session_id`, `plan_path`, `plan_hash`, `version`, `branch`, `worktree`, `repo_common_dir`, `pending_write`, готовность, ошибка) и `context.md`, `plan.md` (черновик до утверждения)
- [ ] `fingerprint` = хеш `description`, `comments_total`, `column_id`, `properties`, ответственного, id и `updated` связанных карточек, имён файлов за вычетом `taskboard-plan-*`. HEAD репы не входит. Снимается повторным GET после своих записей. Обновить вручную: `scan --card <id>`
- [ ] Команды:
  - `scan [--max 5] [--card <id>]`: только чтение Kaiten;
  - `approve <card>`: `plan.py lint` для `plan.approved.md`, `upload_plan`, «Готово к выполнению»;
  - `start <card>`: worktree и ветка `kaiten-<card>` через `wt -a none` (по умолчанию `wt` запускает claude, `wt/wt.zsh:15,482-508`); `plan.approved.md` переносится в `<worktree>/docs/plans/<дата>-kaiten-<card>.md` и коммитится на ветку (в state остаётся только путь); `move_card` IN PROGRESS; запись `branch`, `worktree`, `repo_common_dir`;
  - `move <card> <колонка>`, `retry <card>` (повтор `pending_write`), `open <card>`, `board`
- [ ] Сбой записи в Kaiten ставит `pending_write`. `scan` его только показывает, повтор выполняет `retry` или кнопка доски
- [ ] Тесты:
  - переходы колонок и синхронизация по GET (каждое правило из «Колонок доски»);
  - `scan` не пишет в Kaiten (фейковый API падает на любой записи);
  - кеш: `scan` → своя запись → `scan` не вызывает claude;
  - изменение связанной карточки сбрасывает кеш;
  - сбой Kaiten без частичной записи состояния, `pending_write` и `retry`;
  - `start` кладёт план в worktree, проверка колонок при старте
- [ ] `make lint` (`ast.parse` для `taskboard/agterm-taskboard`), bats-обёртка Python-тестов
- [ ] Прогнать `make test`, должны пройти

### Task 4: сбор и черновик плана через Claude (agterm-kit)
**Files:**
- Modify: `taskboard/agterm-taskboard`
- Create: `taskboard/prompts/prep.md`
- Create: `taskboard/schema/prep.json`
- Modify: `tests/taskboard_test.py`

- [ ] Код собирает данные через `taskboard_api card`: описание, комментарии, связанные карточки, имена файлов, путь к репе сервиса
- [ ] Вызов: `claude -p --model opus --permission-mode dontAsk --strict-mcp-config --tools Read,Grep,Glob --max-budget-usd 2 --output-format json --json-schema <schema>`, cwd = репа сервиса, env без `CLAUDECODE`/`CLAUDE_CODE_*`, таймаут 15 мин. Карточки идут последовательно, сначала Критический и Высокий, не больше `--max`
- [ ] Ответ `{context_md, plan_md}`. Черновик пишется в state и проверяется `plan.py lint`; провал даёт бейдж «сбор упал: <причина>»
- [ ] Промпт:
  - данные карточки помечены как недоверенный ввод;
  - формат как у /planning:make: `## Implementation Steps`, `### Task N: [агент] …` или `### Task N: [я] …`, `**Files:**` отдельной строкой со строками `- Create|Modify|Delete:` и путями от корня репы;
  - `[агент]` только для кода в репах пользователя;
  - `[я]` для вики, согласований и вопросов людям;
  - вопросы к пользователю записываются как `❓`;
  - в плане нет кода, логов и секретов
- [ ] Готовность считается кодом: описание заполнено, репа существует, есть задача `[агент]`, нет открытых `❓`. Бейдж вида `3/4 · ❓2`. Если все задачи `[я]`, бейдж «не для агента»
- [ ] Тесты: разбор JSON-ответа, `plan.py lint` на хорошем и плохом черновике, готовность, кеш с фейковым `claude` в PATH, env без `CLAUDE_CODE_*`
- [ ] Прогнать `make test`, должны пройти

### Task 5: доска (agterm-kit)
**Files:**
- Create: `taskboard/board.html`
- Modify: `tests/taskboard.bats`

- [ ] Каждая команда копирует `board.html` в state-каталог и пишет рядом `board.json`. Overlay открывается с `--cwd` на state-каталог, после изменений идёт `session.overlay.reload`
- [ ] 6 колонок, у синхронизируемых в подписи `⇄`. Карточка показывает приоритет, бейдж готовности, число `❓`, оценку $, список задач `[я]` и `pending_write` с кнопкой «Повторить»
- [ ] Клик в «Оценке готовности»: при живом `session_id` вызывается `session.select`, иначе `session.new` с `cwd` = репа сервиса и абсолютной командой `<…>/agterm-taskboard open <card>`
- [ ] Кнопка «На исполнение» в «Готово к выполнению»: `session.new` с абсолютной командой `agterm-taskboard start <card>`, затем сессия plan-dispatch в worktree
- [ ] Тест рендера через node на фикстуре `board.json`, как `stages.bats:182`
- [ ] Прогнать `make test`, должны пройти

### Task 6: сессия планирования и переход по MR (agterm-kit)
**Files:**
- Modify: `taskboard/agterm-taskboard`
- Modify: `stages/agterm-stage`
- Modify: `tests/stages.bats`
- Modify: `tests/taskboard.bats`

- [ ] `open <card>`: `claude --permission-mode plan` в репе сервиса с `context.md` и `plan.md`. По утверждении план пишется в `state/<card>/plan.approved.md` (не в репу), затем вызывается `agterm-taskboard approve <card>`
- [ ] Хук `agterm-stage` после `glab mr create` срабатывает только на событие `PostToolUse` с URL MR (`https://…/merge_requests/<N>`) в `tool_response.stdout`, и только если `git common dir` и ветка совпадают с `repo_common_dir`/`branch` в состоянии. Тогда вызывается `agterm-taskboard move <card> ready_for_review`. Ошибка ставит `pending_write`
  Кода выхода в payload `PostToolUse` нет: упавший вызов приходит событием `PostToolUseFailure`, которое хук не слушает, а в песочнице упавший `glab` может прийти как `PostToolUse` с кодом 0 (`agterm-stage:313`). Поэтому признак успеха — только URL. Сейчас `auto_stage` (`agterm-stage:275-279`) `tool_response` не читает, хук нужно научить этому
- [ ] Тесты хука: `PostToolUse` с URL MR на ветке с карточкой; `PostToolUse` без URL (упавший `glab` в песочнице); `PostToolUseFailure` игнорируется; та же ветка в другой репе; ошибка перемещения → `pending_write`
- [ ] Прогнать `make test`, должны пройти

### Task 7: eval и документация (agterm-kit)
**Files:**
- Modify: `README.md`
- Create: `taskboard/EVAL.md`

- [ ] Базовая линия до внедрения: на двух текущих карточках пользователь ведёт ручной сценарий (kaiten-скилл → planning:make → plan-dispatch). Считается число его сообщений до утверждённого плана и токены сессии
- [ ] Ручной eval: `scan` на текущих карточках TO DO, пользователь оценивает каждый черновик. Порог: 2 из 3 пригодны после одного раунда вопросов, и **ни одна** задача с внешним взаимодействием не получила `[агент]` (safety-gate). Записать цену `scan` на карточку
- [ ] README taskboard; ручной шаг пользователя: строка в `~/.claude/CLAUDE.md` «в планах с метками `[агент]`/`[я]` исполняются только `[агент]`» (песочница запрещает агенту эту правку)

### Task 8: пачки связанных карточек (agterm-kit)
**Depends on:** Task 3, Task 4, Task 5, Task 6
**Files:**
- Modify: `taskboard/agterm-taskboard`
- Modify: `taskboard/prompts/prep.md`
- Modify: `taskboard/schema/prep.json`
- Modify: `taskboard/board.html`
- Modify: `stages/agterm-stage`
- Modify: `tests/taskboard_test.py`
- Modify: `tests/taskboard.bats`
- Modify: `tests/stages.bats`

- [ ] `scan`: кандидаты в пачку детерминированно, без LLM: карточки в колонке TO DO с одинаковым `properties.id_2723` (одна репа); общий `parents_ids` пишется в `reason`, но без общего сервиса не склеивает
- [ ] Сбор идёт одним вызовом Claude на кандидата-пачку; схема ответа `{context_md, plan_md, bundle: {members, primary, reason} | null}`; `bundle` — предложение, `state.json` хранит его как `proposed_bundle`, при `null` карточки собираются по отдельности
- [ ] `open`: сессия планирования показывает предложение пачки; подтверждение пишет `bundle` в состояние главной и `bundle_of` в остальные; при отказе общий черновик остаётся у главной, а остальные карточки получают состояние «не собрано» и собираются по отдельности (`scan --card`), без копий общего плана
- [ ] Главная: выше приоритет, при равенстве раньше `created`; приоритет пачки — максимальный
- [ ] `approve` пачки: `upload_plan` в главную, комментарий через `taskboard_api comment` «делается вместе с #<главная>, план там» в остальные; записи в Kaiten идут только при `approve`, текст комментария показывается пользователю в сессии до подтверждения
- [ ] `move`/`start`/хук MR двигают все карточки пачки; частичный сбой ставит `pending_write` по каждой карточке
- [ ] Доска: пачка — одна карточка с бейджем `+N связанных` и списком номеров
- [ ] Синхронизация: участник, ушедший из ⇄ колонок в Kaiten, выходит из пачки с пометкой. Если ушла главная, пачка распускается: план и состояние исполнения остаются у главной, остальные теряют `bundle_of`, возвращаются в «Оценку готовности» с пометкой «пачка распущена» и требуют индивидуального сбора
- [ ] Тесты: поиск кандидатов (общий сервис в TO DO, общий родитель без общего сервиса не склеивает, карточки вне TO DO и без сервиса не входят), выбор главной, отказ от пачки (остальные «не собрано»), уход главной (роспуск с сохранением плана), approve пачки (upload в главную, комментарий в остальные, без записи до approve), перенос всех карточек, частичный сбой, рендер бейджа
- [ ] Прогнать `make test`, должны пройти

## Не делаем

Загрузку `context.md` в Kaiten, запись в Kaiten из `scan`, автоматический `scan`,
автоматический перевод в IN PROGRESS, другие доски Kaiten, GitLab issues, несколько реп на
задачу (`docs/backlog/task-multi-repo.md`), токены Claude в оценке до retro.
