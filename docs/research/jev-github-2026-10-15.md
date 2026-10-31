# TypeSafe Jev на GitHub: интеграции, которые подтверждает код

## Вывод

Публичная экосистема Jev уже не мала по заданному порогу. Разобраны восемь крупных проектов и пять инструментов для ежедневной работы с агентами. У всех есть проверенное доказательство интеграции TypeSafe Jev; отдельные группы ниже показывают, где это runtime-функция, eval arm или библиотечная поддержка. Ещё две популярные библиотеки реализуют транспорт OpenRouter `POST /api/alpha/decisions`, но не закрепляют TypeSafe-модель в конечном приложении. Они вынесены в отдельную группу и не выданы за подтверждённых потребителей Jev.

Для ежедневной работы первым стоит испытать `jevgrep` на незнакомых репозиториях. Для AgentDispatch приоритетны локальная проверка приватности до внешнего запроса, строгая проверка wire-ответа, калибровка порогов, ограниченные повторы и контроль размера запроса. AgentDispatch уже разделяет probabilistic judgment и детерминированную политику: Jev оценивает задачу, а `capability.py`, guards и corporate perimeter решают, что разрешено выполнить.

Этот срез ничего не говорит о росте популярности. Stars и forks измерены один раз. По ним нельзя вычислить скорость роста, число пользователей или объём реального трафика.

## Время среза и правила отбора

- Время проверки: **2026-10-15 08:01:53 UTC**, по заголовку `Date` ответа GitHub API. Это **2026-10-15 13:01:53 Asia/Yekaterinburg**. Исторический срез; повторное ревью документа выполнено 2026-10-31, без обновления исторических stars/forks.
- Граница активности в 90 дней: **2026-07-17 08:01:53 UTC**. Для каждого включённого репозитория проверен `committedDate` HEAD default branch, а не только `pushed_at`.
- Популярность: не менее 100 stars на момент среза.
- Связь с Jev считалась подтверждённой, если файл на закреплённом commit SHA содержит модель TypeSafe Jev, Jev-specific provider либо одновременно Jev semantics и Decisions endpoint.
- Только наличие `/api/alpha/decisions` без TypeSafe/Jev в коде считается поддержкой протокола, а не доказательством использования модели Jev.
- JAX, Jest, JVM, обычные autoregressive LLM judges и совпадения по имени `Jev` без typed decisions исключены.

