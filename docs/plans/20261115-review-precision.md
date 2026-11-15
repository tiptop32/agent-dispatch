# Стабильность fail в ревью (задача 2 из 20261115-sadensmol-adoption.md)

## Overview

Каждый fail ревью оплачивается раундом правки или звеном эскалации. Нужно видеть, какие
fail нестабильны (тот же ревьюер после раунда без правок даёт pass) и какой размер правки
следует за fail. Нужно также сохранять состояние, которое видел ревьюер: без него
разметка точности (часть B, `docs/backlog/review-precision-labeling.md`) невозможна.

## Context (сверено с кодом 2026-11-15)

- Срез replay: 78 ревью, 38 fail, раунды правки $5.3, звено эскалации в среднем $0.76.
- В конфиге пользователя сегодня у `omp/sol` убран `review_by` после ручной разметки:
  12 fail x5-large, 3 оправданы, 75% замечаний ложные (`~/.config/agent-dispatch/config.yaml`,
  комментарий у `omp/sol`). Ступени теперь зависят от работы, поэтому разбивка идёт по
  имени ревьюера, а не по номеру ступени (`harness.py:1557`).
- Повторное ревью после раунда пишет строку `review_stages` с `round` = номер раунда
  (`dispatcher.py:1192`), повторяется только проваленная ступень; `fix_rounds[i].stage` есть (`:1257`).
- `fix_rounds[i].verdict` ставится и веткой verify (`:1150`, `"fail"` на `:1203`), и при сбое раунда
  (`:1264`), поэтому как вердикт повторного ревью он не годится.
- Файлов раунда отдельно нет, они сливаются в общий список (`:1252`).
- Worktree: перед ревью делается коммит (`:1353`), `meta.patch` содержит финальный патч уже после правок (`:2401-2414`).

## Метрика

По каждому ревьюеру: `unstable_share` (доля нестабильных fail), гистограмма размера
правки после fail, $ на нестабильные fail. Это подмножество `fix_round.round_cost`, с ним
не складывается. Цифру нельзя называть «$ на ложные fail»: ревьюер на повторе не видит
прошлых замечаний, и ложным мог быть как первый fail, так и второй pass.

## Задачи

### Task 1: дельта раунда
- [ ] `dispatcher.py`: `DiffStatSnapshot(tree.path if tree else req.cwd)` через `asyncio.to_thread` перед `_call_adapter` раунда (`:1234`); `fix_rounds[i].diff_stat` по `fixed.changed_files` после `_recover_report` (`:1246`); `close()` снимка на всех выходах, включая ранние `return` при supervision (`:1235-1239`, `:1247-1251`); `inherited_files` для раунда не нужен; при сбое снимка `fix_rounds[i].diff_stat_unavailable` с причиной (образец `:1042,1049`)
- [ ] Тесты `tests/test_review_fix_round.py`: дельта только раунда; in_place с грязным файлом вызывающего (база включает грязь); сбой снимка; ранний выход supervision закрывает снимок. `tests/test_dispatcher_worktree.py`: worktree
- [ ] Прогнать тесты, должны пройти

