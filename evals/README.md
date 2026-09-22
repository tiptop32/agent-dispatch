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
| 2026-09-22 | 100% (20/20) | 0.0008 | 422 | нет |

Прогон 2026-09-22 через Jev напрямую (`--router jev`, `typesafe/jev-1.13` на OpenRouter) на дефолтном реестре: 6 claude, 7 codex, 7 kimi. Пять решений с confidence < 0.9 (r12 0.80, r13 0.75, r15 0.84, r16 0.73, r20 0.86): все на границе codex/kimi, где descriptions executors близки. Предыдущий прогон того же дня на 20 кейсах, из которых три были заточены под корпоративный executor, дал 95%: один такой кейс ушёл в codex с confidence 0.35, что ниже `min_confidence`, и в реальном `dispatch` его получил бы `fallback_executor`. Эти три кейса заменены на r18-r20 для `opencode/kimi`.

## Smoke

| Дата | Исполнитель | fix | docstring | rename |
|---|---|---|---|---|
| 2026-09-22 | codex | ok (~7 мин) | ok | ok |
| 2026-09-22 | claude | ok (~40 с) | ok | ok |
| 2026-09-22 | opencode (корпоративная модель) | ok (~20 с) | ok | ok |

Первый прогон smoke показал дефект харнесса: шаблон репы содержал баг в `add()` для всех задач, поэтому `docstring` и `rename` проваливались по `pytest` у всех исполнителей одинаково. Теперь баг вносится только для задачи `fix` (`prepare_repo(..., inject_bug=True)`).

