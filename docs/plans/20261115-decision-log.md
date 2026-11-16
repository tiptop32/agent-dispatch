# Журнал решений (задача 1 из 20261115-sadensmol-adoption.md)

## Overview

Решения, которые агент принял без пользователя, и вопросы, которые он задал, попадают в
журнал. Пользователь разбирает их потом (`open` → `mark`), а не следит за сессией.
Пишут две стороны: Claude-сессия через CLI `agterm-decision` и демон agent-dispatch по полю
`decisions` в отчёте исполнителя. Сам исполнитель вне workspace ничего не пишет.

## Context (сверено с кодом 2026-11-15)

- Схема отчёта: `agent_dispatch/schemas/agent_result.schema.json`, `additionalProperties: false` (стр. 20). Без правки схемы новое поле роняет отчёт в `partial` + `parse_error`.
- Инструкция к блоку отчёта: `agent_dispatch/dispatch/task_package.py:158 result_instructions()`, общая для задач, review, report_only, review_fix, followup_resumed.
- Нормализация полей: `executors/result_parser.py` `_coerce` (образец: `confidence`, стр. 211-219, `meta.coerced`); `validate_agent_result` идёт после `_coerce` (:114-127), ошибка схемы даёт `partial` + `parse_error`. `ExecutionResult` собирается явно по полям (:128-142), у модели (`models.py:233`) поля `decisions` нет: без явного копирования в `meta` поле молча теряется.
- `result_instructions(adapter_kind)` не знает kind задачи; переключатель review уже есть в `render_prompt` (`task_package.py:203/:226`, `review=req.kind == "review"`).
- `redact_result` вызывается только при prod (`dispatcher.py:2161-2163`, предикат `request.prod or decision.meta.perimeter == "prod"`).
- Слияния, которые теряют meta: раунд правки `dispatch/dispatcher.py:1298` (`meta.update(fixed.meta)`), дозапрос отчёта `dispatch/report_recovery.py:74-78` (`merge_report`).
- Redact: `dispatch/redact.py:134 redact_result` чистит только `summary` и `error`.
- Компактный ответ MCP: `mcp/server.py:192 _task_text`, пустые поля не печатаются (стр. 241).
- Имя `decisions` в replay занято (`supervision_metrics`, `load_decisions`), новая секция называется `decision_log`.
- agterm-kit: правило «одна утилита, один каталог» (`AGENTS.md`), образцы `stages/agterm-stage`, `lb-limits/lb-limits`; тесты bats.
- PIPE_BUF на этой машине 512 байт, к обычным файлам он не относится. Запись идёт одним `write()` под `fcntl.flock`.

## Метрика

- Покрытие: доля завершённых задач диспатчера с записью (`decision_log.share`). Это шумовая метрика, без второй её смысл теряется.
- Шум: доля вердиктов `keep` у размеченных записей и доля записей с пустым `why`. Если `keep` близко к 100%, журнал пишет очевидное.
- Польза: доля `change|revert`, это вход retro (задача 3).

## Формат (одна строка JSONL, только дозапись)

Файл `<decision_log_dir>/<repo>/<YYYY-MM-DD>.jsonl`, по умолчанию `decision_log_dir = ~/.local/state/decisions`.

```json
{"id":"<10 hex>","ts":"2026-11-15T14:02:11+03:00","repo":"agent-dispatch","session":"<id>","source":"claude|<executor>","task_id":null,"kind":"decision|question|answer|verdict","text":"...","why":"...","ref":null}
```

- `id` случайный (`secrets.token_hex(5)`). `show` и `mark` принимают префикс и отказывают, если префикс неоднозначен.
- `repo` равен basename каталога над абсолютным git common dir. CLI берёт его через `git rev-parse --path-format=absolute --git-common-dir`, демон из события `git` задачи (`common_dir`, пишется в `submit`, `dispatcher.py:209`), без git-вызова в `_finalize`. Вне git используется basename `cwd`.
- `session`: у Claude это `CLAUDE_CODE_SESSION_ID` (проверено в env 2026-11-15; `child_env` вырезает `CLAUDE_CODE_*`, внутри исполнителя её нет, там пишет демон) или `--session`, у демона корневой `task_id` линии (хелпер `_lineage_root` по `escalated_from`/`followup_of`/`parent_task_id`, по образцу `_lineage_perimeter`).
- `answer` и `verdict` ссылаются на запись через `ref`. У `verdict` поле `text` принимает `keep|change|revert`, а `why` содержит заметку.
- Prod-линия (тот же предикат, что у `redact_result`, `dispatcher.py:2161`): `text` равен `"(prod)"`, `why` равен `null`. Полный текст есть только в `result.meta.decisions` в БД после `redact_result`. У CLI для этого флаг `--prod`.
- Вне prod перед записью в журнал `text`/`why` всегда проходят `redact_text`: журнал это хранилище вне БД, его читает скилл.
- Один источник формата: `agterm-kit/decision/decision_log.example.jsonl`. Тест agent-dispatch читает его по `AGTERM_KIT_DIR` или из соседнего каталога `../agterm-kit`; если файла нет, тест пропускается с явной причиной. Формат меняется сначала в agterm-kit.