### Task 2: сохранённое состояние ревью
- [ ] Патч пишется внутри `_review_work` после коммита (`:1353`) и только когда ревью-потомок действительно запущен. Строки `skipped` без `task_id` (`commit:` на `:1356`, `perimeter: no corporate reviewer` на `:1171-1176`, сбой `submit` на `:1414`) получают `input_patch: null` и `input_skipped: <причина>`
- [ ] Worktree: `git diff <tree.base>..<sha коммита>`; при handoff в диф входит патч предка, это то же, что видел ревьюер
- [ ] In_place: база HEAD (ревьюер видел работу вместе с грязью вызывающего, это и сохраняется). Патч строится во временном индексе, как в `DiffStatSnapshot` (`workspace.py:223-260`), с pathspec по `changed_files` (тот же scope, что у ревьюера, `:1384-1387`), поэтому untracked-файлы задачи входят, чужие грязные файлы и индекс вызывающего не трогаются; сбой даёт `input_skipped: snapshot`
- [ ] Файл `<data_dir>/review_inputs/<review_task_id>.patch`, путь в `review_stages[i].input_patch`; лимит `REVIEW_INPUT_LIMIT_BYTES = 1 MiB`, при превышении файл не пишется, ставится `input_skipped: size`
- [ ] Периметр: используется готовое `perimeter` (`_lineage_perimeter`, `:2045-2082`, уже вычислено на `:1167` в `_verify_and_review`, передаётся в `_review_work` параметром), своя проверка не пишется. Под периметром `input_skipped: perimeter`. Патчи и логи таких задач и так лежат на диске (`patches/`), пропуск нужен ради части B (отправка судье)
- [ ] Хранение: автоматического удаления нет, `worktrees --clean` каталог не трогает (это датасет разметки). `doctor` выдаёт WARN, если `review_inputs/` вместе с `patches/` больше 500 MB
- [ ] Тесты: worktree, in_place с untracked-файлом, skipped-ступень без `task_id`, периметр, лимит, сбой снимка, WARN `doctor`
- [ ] Прогнать тесты, должны пройти

### Task 3: секция replay `review_stability`
- [ ] Единица подсчёта: каждая строка `review_stages` с `verdict: fail` (и первичная, и повторная). Её исход определяет раунд `fix_rounds[j]`, где `trigger == review_failed`, `stage == by` и `round == row.round + 1` (у первичной строки `round: 0`, пишется на `:1191`; поля `round`/`stage`/`trigger` появились одним коммитом 2243e93, строк без `round` нет; фикстуры тестов строятся с `round: 0`). Так каждый fail-вердикт считается один раз: `still_fail` это исход одной единицы, а сама повторная fail-строка становится следующей единицей
- [ ] Исходы проверяются по порядку, первый подошедший выигрывает:
  1. `no_round`: раунда нет (бюджет, нет сессии, `_budget_stop`, бюджет потрачен на verify, выход supervision до записи раунда `:1237,1248`; причина не хранится, корзина одна)
  2. `round_failed`: `fix_rounds[j].status` есть и не `completed` (`:1264`)
  3. `verify_failed_after`: повторной строки `review_stages[round == j, by == stage]` нет, и дальше идёт раунд с `trigger == verification_failed` или финальный `verification.result == failed`
  4. `rereview_unknown`: повторная строка `unknown`/`skipped` или повторной строки нет без признаков п. 3
  5. `still_fail`: повторная строка `fail`
  6. повторная строка `pass`: `unmeasured`, если нет `fix_rounds[j].diff_stat`; иначе `unstable` (0 файлов), `small_fix` (`added + removed ≤ SMALL_FIX_LINES = 5`, комментарий «почему 5»), `fixed`
- [ ] `unstable_share = unstable / (unstable + small_fix + fixed)`: знаменатель это измеренные pass после правки
- [ ] Разбивка по имени ревьюера; стоимость `unstable` как подмножество `fix_round.round_cost`, не складывается
- [ ] `recommendations`: WARN при `unstable_share` > 0.2 и знаменателе не меньше 10; рядом комментарий с обоснованием, как у `REVIEW_SKIPPED_SHARE` (`harness.py:1688`)
- [ ] Тесты `tests/test_replay.py`: каждый исход; порядок исходов; `still_fail` при исчерпанном бюджете считается двумя единицами (`still_fail`, затем `no_round`), а не дважды одной; старая строка без `diff_stat` с исходом `still_fail` не попадает в `unmeasured`; multi-stage; порог WARN
- [ ] Прогнать тесты, должны пройти

### Task 4: документация
- [ ] CLAUDE.md проекта и `docs/architecture.md` (`:49,51,179`): дельта раунда, `review_inputs`, то, что по метрике решает человек
- [ ] Полный прогон `uv run pytest tests -q`, `ruff check`, `ruff format --check`

## Не делаем

Платную разметку (backlog), автоматическое отключение ступени, смену ревьюера по метрике.