Первичные источники методики: [GitHub code search по `typesafe/jev-1.13`](https://github.com/search?q=%22typesafe%2Fjev-1.13%22&type=code), [по `typesafe/jev`](https://github.com/search?q=%22typesafe%2Fjev%22&type=code) и [по `api/alpha/decisions`](https://github.com/search?q=%22api%2Falpha%2Fdecisions%22&type=code). Результаты поиска затем проверены по файлам на commit SHA. Первоначальные доказательства были сохранены в `/tmp/jev-research/`; при ревью 2026-10-31 этот каталог уже отсутствовал. Исторические metadata ниже повторно не подтверждены. Результаты новой проверки ссылок и копии pinned исходников сохранены в `/tmp/agent-dispatch-review/`.

## Что именно отличает Jev

Официальный контракт TypeSafe принимает общий `state` и карту typed `questions`, а возвращает `answers` с теми же идентификаторами. Поддерживаются три примитива: `noul`, `choice`, `score`. Это не chat completion и не текстовый LLM judge. Источник: [TypeSafe API reference](https://docs.typesafe.ai/api), включая endpoint `POST https://api.typesafe.ai/v1/systemone`, описание [Noul](https://docs.typesafe.ai/api#noul), [Choice](https://docs.typesafe.ai/api#choice), [Score](https://docs.typesafe.ai/api#score) и [ответов](https://docs.typesafe.ai/api#response).

OpenRouter обслуживает ту же форму запроса через отдельный alpha Decisions endpoint. Официальный пример использует Jev для typed moderation, а не для генерации текста: [How to Use Jev: Moderation with the Jev API in TypeScript](https://openrouter.ai/blog/tutorials/how-to-use-jev/).

Из этого следует граница исследования:

- **подтверждённый Jev**: код называет TypeSafe/Jev и отправляет `state + questions`;
- **подтверждённый Decisions transport**: код реализует `/alpha/decisions`, но сам не доказывает выбор TypeSafe-модели;
- **аналог**: локальная или другая модель повторяет Jev-style контракт, но TypeSafe Jev не вызывает.

## Что уже есть в AgentDispatch

Текущий AgentDispatch использует Jev как узкий слой классификации, а не как исполнителя:

- [`agent_dispatch/routing/jev.py`](../../agent_dispatch/routing/jev.py) отправляет один запрос, сохраняет `jev_id`, фактическую модель, latency и `usage.cost`, повторяет transport/timeout/5xx и передаёт решение в обычный код.
- [`agent_dispatch/routing/questions.py`](../../agent_dispatch/routing/questions.py) задаёт `capability` как `choice`, а `judgment`, `corporate_data` и `decomposable` как `noul`; `difficulty`, `risk`, `ambiguity` идут как `score`.
- [`agent_dispatch/routing/capability.py`](../../agent_dispatch/routing/capability.py) детерминированно применяет corporate perimeter, judgment adapter и ближайший доступный tier. Capability ceiling применяется в `routing/decision.py`. Jev не получает имена исполнителей.
- [`agent_dispatch/routing/exploration.py`](../../agent_dispatch/routing/exploration.py) детерминированно распределяет задачи между Jev и статическим balanced arm, сохраняя оба выбора.
- [`evals/routing/harness.py`](../../evals/routing/harness.py) считает accuracy, confusion matrix, cost и p50 latency на 20 размеченных случаях. [`evals/replay/harness.py`](../../evals/replay/harness.py) сравнивает Jev со статикой и результаты experiment arms.
- На ревью 2026-10-31 в исходниках есть 24 test-функции Jev router, 21 capability и 9 exploration. Это подсчёт функций, а не число параметризованных случаев и не результат платного eval.

Поэтому не нужны ещё один model router, ещё одна таблица cost или новый A/B-механизм с нуля. Нужны точечные усиления существующих границ.

## Срез популярных и активных репозиториев

`pushed_at` и HEAD commit различаются по смыслу: первое поле задаёт GitHub, второе доказывает коммит в default branch. У всех строк HEAD новее границы 90 дней.

| Репозиторий | Класс доказательства | Stars | Forks | Created UTC | Pushed UTC | HEAD default branch UTC |
|---|---|---:|---:|---|---|---|
| [NousResearch/hermes-agent](https://api.github.com/repos/NousResearch/hermes-agent) | прямой Jev, eval arm | 253 889 | 55 107 | 2025-07-22 22:22:28 | 2026-10-15 07:57:57 | [2026-10-15 07:57:53](https://github.com/NousResearch/hermes-agent/commit/db44c46208f1b421b4ecb7e56bf9d68b0423906f) |
| [BerriAI/litellm](https://api.github.com/repos/BerriAI/litellm) | Decisions transport, без закреплённой Jev-модели | 61 284 | 12 360 | 2023-07-27 00:09:52 | 2026-10-15 07:58:53 | [2026-10-15 07:47:59](https://github.com/BerriAI/litellm/commit/bf56719f09338aaa74a92e698cc6c62d462e5dde) |
| [ToolJet/ToolJet](https://api.github.com/repos/ToolJet/ToolJet) | прямой Jev, marketplace connector | 41 093 | 5 498 | 2021-03-30 08:51:34 | 2026-10-15 07:42:00 | [2026-10-14 11:06:26](https://github.com/ToolJet/ToolJet/commit/6b2e10d457e49c6130e96299f159c69b8eace40a) |
| [activepieces/activepieces](https://api.github.com/repos/activepieces/activepieces) | прямой Jev, runtime router | 25 058 | 4 339 | 2022-12-03 02:52:46 | 2026-10-15 07:50:27 | [2026-10-15 07:50:22](https://github.com/activepieces/activepieces/commit/aac8e039dda655aff08901a9fb1c9931bc9076c3) |
| [1jehuang/jcode](https://api.github.com/repos/1jehuang/jcode) | прямой Jev, runtime decisions | 20 824 | 2 418 | 2026-01-05 09:43:36 | 2026-10-15 03:33:03 | [2026-10-15 03:33:02](https://github.com/1jehuang/jcode/commit/ac2a0b09848a9ecc09942b8faf196b3af69a65bb) |
| [Effect-TS/effect](https://api.github.com/repos/Effect-TS/effect) | Decisions transport, без закреплённой Jev-модели | 17 291 | 862 | 2019-11-13 12:49:21 | 2026-10-15 07:58:55 | [2026-10-15 07:58:07](https://github.com/Effect-TS/effect/commit/973d599a3e8611626764a0e3e8f79edbcf30dc7c) |
| [567-labs/instructor](https://api.github.com/repos/567-labs/instructor) | прямой Jev, typed client | 14 014 | 1 310 | 2023-06-14 10:42:23 | 2026-10-14 22:21:57 | [2026-10-10 21:48:08](https://github.com/567-labs/instructor/commit/035c77b7b04dbbf70ae4457eb102038907b5a2a4) |
| [BasedHardware/omi](https://api.github.com/repos/BasedHardware/omi) | прямой Jev, runtime + shadow | 13 739 | 2 609 | 2024-03-22 18:12:25 | 2026-10-15 07:38:50 | [2026-10-15 07:38:47](https://github.com/BasedHardware/omi/commit/7d47c6cac9e1f5d84277f6638de5bd0dbae8ff1f) |
| [archestra-ai/archestra](https://api.github.com/repos/archestra-ai/archestra) | прямой Jev, observable proxy | 4 371 | 1 211 | 2025-07-15 12:07:55 | 2026-10-15 07:49:47 | [2026-10-15 07:35:55](https://github.com/archestra-ai/archestra/commit/a2c2fff79e06e0e0f3c7331088ae90a0d764a7d8) |
| [TanStack/ai](https://api.github.com/repos/TanStack/ai) | прямой Jev, evaluate adapter | 3 195 | 365 | 2025-10-08 17:52:48 | 2026-10-15 07:35:30 | [2026-10-15 01:31:48](https://github.com/TanStack/ai/commit/5f95dbd08f2780442000ac52074ea747d1b44187) |

## Восемь прямых интеграций Jev

### NousResearch/hermes-agent: Jev как измеряемый eval arm

Код прямо фиксирует OpenRouter endpoint и модель `~typesafe/jev-latest`, формирует по два `noul` на tool call, собирает usage и cost, затем применяет решения в compaction eval: [transport и модель](https://github.com/NousResearch/hermes-agent/blob/db44c46208f1b421b4ecb7e56bf9d68b0423906f/evals/compaction/jev_arm.py#L14-L41), [batch вопросов и учёт usage](https://github.com/NousResearch/hermes-agent/blob/db44c46208f1b421b4ecb7e56bf9d68b0423906f/evals/compaction/jev_arm.py#L424-L483). Файл менялся [8 октября 2026 года](https://github.com/NousResearch/hermes-agent/commit/d76cce4f1161bdac895f7e373ce41cbd04e7c2f2).

Что взять: детерминированный request budget до сетевого вызова и раздельные счётчики requests/input/output/cost. Что уже есть: AgentDispatch уже пишет cost, latency и модель, а также имеет отдельные routing и replay evals. Не надо копировать compactor или его paid arm.

### ToolJet/ToolJet: два провайдера одного wire-контракта

Marketplace connector различает прямой TypeSafe endpoint и OpenRouter, фиксирует `jev-latest` и `typesafe/jev-1.13`, перечисляет `choice`, `score`, `noul` и валидирует вопросы до запроса: [endpoint, модели и объяснение контракта](https://github.com/ToolJet/ToolJet/blob/6b2e10d457e49c6130e96299f159c69b8eace40a/marketplace/plugins/typesafe/lib/index.ts#L13-L24), [preflight validation](https://github.com/ToolJet/ToolJet/blob/6b2e10d457e49c6130e96299f159c69b8eace40a/marketplace/plugins/typesafe/lib/index.ts#L87-L151), [provider-specific model names](https://github.com/ToolJet/ToolJet/blob/6b2e10d457e49c6130e96299f159c69b8eace40a/marketplace/plugins/typesafe/lib/index.ts#L154-L175). Интеграция вошла в release [25 сентября 2026 года](https://github.com/ToolJet/ToolJet/commit/a70c1ca142c6c96c084a72c232e4711d0fdcf13d).

Что взять: локальные ошибки с именем неверного question ID вместо сырого provider 422. Что уже есть: вопросы AgentDispatch строит сам, поэтому пользовательский JSON валидировать не нужно; важнее симметрично усилить проверку ответа.

### activepieces/activepieces: Jev управляет ветвлением workflow

Сервис закрепляет `AI_ROUTER_MODEL_ID = 'typesafe/jev-1.13'`: [model binding](https://github.com/activepieces/activepieces/blob/aac8e039dda655aff08901a9fb1c9931bc9076c3/packages/server/api/src/app/ai/ai-router.service.ts#L47-L91). Worker отправляет `state + questions` в OpenRouter, поддерживает один `choice` или набор `noul`, валидирует ответ через Zod и учитывает provider-reported cost: [вызов и выбор режима](https://github.com/activepieces/activepieces/blob/aac8e039dda655aff08901a9fb1c9931bc9076c3/packages/server/worker/src/lib/execute/jobs/ai/route.ts#L6-L77), [billing и endpoint](https://github.com/activepieces/activepieces/blob/aac8e039dda655aff08901a9fb1c9931bc9076c3/packages/server/worker/src/lib/execute/jobs/ai/route.ts#L81-L125). Feature commit: [23 сентября 2026 года](https://github.com/activepieces/activepieces/commit/5885f009ad3061fb7ecf78597667d794b27be7fc).

Что взять: полную runtime-schema проверку ответа до применения маршрута. Что уже есть: AgentDispatch уже использует стабильные question IDs, умеет `choice` и `noul`, сохраняет provider cost и не смешивает judgment с запуском.

### 1jehuang/jcode: наиболее зрелая транспортная граница

Jcode поддерживает OpenRouter, прямой TypeSafe, AIMLAPI и собственный gateway с разными endpoint/model/key profiles: [profiles](https://github.com/1jehuang/jcode/blob/ac2a0b09848a9ecc09942b8faf196b3af69a65bb/crates/jcode-base/src/jev.rs#L87-L140). Перед отправкой код проверяет лимиты typed questions, после ответа проверяет соответствие вопросов и ответов. Повторы ограничены статусами `429/502/503/504/529`; короткий `Retry-After` соблюдается, длинный квотный ответ быстро возвращается вызывающему: [validation boundary](https://github.com/1jehuang/jcode/blob/ac2a0b09848a9ecc09942b8faf196b3af69a65bb/crates/jcode-base/src/jev.rs#L245-L285), [bounded retries](https://github.com/1jehuang/jcode/blob/ac2a0b09848a9ecc09942b8faf196b3af69a65bb/crates/jcode-base/src/jev.rs#L331-L417). Jev-файл менялся [29 сентября 2026 года](https://github.com/1jehuang/jcode/commit/dbffdea45b0a0c84ba255db82b7c5d59f5c4ea3d).

Что взять: матрицу transient statuses, `Retry-After` и fail-fast для квоты; проверку answer IDs/types/ranges. Что уже есть: AgentDispatch имеет bounded exponential backoff и отдельный fallback на `ClaudeLocalRouter` после отказа Jev, но сейчас повторяет любой 5xx, не повторяет 429 и не учитывает `Retry-After`.

### 567-labs/instructor: typed schema поверх трёх Decisions API

Instructor строит вопросы из Pydantic model, отправляет один и тот же typed contract в TypeSafe и OpenRouter и строго разбирает ответ: [provider endpoints и общий request](https://github.com/567-labs/instructor/blob/035c77b7b04dbbf70ae4457eb102038907b5a2a4/instructor/decisions/client.py#L14-L56), [sync/async clients](https://github.com/567-labs/instructor/blob/035c77b7b04dbbf70ae4457eb102038907b5a2a4/instructor/decisions/client.py#L59-L176). Документация показывает `openrouter/typesafe/jev-1.13`, прямой `typesafe/jev-latest` и предупреждает, что confidence описывает распределение, а threshold надо выбирать на размеченных данных: [Jev providers](https://github.com/567-labs/instructor/blob/035c77b7b04dbbf70ae4457eb102038907b5a2a4/docs/concepts/decisions.md#L91-L144), [probabilities and scores](https://github.com/567-labs/instructor/blob/035c77b7b04dbbf70ae4457eb102038907b5a2a4/docs/concepts/decisions.md#L209-L224). Код менялся [8 октября 2026 года](https://github.com/567-labs/instructor/commit/131832a7ea671fe6f1715cb45c7087dde39fbe4b).

Что взять: calibration metrics на размеченных routing cases. Что уже есть: AgentDispatch уже хранит полные probabilities и имеет 20 размеченных cases, поэтому новый SDK ради этого не нужен.

### BasedHardware/omi: rollout с fail-closed deadline

Omi закрепляет `typesafe/jev-1.13`, OpenRouter lane, трёхсекундный timeout и один retry: [Jev configuration](https://github.com/BasedHardware/omi/blob/7d47c6cac9e1f5d84277f6638de5bd0dbae8ff1f/backend/config/jev_decisions.py#L1-L42). Experiment arm выбирается стабильным salted hash по conversation ID. Неверные проценты отключают эксперимент, а shadow capture имеет жёсткий UTC stop и fail-closed parsing: [stable buckets and arms](https://github.com/BasedHardware/omi/blob/7d47c6cac9e1f5d84277f6638de5bd0dbae8ff1f/backend/config/jev_decisions.py#L70-L143), [hard stop](https://github.com/BasedHardware/omi/blob/7d47c6cac9e1f5d84277f6638de5bd0dbae8ff1f/backend/config/jev_decisions.py#L146-L165). Jev rollout менялся [1 октября 2026 года](https://github.com/BasedHardware/omi/commit/f86f72b6f38f9c0fcf75a9fd29d1b07bacfc0e13).

Что взять: обязательную дату окончания для `explore_share` и fail-closed поведение при неверной дате. Что уже есть: AgentDispatch уже имеет стабильный hash split, сохраняет crossover и сравнивает arms в replay; второй shadow framework не нужен.

### archestra-ai/archestra: Jev как отдельный observable provider

Archestra объявляет Jev decisions-only provider, принимает полный TypeSafe или OpenRouter endpoint, запрещает streaming и проводит вызов через общую retry/metrics обвязку: [контракт адаптера](https://github.com/archestra-ai/archestra/blob/a2c2fff79e06e0e0f3c7331088ae90a0d764a7d8/platform/backend/src/routes/proxy/adapters/jev.ts#L1-L13), [provider factory и execute](https://github.com/archestra-ai/archestra/blob/a2c2fff79e06e0e0f3c7331088ae90a0d764a7d8/platform/backend/src/routes/proxy/adapters/jev.ts#L41-L107). Commit прямо отмечает, что тесты использовали fake upstream, а live key не проверялся: [5 октября 2026 года](https://github.com/archestra-ai/archestra/commit/f7e6fac9e3698cb2c56b3dd3495abba6591987b8).

Что взять: явно маркировать Jev transport как decisions-only в telemetry/error taxonomy. Что уже есть: AgentDispatch уже отделяет router от chat executors и использует fake HTTP в тестах. Не надо вводить OpenAI SDK как лишнюю зависимость.

### TanStack/ai: строгий evaluate adapter

OpenRouter adapter называет Jev не-chat моделью, вызывает `/api/alpha/decisions`, проверяет union ответа для `choice/score/noul`, нормализует usage и возвращает provider response ID: [runtime type guard](https://github.com/TanStack/ai/blob/5f95dbd08f2780442000ac52074ea747d1b44187/packages/ai-openrouter/src/adapters/evaluate.ts#L13-L83), [request and response mapping](https://github.com/TanStack/ai/blob/5f95dbd08f2780442000ac52074ea747d1b44187/packages/ai-openrouter/src/adapters/evaluate.ts#L116-L180), [Jev constructor example](https://github.com/TanStack/ai/blob/5f95dbd08f2780442000ac52074ea747d1b44187/packages/ai-openrouter/src/adapters/evaluate.ts#L183-L236). Metadata `id` и `provider` добавлены [25 сентября 2026 года](https://github.com/TanStack/ai/commit/a450d007a039610994342dd9c3387f880a4eb992).

Что взять: полную tagged-union проверку и поле `provider` рядом с уже сохраняемыми `jev_id` и `model`. Что уже есть: request ID, model, usage cost, latency и raw probabilities уже сохраняются.

## Два transport-проекта, которые нельзя считать доказательством TypeSafe use

### BerriAI/litellm

LiteLLM реализует OpenRouter Decisions path и берёт цену из `usage.cost`: [adapter](https://github.com/BerriAI/litellm/blob/bf56719f09338aaa74a92e698cc6c62d462e5dde/litellm/llms/openrouter/decisions/transformation.py#L1-L19). Последняя правка файла касается billing и датирована [9 октября 2026 года](https://github.com/BerriAI/litellm/commit/465f20f2747c2b6f0e1965b7d45b4813d3e1e815). В этом файле нет `typesafe/jev-*`, поэтому это подтверждение поддержки endpoint, но не доказательство, что LiteLLM или его пользователь выбрал Jev.

Для AgentDispatch нового почти нет: `usage.cost` уже сохраняется. LiteLLM как посредник добавит зависимость без нужной функции.

### Effect-TS/effect

Effect создаёт typed `createDecisions`, корректно убирает `/v1` перед добавлением `/alpha/decisions`, поддерживает custom client transform и проверяет ответ schema decoder: [base URL semantics](https://github.com/Effect-TS/effect/blob/973d599a3e8611626764a0e3e8f79edbcf30dc7c/packages/ai/openrouter/src/OpenRouterClient.ts#L120-L149), [Decisions call](https://github.com/Effect-TS/effect/blob/973d599a3e8611626764a0e3e8f79edbcf30dc7c/packages/ai/openrouter/src/OpenRouterClient.ts#L193-L225). Файл менялся [30 сентября 2026 года](https://github.com/Effect-TS/effect/commit/2c552f220e09dd63d4a777db6cd20b9a13e732f4). В доказательном фрагменте нет TypeSafe model slug, поэтому это transport support, не Jev consumer.

AgentDispatch уже принимает полный `base_url`, что проще и не ломает proxy prefix. Полезна только идея typed response decoder.

## Смежные проекты, исключённые из списка интеграций

Эти репозитории популярны и активны, но сами описывают другую модель или Jev-style совместимость. Они не вызывают TypeSafe Jev и не входят в десять строк выше.

| Репозиторий | Stars / forks | Почему это не подтверждённый Jev | Полезный паттерн |
|---|---:|---|---|
| [wfzyx/von](https://api.github.com/repos/wfzyx/von) | 900 / 69 | GitHub description прямо называет проект local drop-in alternative to TypeSafe Jev. | Локальный совместимый backend для будущих contract tests. |
| [featherless-ai/simple-jev](https://api.github.com/repos/featherless-ai/simple-jev) | 604 / 70 | Превращает open model в classifier/Jev endpoint; TypeSafe service не подтверждён. | Проверка portability wire-контракта без привязки к одному model vendor. |
| [Yinsongxu/LLM2Jev](https://api.github.com/repos/Yinsongxu/LLM2Jev) | 441 / 47 | README/description обещает Jev-style structured decisions из локальных LLM. | Тест совместимости choice/score/noul с неполным сторонним backend. |

Их HEAD commits датированы соответственно [2026-10-08](https://github.com/wfzyx/von/commit/9319ced779f8232ab1d8d3e9c69cdc52ddce77ec), [2026-10-07](https://github.com/featherless-ai/simple-jev/commit/31c8d7a55a48e82d21a481799f3044eb45d81c75) и [2026-10-11](https://github.com/Yinsongxu/LLM2Jev/commit/f2bdad648e7a34bfce64401fb0f7c44bc13209ae). Все проходят формальные пороги stars и активности, но не критерий связи с TypeSafe Jev.

## Что полезно в ежедневной работе

Эти пять проектов прямо используют Jev. Здесь полезность для ваших coding-задач важнее размера платформы. Metadata снята отдельно от большой таблицы выше: точное время файлов snapshot и проверка HEAD первоначально сохранялись в `/tmp/jev-research/daily-tools-snapshot.json`, который больше недоступен; исторические значения здесь не подтверждены повторно. Stars остаются одним срезом, а не доказательством надёжности или роста.

| Инструмент | Stars / forks | Created UTC | Pushed UTC | HEAD UTC | Польза и ограничение |
|---|---:|---|---|---|---|
| [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py) | 22,958 / 1,705 | 2026-09-16 21:30:12 | 2026-09-30 01:24:38 | 2026-09-18 16:28:35 | Для браузерных задач: Jev выбирает операцию и индекс наблюдаемого DOM-элемента, код проверяет цель. В AgentDispatch уже есть граница decision/execution; отдельный браузерный executor нужен только при такой задаче. |
| [tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction/blob/e3f262a7f4d42bd8dd32ced30d26176f7cb545b0/src/compact.ts) | 7,632 / 551 | 2026-09-17 05:57:20 | 2026-09-18 04:45:31 | 2026-09-17 22:29:18 | Для длинных сессий Claude Code: сохраняет оставленный текст дословно, удаляет или сокращает пары tool call/result. В компактном Task Package отдача меньше; ошибки и пропуск нужного результата требуют fallback и проверки сохранения ограничений. |
| [dzhng/jevgrep](https://github.com/dzhng/jevgrep/blob/620468877c3a10f97469b1c039e3fe86a4a61ec3/packages/core/src/providers.ts) | 2,685 / 200 | 2026-09-26 04:06:56 | 2026-10-14 20:42:52 | 2026-10-14 20:42:51 | Первый кандидат для пробы: находит файлы и дословные фрагменты перед делегированием. Результат можно передавать через существующие files/context; нужен контроль полноты, приватности и цены поиска. |
| [kerpopule/hermes-jev-skills](https://github.com/kerpopule/hermes-jev-skills/blob/b1ddc41ae962b6f9f1cc46fc5c14c6f70c976006/jevkit/client.py) | 1,119 / 111 | 2026-09-18 20:01:29 | 2026-10-13 10:33:15 | 2026-10-13 10:30:13 | Отбор skills, shadow-политики и минимизация отправляемых данных. Полезны отдельные приёмы; массовая установка всего набора может дублировать ваш routing и менять конфиги агентов. |
| [devagrawal09/jev-review](https://github.com/devagrawal09/jev-review/blob/31f89602797fb7bea007f8a480bf368bf564954e/src/review/judgments.ts) | 686 / 45 | 2026-09-16 21:45:36 | 2026-09-17 00:29:25 | 2026-09-17 00:29:22 | Предварительно отбирает места для ревью по correctness/security/test gap, затем уточняет сигнал. Находка остаётся поводом для проверки; полный ревьюер и тесты всё равно нужны. |

У `jevgrep` опубликован небольшой [эксперимент на десяти SWE-bench задачах](https://github.com/dzhng/jevgrep/blob/620468877c3a10f97469b1c039e3fe86a4a61ec3/README.md#what-we-measured): обе ветки решили 8/10 задач, стоимость основного агента ниже с поиском; стоимость Jev из этой цифры исключена. Это результат авторов на ограниченном наборе, а не обещание экономии у вас. Проба на ваших задачах должна считать весь путь до принятого изменения: поиск, router, исполнитель, проверки и переделки. Для известного символа прямой `rg` остаётся проще.

У `fast-jev-compaction` результат зависит от доступных hook API и сохранения нужных сообщений. [Алгоритм](https://github.com/tamaratran/fast-jev-compaction/blob/e3f262a7f4d42bd8dd32ced30d26176f7cb545b0/src/compact.ts) не пересказывает текст, но неверное удаление всё равно теряет информацию. README и наличие библиотеки не доказывают совместимость с вашей установленной версией Claude Code; установка и live-проба не выполнялись.

У `hermes-jev-skills` полезны [измерение роутера на собственном трафике](https://github.com/kerpopule/hermes-jev-skills/blob/b1ddc41ae962b6f9f1cc46fc5c14c6f70c976006/docs/measuring-a-router.md) и [переход от shadow к live](https://github.com/kerpopule/hermes-jev-skills/blob/b1ddc41ae962b6f9f1cc46fc5c14c6f70c976006/docs/shadow-to-live.md). Его [правила отправки данных](https://github.com/kerpopule/hermes-jev-skills/blob/b1ddc41ae962b6f9f1cc46fc5c14c6f70c976006/README.md#what-leaves-your-machine) дают пример локального отбора до HTTP. Редакция строк снижает объём данных, но не заменяет явную политику запрета внешнего запроса.

## Пять приоритетов для AgentDispatch

### P0. Проверять приватность до запроса Jev

Файлы: `agent_dispatch/routing/questions.py`, `agent_dispatch/routing/decision.py`, `agent_dispatch/routing/jev.py`, `agent_dispatch/config.py`, тесты маршрутизации.

Сейчас `build_state` передаёт task, context, files и constraints во внешний HTTP-запрос до того, как ответ `corporate_data` ограничивает исполнителей. Это уже явно описано в [архитектуре](../architecture.md#контракт-jev). Корпоративный исполнитель не защищает сам запрос к внешнему роутеру. Явная локальная privacy policy для репозитория или запроса должна обходить внешний Jev до сериализации state и выбирать допустимый локальный путь либо отказывать с понятной причиной. `dispatch_to` уже обходит Jev; это существующий ручной путь, не новая функция.

Метрика успеха: для явно private задач ноль внешних HTTP-вызовов и ноль исходного текста в отправляемом state/логах; локальные тесты проверяют перехваченную сеть, а не обещание модели. Ограничение: regex-редакция секретов не распознаёт весь корпоративный контент. Разрешение отправки определяет локальная политика владельца данных, не ответ уже внешней модели. Изменений поведения или privacy-конфига в рамках исследования нет.

### P0. Проверять полный wire contract до выбора executor

Файлы: `agent_dispatch/routing/questions.py`, `agent_dispatch/routing/jev.py`, `tests/test_jev_router.py`.

Добавить проверку точного набора ожидаемых answer IDs, совпадения `type`, допустимых choice labels, score legend и суммы вероятностей для каждого ответа. При ревью 2026-10-31 уже есть проверки конечности и диапазонов `noul`, `capability.confidence` и элементов probabilities: [фикс `996e716`](https://github.com/tiptop32/agent-dispatch/commit/996e7168b9f4a3e9eda1a008b8337f5749c1f1f8) от 2026-10-23. Описанное в первоначальном черновике прохождение NaN и `noul` вне `[0, 1]` больше не воспроизводится. Остаются пропуск неизвестного типа вспомогательного ответа и отсутствие полной проверки набора вопросов; `corporate_data` при заданном вопросе уже проверяется отдельно в `JevRouter`. Уверенность вспомогательных `choice`/`score` и значение `score` ещё не проверяются на конечность. Это анализ границы по коду, не свидетельство production-инцидента.

Метрика успеха: 100 % таблицы malformed-response fixtures дают `RouterError` и ни один не достигает `select`; в telemetry нет решений с отсутствующим ожидаемым judgment. Ограничение: принять документированные различия OpenRouter и прямого TypeSafe, не требовать необязательные provider fields.

### P0. Повторять только transient ошибки и учитывать `Retry-After`

Файлы: `agent_dispatch/routing/jev.py`, `tests/test_jev_router.py`, при добавлении счётчика попыток `agent_dispatch/models.py` или `RouteDecision.meta`.

Взять политику Jcode: retry для network timeout и `429/502/503/504/529`, короткий numeric `Retry-After`, fail-fast для длинной квоты, 4xx schema/auth без retry. Сейчас 429 сразу становится `RouterError`, любой 500–599 повторяется, а `Retry-After` игнорируется.

Метрика успеха: gate tests доказывают точное число попыток и задержек по каждому status; p95 времени до fallback не превышает настроенный router timeout budget. Ограничение: не переключать provider/account после auth или billing error и не логировать ключ.

### P1. Добавить calibration в существующие eval и replay

Файлы: `evals/routing/harness.py`, `evals/replay/harness.py`, `evals/routing/cases.jsonl`, `evals/README.md`.

Считать Brier score и expected calibration error для `capability`, а для `noul` сначала добавить независимую разметку ожидаемого ответа. Одних сохранённых probabilities недостаточно для оценки правильности. Отдельно показывать accuracy, опасные недооценки сложности, fallback rate и accepted completion по confidence bucket. Использовать существующие eval и replay, с отдельной выборкой для настройки порогов и для оценки.

Метрика успеха: отчёт воспроизводимо показывает Brier/ECE и sample count по bucket; threshold меняется только после заранее заданного минимума наблюдений. Ограничение: 20 routing cases мало для узких buckets; не подбирать threshold по тому же набору, на котором его оценивают.

Методический пример, не популярный проект: [themsquared/jev-calibration](https://github.com/themsquared/jev-calibration/blob/311daf22fdae7b88696ed6384aa474af7d17cc3e/README.md) имел 0 stars на срезе. Независимый пересчёт его двух опубликованных JSONL дал по 240 случаев, 217 точных совпадений с меткой и 8 ошибок при `confidence >= 0.9`. В run `jev-latest` среди 134 ответов с `confidence == 1` правильны 133. Сырые данные и пересчёт первоначально сохранялись в `/tmp/jev-research/themsquared__jev-calibration/`; каталог недоступен, поэтому эти числа при повторном ревью не подтверждены и не используются как доказательство. Это одна задача классификации tool calls с авторской разметкой; её точность и пороги нельзя переносить на ваш routing. Полезен способ проверять уверенные ошибки.

### P1. Ограничить wire-размер до HTTP-вызова

Файлы: `agent_dispatch/routing/questions.py`, `agent_dispatch/routing/jev.py`, `agent_dispatch/config.py`, `tests/test_jev_router.py`.

Считать сериализованный размер `state + questions`, иметь явный `max_request_bytes` и возвращать понятный `RouterError` до сети. AgentDispatch всегда задаёт только 7–8 вопросов, поэтому batching вопросов сейчас не нужен. Риск находится в длинных `task/context/constraints`.

Метрика успеха: ноль provider 413/422 из-за размера; telemetry показывает request bytes и долю локальных oversize rejects. Ограничение: не обрезать task или security constraints молча; безопаснее перейти к fallback с причиной.

## Что не стоит делать

- Не заменять текущий hand-written client на LiteLLM, Instructor или Effect только ради endpoint. Текущий transport мал, async и уже тестируется fake HTTP.
- Не переходить с pinned `typesafe/jev-1.13` на `~typesafe/jev-latest` без отдельного зафиксированного eval baseline. Floating alias удобен для demos, но меняет поведение без code diff.
- Не считать `confidence` доказательством эмпирической точности на ваших задачах. Официальный контракт и Instructor описывают его как свойство распределения; thresholds требуют размеченных данных.
- Не добавлять локальный Jev-style model в fallback под видом того же Jev. Это новый provider с отдельной calibration и contract matrix.
- Не делать вывод о росте экосистемы по этому срезу. Для growth нужны минимум два одинаково собранных snapshot в разные даты.

## Проверка и ограничения исследования

Повторное ревью 2026-10-31 разделено на три прохода:

1. Проверка публичных ссылок, HTML titles и доступности pinned исходников. Результаты и копии файлов: `/tmp/agent-dispatch-review/links.json` и `*.source`. Доступность ссылки сама по себе не подтверждает вывод об интеграции.
2. Сверка утверждений об AgentDispatch с текущими `routing/questions.py`, `routing/jev.py`, историей фикса `996e716` и независимым ревью. Исправлены устаревшие утверждения о NaN/`noul`; test-функции пересчитаны через AST, routing cases прочитаны как JSONL. Результат: `/tmp/agent-dispatch-review/local-facts.json`.
3. Штатные проверки staged diff: whitespace, secrets и pre-commit gate. Итог ревью и статусы команд: `/tmp/agent-dispatch-review/report.md`.

Первоначальный черновик ссылался на GitHub code search, GraphQL metadata, расчёт cutoff и чисел calibration в `/tmp/jev-research/`. Этот каталог отсутствует. Исторические stars/forks, timestamps snapshot и числа чужого calibration-эксперимента остаются непроверенными при повторном ревью; для подтверждения нужны первоначальные snapshot либо новый отдельно датированный сбор metadata и повторный расчёт pinned JSONL. Сохранённые исторические значения нельзя выдавать за текущую статистику.

Не проверено и не утверждается:

- что feature включён в production deployment конкретного проекта;
- сколько решений или пользователей прошло через интеграцию;
- качество, latency и цена Jev на текущей модели;
- live compatibility с ключом TypeSafe/OpenRouter.

Платные API и LLM evals не запускались. Доказательство ограничено публичным кодом, историей commits, GitHub metadata и официальной документацией.

Проверки изменения документа: `git diff --check` и `git diff --cached --check`. Новых тестов и eval в diff нет: поведение программы не меняется. Проверки кода запускаются штатным pre-commit при коммите. Эффект предложений на вашей нагрузке остаётся неизмеренным до отдельного эксперимента.