## Критерии записи (правило для агента)

Писать: выбор между вариантами, у которых разное поведение; отклонение от плана или
задачи; допущение о требованиях; необратимое действие; отказ от части скоупа.
Не писать: форматирование, имена переменных, очевидные шаги плана.

## Разбивка по репам

- agterm-kit: CLI `agterm-decision`, golden-формат и исходник скилла `decisions` —
  `~/my_git_reps/agterm-kit/docs/plans/20261116-decision-log.md`.
- agent-dispatch: задачи ниже.
- Ручные шаги (песочница запрещает агенту писать в `~/.claude`):
  - правило в `~/.claude/CLAUDE.md` с критериями выше: при заданном `AGENT_DISPATCH_HOP`
    вместо CLI заполнять только поле `decisions` в отчёте, при работе с prod-данными
    вызывать CLI с `--prod`;
  - симлинк `~/.claude/skills/decisions` → `~/my_git_reps/agterm-kit/decision/skill`.

## Implementation Steps

### Task 1: схема и разбор
**Depends on:** none
**Files:**
- Modify: `agent_dispatch/schemas/agent_result.schema.json`
- Modify: `agent_dispatch/executors/result_parser.py`
- Modify: `agent_dispatch/dispatch/task_package.py`
- Modify: `tests/test_result_parser.py`
- Modify: `tests/test_task_package.py`

- [x] `agent_result.schema.json`: необязательное `decisions: array`, элементы `{kind: decision|question, text: string, why?: string|null}`, `required: [kind, text]`
- [x] `result_parser._coerce` приводит или отбрасывает всё, что схема отвергнет: не список, элемент не объект, неизвестный `kind`, пустой или нестроковый `text`, нестроковый `why` (→ `null`), лишние ключи элемента (срезаются); `decisions` входит в `_OPTIONAL_FIELDS` (:233), `null` удаляется; причины в `meta.coerced`
- [x] Сборка результата (:128-142): нормализованный список копируется в `meta.decisions`
- [x] Инструкция поля добавляется в `render_prompt` только при `review=False` (`task_package.py:203/:226`); сигнатура `result_instructions` не меняется
- [x] Тесты `tests/test_result_parser.py`: валидно → `completed` и assert на `meta.decisions`; нет поля; `null`; не список; каждый вид мусора → `completed` + `coerced`; `tests/test_task_package.py`: у review поля нет, у задачи есть
- [x] Прогнать тесты, должны пройти

### Task 2: слияния
**Depends on:** Task 1
**Files:**
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `agent_dispatch/dispatch/report_recovery.py`
- Modify: `tests/test_review_fix_round.py`
- Modify: `tests/test_report_recovery.py`

- [x] Раунд правки (`dispatcher.py:1298`) и `merge_report` конкатенируют `decisions` первой попытки и продолжения, без дублей по `(kind, text)`
- [x] Тесты `tests/test_review_fix_round.py`, `tests/test_report_recovery*.py`: записи обеих попыток сохраняются
- [x] Прогнать тесты, должны пройти

### Task 3: журнал и redact
**Depends on:** Task 1
**Files:**
- Modify: `agent_dispatch/config.py`
- Modify: `agent_dispatch/config_default.yaml`
- Modify: `config.example.yaml`
- Modify: `agent_dispatch/dispatch/redact.py`
- Modify: `agent_dispatch/dispatch/dispatcher.py`
- Modify: `tests/test_dispatcher.py`
- Modify: `tests/test_redact.py`

