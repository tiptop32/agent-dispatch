# Evals AgentDispatch

Эти два прогона измеряют разные свойства маршрутизации. `routing` проверяет, насколько живой роутер выбирает ожидаемый executor на размеченных coding-задачах. `smoke` отправляет реальные задачи в живой daemon и проверяет результат в изолированной временной Git-репозитории.

Запуск:

```bash
uv run python -m evals.routing
uv run python -m evals.routing --router claude_local
uv run python -m evals.smoke --executors codex
```

По умолчанию `routing` использует живой daemon. Режимы `--router jev` и
`--router claude_local` работают in-process, обходят daemon и предназначены для
сравнения бэкендов маршрутизации.

JSON-отчёты сохраняются в `evals/reports/` (этот каталог игнорируется Git).

## Результаты

| Дата | Accuracy | Стоимость, USD | P50, мс | Промахи |
|---|---:|---:|---:|---|
| 2026-09-22 | 95% (19/20) | 0.0008 | 456 | r19 (x5-code → codex, confidence 0.35: ниже порога, в dispatch сработал бы fallback) |

Прогон 2026-09-22 через живой демон и Jev (`typesafe/jev-1.13` на OpenRouter). Все 6 claude-задач, 7 codex и 4 kimi распознаны; из трёх x5-code задач одна ушла в codex с confidence 0.35, что ниже `min_confidence`, значит в реальном `dispatch` её получил бы `fallback_executor`. Шесть решений с confidence < 0.9 (r12, r13, r15, r16, r19, r20): все на границе kimi/x5-code и codex/kimi, где descriptions executors близки.

## Smoke

| Дата | Исполнитель | fix | docstring | rename |
|---|---|---|---|---|
| 2026-09-22 | codex | ok (~7 мин) | ok | ok |
| 2026-09-22 | claude | ok (~40 с) | ok | ok |
| 2026-09-22 | opencode/x5-code | ok (~20 с) | ok | ok |

Первый прогон smoke показал дефект харнесса: шаблон репы содержал баг в `add()` для всех задач, поэтому `docstring` и `rename` проваливались по `pytest` у всех исполнителей одинаково. Теперь баг вносится только для задачи `fix` (`prepare_repo(..., inject_bug=True)`).

