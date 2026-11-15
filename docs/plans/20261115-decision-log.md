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

## Задачи

### Task 1: agterm-kit, CLI `decision/agterm-decision`
- [ ] Python stdlib: `add --kind --text --why [--ref]`, `list [--repo] [--since 7d] [--kind]`, `show <id-prefix>`, `mark <id-prefix> keep|change|revert [--note]`, `answer <id-prefix> --text`, `open`
- [ ] `open`: записи `decision` без `verdict` и `question` без `answer`, свежие сверху, за 14 дней по умолчанию
- [ ] Запись одним `write()` под `fcntl.flock`; битая строка при чтении пропускается с предупреждением в stderr
- [ ] `install.sh`, README; `decision/agterm-decision` добавляется в существующую `ast.parse`-проверку `make lint`
- [ ] `tests/decision.bats` (изолированный HOME, ASCII-имена тестов): формат против golden, append-only, фильтры, префикс и неоднозначность, open/answer/mark, битая строка, два параллельных `add`, два одинаковых `add` в одну секунду дают разные id, `--prod`, `CLAUDE_CODE_SESSION_ID` задан → `session` заполнен
- [ ] Прогнать тесты agterm-kit, должны пройти

### Task 2: agent-dispatch, схема и разбор
- [ ] `agent_result.schema.json`: необязательное `decisions: array`, элементы `{kind: decision|question, text: string, why?: string|null}`, `required: [kind, text]`
- [ ] `result_parser._coerce` приводит или отбрасывает всё, что схема отвергнет: не список, элемент не объект, неизвестный `kind`, пустой или нестроковый `text`, нестроковый `why` (→ `null`), лишние ключи элемента (срезаются); `decisions` входит в `_OPTIONAL_FIELDS` (:233), `null` удаляется; причины в `meta.coerced`
- [ ] Сборка результата (:128-142): нормализованный список копируется в `meta.decisions`
- [ ] Инструкция поля добавляется в `render_prompt` только при `review=False` (`task_package.py:203/:226`); сигнатура `result_instructions` не меняется
- [ ] Тесты `tests/test_result_parser.py`: валидно → `completed` и assert на `meta.decisions`; нет поля; `null`; не список; каждый вид мусора → `completed` + `coerced`; `tests/test_task_package.py`: у review поля нет, у задачи есть
- [ ] Прогнать тесты, должны пройти

### Task 3: agent-dispatch, слияния
- [ ] Раунд правки (`dispatcher.py:1298`) и `merge_report` конкатенируют `decisions` первой попытки и продолжения, без дублей по `(kind, text)`
- [ ] Тесты `tests/test_review_fix_round.py`, `tests/test_report_recovery*.py`: записи обеих попыток сохраняются
- [ ] Прогнать тесты, должны пройти

### Task 4: agent-dispatch, журнал и redact
- [ ] `Settings.decision_log_dir` (по умолчанию `~/.local/state/decisions`), синхронно `config_default.yaml` и `config.example.yaml`
- [ ] `redact_result` дополнительно чистит `decisions[].text/why` в `meta` (для БД при prod); запись в журнал идёт после него
- [ ] `_finalize` пишет журнал только при непустом `decisions` и только для `kind: task`: ревью-дети идут через тот же `_finalize` (`:738`), их записи отсекает фильтр, а не только отсутствие поля в промпте. Пишет сам (`source` = `result.executor`, `task_id`, `session` = `_lineage_root`); при prod-линии пишется `(prod)`, иначе текст проходит `redact_text`; ошибка записи не ломает `_finalize` (как `mcp_responses.jsonl`); `recover_stale` журнал не пишет, задача не завершена исполнителем
- [ ] `_task_text`: строка `decisions: N` только при N > 0
- [ ] Тесты всегда задают `decision_log_dir` в `tmp_path`, а не только `data_dir` (`test_dispatcher.py:39`): gate и второй демон не должны писать в настоящий журнал
- [ ] Тесты `tests/test_dispatcher*.py`: запись, ревью-ребёнок с `decisions` не пишет, пустой список не пишет, redact вне prod, prod → `(prod)`, ошибка записи, `repo` worktree-задачи равен имени основной репы, `session` эскалированного звена равен корню; `tests/test_mcp*.py`: компактная строка; golden-формат
- [ ] Прогнать тесты, должны пройти

### Task 5: agent-dispatch, replay
- [ ] `evals/replay/harness.py`: секция `decision_log` (`share`, `keep_share`, `empty_why_share`, `change_revert_share`; вердикты читаются из файла журнала, если он есть, иначе `unmeasured`)
- [ ] Тесты `tests/test_replay*.py`
- [ ] Прогнать тесты, должны пройти

### Task 6: Claude, правило и скилл
- [ ] `~/.claude/CLAUDE.md`: правило с критериями выше; при заданном `AGENT_DISPATCH_HOP` вместо CLI заполнять только поле `decisions` в отчёте; при работе с prod-данными вызывать CLI с `--prod`
- [ ] `~/.claude/skills/decisions/SKILL.md`: разбор через `open` → `mark`/`answer`; для change/revert агент предлагает правку или follow-up
- [ ] Строка в `templates/prod_data_rule.md.j2`: в `decisions` только выводы, без данных; тест шаблона в `tests/test_task_package.py`
- [ ] Прогнать тесты, должны пройти

### Task 7: eval
- [ ] `evals/smoke`: два сценария на `omp/sol` и одном корпоративном исполнителе, N=10 прогонов каждый. Позитивный: задача с заложенной развилкой (два равноценных имени API), запись считается, если `text` упоминает развилку. Негативный: задача без развилки, запись считается шумом
- [ ] Порог: позитив не меньше 8/10, шум не больше 2/10 на каждом исполнителе
- [ ] Тест харнесса сценариев без сети (подсчёт позитива и шума по готовым результатам)

### Task 8: документация
- [ ] CLAUDE.md проекта и `docs/architecture.md`: поле `decisions`, кто пишет журнал, prod-правило
- [ ] Полный прогон `uv run pytest tests -q`, `ruff check`, `ruff format --check`

## Не делаем

UI, HTML, синхронизацию между машинами, запись от ревьюеров.
