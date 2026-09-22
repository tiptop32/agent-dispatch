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

| Дата | Accuracy | Стоимость, USD | P50, мс |
|---|---:|---:|---:|
| заполняется после живого прогона | — | — | — |

Результаты заполняются после живого прогона пользователем после merge.
