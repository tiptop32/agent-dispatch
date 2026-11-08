# План улучшения AgentDispatch: static routing и outcome telemetry

## Аудит 2026-11-08

Read-only replay текущей `dispatch.db` содержит 929 задач и 784 завершённые цепочки. 105 цепочек (13.4%) не дали полезного результата и потратили 376.6 минуты. Причины: guard 36, task failure 21, cancelled 21, quota 19, timeout 3, needs_context 2, auth 2, no_report 1. Из 44 таймаутов 36 не имели достаточной диагностики `last_output_age_s`. Jev совпал со статическим `balanced` в 26 из 28 сопоставимых исторических решений (92.9%); выборка не доказывает превосходство ни одного варианта. Acceptance: 353/363 проверенных диффов были закоммичены, сохранено 92.7% значимых строк. Контрфактические результаты replay не моделирует.

## Реализация

1. `router.backend: static` по умолчанию; `capability` выбирает `fast`, `balanced` или `strong`, `max_capability` сохраняет семантику потолка.
2. Jev и `claude_local` остаются opt-in. Jev получает task state только после realpath-проверки cwd против `router.external.trusted_roots`; неизвестные проекты и `prod` закрыты.
3. Введена внутренняя taxonomy ошибок и fail-closed retry predicate. `sequence_number regressed` и transport failures получают ограниченный retry только до первого ответа и при неизменённой рабочей копии. Transport failure сам по себе не запускает дорогую escalation.
4. Таймауты записывают cause, type, duration, timeout limit, last output/activity age. MCP errors и guard results содержат `error_code`, `retryable`, `next_action`.
5. Replay собирает outcome-aware поля по policy version: verified success, observed completion, cost, latency, retries, escalations, transport errors и waste. Контрфактические сравнения остаются запрещены.

## Проверка

- `tests/test_static_router.py`: отсутствие сетевого вызова и symlink/trusted-root preflight.
- `tests/test_health.py`, `tests/test_result_parser.py`, `tests/test_process.py`: taxonomy, retry safety и timeout diagnostics.
- `tests/test_replay.py`: observed outcome metrics.
- `uv run pytest tests -q`, `uv run ruff check .`, `uv run ruff format --check .`.
- `uv run python -m evals.replay --json` на read-only `dispatch.db`.

## Ограничения

Исторический replay не может показать, какой результат дала бы другая модель. Для будущего adaptive routing нужны реальные `policy_version`, exact `model_id`, retry/escalation/failure/verification и caller feedback; policy остаётся пассивной до накопления достаточной выборки.
