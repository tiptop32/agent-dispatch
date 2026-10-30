# Что взять из LiteLLM для AgentDispatch

Дата среза: 2026-10-30. Исходники LiteLLM зафиксированы на commit [`b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4`](https://github.com/BerriAI/litellm/commit/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4). Репозиторий описывает LiteLLM как self-hosted gateway с единым API, cost tracking, guardrails и load balancing. Это исследование не предлагает устанавливать LiteLLM и не меняет контракт локальных CLI-исполнителей.

## Вывод

LiteLLM полезен как каталог проверенных идей для внутреннего слоя выбора deployment. AgentDispatch уже имеет более подходящую для coding-задач границу: Jev выбирает capability, а код запускает CLI, проверяет diff и выполняет `verify`. Переносить LiteLLM целиком не стоит: повтор API-вызова и повтор coding-задачи имеют разные последствия. CLI может успеть изменить файлы или вызвать внешние инструменты ещё до сбоя; даже пустой `changed_files` не доказывает отсутствие побочных эффектов.

## Что уже есть у нас

- cooldown после quota, capacity, auth и network-сбоев, с группами лимита и восстановлением из SQLite: [`agent_dispatch/executors/health.py`](../../agent_dispatch/executors/health.py);
- fallback и escalation с пропуском недоступных исполнителей: [`agent_dispatch/dispatch/escalation.py`](../../agent_dispatch/dispatch/escalation.py);
- cost и latency в telemetry, queue slots, acceptance, verification и replay: [`agent_dispatch/telemetry/storage.py`](../../agent_dispatch/telemetry/storage.py), [`evals/replay/harness.py`](../../evals/replay/harness.py);
- корпоративный perimeter в коде и отдельная локальная политика: `/Users/tiptop32/.config/agent-dispatch/config.yaml`, корпоративная модель первой в `balanced` и fallback. Снимок применения: `/tmp/agent-dispatch-corporate-first/applied.json`; описание workflow в commit `fd6ce79`. Эти локальные настройки не являются дефолтами пакета.

## Приоритеты

### P1. Разделить retry policy по типу исполнителя

LiteLLM явно настраивает `num_retries` и `request_timeout` в proxy settings, а также context-window fallback в [`proxy_server_config.yaml`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/proxy_server_config.yaml#L174-L183). У нас Jev повторяет transport/timeout/5xx с backoff, а сбои исполнителей отдельно обрабатывают `classify_failure` и `should_escalate`. Для CLI задача с изменёнными файлами не должна автоматически повторяться: это может породить второй набор правок и конфликт.

Что взять: описывать policy явно по фазам `route`, `execute`, `verify`, `report`. У нас уже есть эскалация для failure, failed verification и `partial` без результата, а `report_recovery` повторяет запрос в той же сессии и сохраняет работу с `changed_files`. Новая задача здесь не «добавить retry», а записывать компактные `attempt` и `retry_reason`, чтобы отличать retry transport Jev, эскалацию исполнителя и report recovery. Повторный coding-запуск с изменёнными файлами по-прежнему требует отдельного guard.

LiteLLM также разделяет `RetryPolicy` и `AllowedFailsPolicy`: тип ошибки определяет retry/cooldown, а не один глобальный счётчик. У нас `health.py` уже различает quota, capacity, auth и network. Quota имеет отдельный срок и разбор времени сброса, а capacity/auth/network используют общий `failure_cooldown_seconds`. Полезное небольшое улучшение — конфигурируемые длительности по error kind с сохранением групп лимита и ограничений периметра. Это адаптация идеи политик по типам ошибок, а не перенос конкретных порогов LiteLLM. Метрика: повторные одинаковые ошибки до следующего разрешённого запуска и время до успешного fallback.

### P1. Сначала измерить очередь, затем учитывать занятость эквивалентных исполнителей

В [`least_busy.py`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/litellm/router_strategy/least_busy.py) счётчик активных запросов увеличивается перед вызовом и уменьшается при успехе или ошибке. В [`lowest_cost.py`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/litellm/router_strategy/lowest_cost.py#L204-L330) LiteLLM сначала исключает deployment, превысившие TPM/RPM, и только потом сортирует оставшиеся по стоимости. В [`lowest_latency.py`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/litellm/router_strategy/lowest_latency.py#L390-L490) хранятся rolling samples, TTL, latency normalization и TTFT для streaming.

У нас есть cooldown и общий лимит группы, но выбор первого кандидата не учитывает, что корпоративная модель может быть занята, пока Codex того же capability свободен. Первый практический перенос: least-busy или queue-aware tie-break только внутри эквивалентного capability, с конфигурируемым максимальным ожиданием. Корпоративный исполнитель остаётся preferred до этого предела, Codex — backup; guards, source-agent exclusions и perimeter не меняются. Если guard оставил только корпоративного кандидата, переключения не происходит. LiteLLM-документация отдельно описывает least-busy, retry policy, allowed-fails policy и `cooldown_time` на странице [Router - Load Balancing](https://docs.litellm.ai/docs/routing), страница документации меняется независимо от pinned commit; её точная локальная копия и SHA-256 указаны в `/tmp/agent-dispatch-litellm/docs-snapshot.json`. Метрика: p95 ожидания слота, verified share и доля переключений на backup. Исторических данных для обещания экономии пока нет.

### P1. Развести routing quality и transport health

LiteLLM ведёт success/failure callbacks и counters рядом с router usage; его latency handler записывает последние samples и TTFT в cache. У AgentDispatch уже есть отдельные события `spawn`, `exit`, `verify`, `escalate`, `report_recovery` и `feedback`, поэтому новые одноимённые события не нужны. Полезный компактный пробел — snapshot конфигурации выбора и причина попытки в существующих событиях.

У нас уже есть verification и replay, но срез `/tmp/agent-dispatch-evaluation-full.json` показал `feedback.eligible: 740` и `feedback.labeled: 0` среди 786 задач. Eligible здесь означает задачи с результатом после исключения guard-отказов, а не только статус `completed`. Независимый пересчёт по read-only SQLite сохранён в `/tmp/agent-dispatch-litellm/feedback-evidence.json`. Поэтому первый практический перенос это не новый callback backend, а обязательная запись `user_accepted`/`user_reworked` после приёмки. Метрика: покрытие явными оценками завершённых задач; целевое значение ещё не согласовано. Запись feedback уже описана в нашем workflow, автоматическая обратная связь из реального процесса приёмки остаётся отдельной задачей. Эти данные не следует выдавать за измерение качества от LiteLLM.

### P2. Дебитировать бюджет до запуска

В sample config LiteLLM есть закомментированные `max_budget` и `budget_duration`, но этого файла недостаточно, чтобы утверждать наличие reservation. В исходниках LiteLLM есть отдельные budget reservation/release компоненты: [`budget_reservation.py`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/litellm/proxy/spend_tracking/budget_reservation.py) и [`budget_reservation_release_middleware.py`](https://github.com/BerriAI/litellm/blob/b8283d8b61f9d15bdc734d58a7f8a0a38cff5bd4/litellm/proxy/middleware/budget_reservation_release_middleware.py). У нас есть `daily_cost_limit_usd` и `max_chain_cost_usd`, но цена часто появляется только после результата CLI, а Codex не всегда сообщает cost.

Что взять: при submit резервировать известный `estimated_cost` и хранить `budget_reservation`/`budget_release` события; неизвестную цену считать `unpriced`, а не нулём. Для coding CLI безопаснее лимитировать по chain count и wall-clock до появления надёжной цены. Метрика: overshoot известного бюджета и доля задач без цены. Резерв по оценке ограничивает конкурирующие запуски, но не гарантирует точный денежный потолок при неизвестной стоимости CLI.

### P2. Конфигурационные fallback-группы как данные

LiteLLM описывает deployments и fallback mapping в YAML, включая model group и `context_window_fallbacks`. У AgentDispatch аналогичная информация уже в `executors`, `tier`, `limit_group` и `escalation`. Поэтому новая DSL не нужна. Полезно добавить валидируемый `failure_policy` к существующему executor config, а не вводить второй registry.

## Что не брать

- глобальный `num_retries` для coding CLI: side effects делают повтор небезопасным;
- выбор только по lowest latency/cost: эти сигналы не измеряют correctness и acceptance;
- большой gateway/proxy слой ради унификации: текущая архитектура намеренно не отправляет coding task через hosted LLM API;
- fallback при любой ошибке report: если `changed_files` непустой, следующий исполнитель может работать поверх незавершённой правки.

## Рекомендуемый первый шаг

Добавить queue-aware least-busy выбор среди исполнителей одного capability: корпоративная модель preferred до заданного `max_wait_seconds`, Codex backup только если он разрешён текущими guards. Сначала измерить исходные p95 ожидания и verified share по `slot` и результатам. Эти записи показывают прошлое ожидание, но не доказывают эффект иной маршрутизации: для точного моделирования нужны моменты поступления в очередь и занятость альтернатив. Эффект нового выбора затем оценивать отдельно, не смешивая с историческими experiment arms. Отдельным небольшим изменением добавить `attempt`/`retry_reason` в существующие события.

## Verification log

- Прочитан GitHub API metadata и pinned commit; исходные файлы сохранены в `/tmp/agent-dispatch-litellm/`. Метаданные: `repo.json` и `commit.json`; они не являются оценкой качества проекта.
- Прочитаны README, proxy sample, `lowest_cost.py`, `lowest_latency.py` и owning budget reservation sources на том же SHA.
- Дополнительный неприкреплённый к commit источник: официальная routing page, `/tmp/agent-dispatch-litellm/routing.html`, title `Router - Load Balancing | liteLLM`. В `docs-snapshot.json` сохранены URL, время файла и SHA-256. Код least-busy и retry policy отдельно проверен по pinned исходникам.
- Дополнительно прочитаны `least_busy.py` (счётчик активных запросов), `get_retry_from_policy.py` (политика по классу исключения), `budget_reservation.py` (reserve/reconcile/release). Файл `lowest_requests.py` оказался ответом `404` и в вывод не включён.
- Три прохода: pinned исходники LiteLLM; сверка с production-кодом AgentDispatch и текущей телеметрией; проверка ссылок/заголовков и `git diff --check`. Результаты URL-проверки: `/tmp/agent-dispatch-litellm/link-check.json`.
- Live-запросы к LiteLLM и установка зависимости не выполнялись.