- [ ] `Settings.decision_log_dir` (по умолчанию `~/.local/state/decisions`), синхронно `config_default.yaml` и `config.example.yaml`
- [ ] `redact_result` дополнительно чистит `decisions[].text/why` в `meta` (для БД при prod); запись в журнал идёт после него
- [ ] `_finalize` пишет журнал только при непустом `decisions` и только для `kind: task`: ревью-дети идут через тот же `_finalize` (`:738`), их записи отсекает фильтр, а не только отсутствие поля в промпте. Пишет сам (`source` = `result.executor`, `task_id`, `session` = `_lineage_root`); при prod-линии пишется `(prod)`, иначе текст проходит `redact_text`; ошибка записи не ломает `_finalize` (как `mcp_responses.jsonl`); `recover_stale` журнал не пишет, задача не завершена исполнителем
- [ ] Запись совместима с CLI `agterm-decision` (agterm-kit `ca99675`): файл открывается `O_RDWR|O_APPEND`, под `flock` проверяется последний байт и при хвосте без LF перед записью добавляется `\n` (иначе запись склеивается с прерванной строкой и теряется); строка пишется одним `write()`; `verdict`/`answer` демон не пишет. CLI маскирует копией `redact_text` и сверяет её тестом паритета, поэтому правки `redact.py` проверяются и `bats tests/decision.bats` в agterm-kit
- [ ] Тесты всегда задают `decision_log_dir` в `tmp_path`, а не только `data_dir` (`test_dispatcher.py:39`): gate и второй демон не должны писать в настоящий журнал
- [ ] Тесты `tests/test_dispatcher*.py`: запись, ревью-ребёнок с `decisions` не пишет, пустой список не пишет, redact вне prod, prod → `(prod)`, ошибка записи, `repo` worktree-задачи равен имени основной репы, `session` эскалированного звена равен корню; golden-формат
- [ ] Прогнать тесты, должны пройти

### Task 4: replay
**Depends on:** Task 3
**Files:**
- Modify: `evals/replay/harness.py`
- Modify: `evals/replay/__main__.py`
- Modify: `tests/test_replay.py`

- [ ] `evals/replay/harness.py`: секция `decision_log` (`share`, `keep_share`, `empty_why_share`, `change_revert_share`; вердикты читаются из файла журнала, если он есть, иначе `unmeasured`)
- [ ] Тесты `tests/test_replay*.py`
- [ ] Прогнать тесты, должны пройти

### Task 5: prod-правило
**Depends on:** Task 1
**Files:**
- Modify: `agent_dispatch/templates/prod_data_rule.md.j2`
- Modify: `tests/test_task_package.py`

- [x] Строка в `templates/prod_data_rule.md.j2`: в `decisions` только выводы, без данных; тест шаблона в `tests/test_task_package.py`
- [x] Прогнать тесты, должны пройти

### Task 6: eval
**Depends on:** Task 3
**Files:**
- Modify: `evals/smoke/harness.py`
- Modify: `tests/test_smoke.py`

- [ ] `evals/smoke`: два сценария на `omp/sol` и одном корпоративном исполнителе, N=10 прогонов каждый. Позитивный: задача с заложенной развилкой (два равноценных имени API), запись считается, если `text` упоминает развилку. Негативный: задача без развилки, запись считается шумом
- [ ] Порог: позитив не меньше 8/10, шум не больше 2/10 на каждом исполнителе
- [ ] Тест харнесса сценариев без сети (подсчёт позитива и шума по готовым результатам)

### Task 7: документация
**Depends on:** Task 2, Task 3, Task 4, Task 5, Task 8
**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/architecture.md`

- [ ] CLAUDE.md проекта и `docs/architecture.md`: поле `decisions`, кто пишет журнал, prod-правило
- [ ] Полный прогон `uv run pytest tests -q`, `ruff check`, `ruff format --check`

### Task 8: компактная строка MCP
**Depends on:** Task 3
**Files:**
- Modify: `agent_dispatch/mcp/server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] `_task_text`: строка `decisions: N` только при N > 0 (правило пустых полей компактного ответа); полный список отдаёт `verbose`
- [ ] Тесты `tests/test_mcp_server.py`: строка при N > 0, нет строки при пустом и отсутствующем `meta.decisions`, `verbose` содержит список
- [ ] Прогнать тесты, должны пройти

## Не делаем

UI, HTML, синхронизацию между машинами, запись от ревьюеров.
