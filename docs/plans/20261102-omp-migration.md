# Переход с codex CLI и opencode CLI на oh-my-pi

## Overview
- codex CLI и opencode CLI заменяются на oh-my-pi (`omp`, github.com/can1357/oh-my-pi, MIT) везде:
  интерактивный агент пользователя и исполнители AgentDispatch.
- Переход сразу везде (решение brainstorm 2026-11-02): адаптеры `codex` и `opencode` удаляются,
  адаптер `claude` остаётся. Работа идёт в трёх репозиториях и в `~/.claude`, поэтому коммиты делаются
  по одному на репозиторий, а само переключение (Task 12) выполняется одним шагом с записанным откатом.
- GPT идёт через балансер agent-lb (`127.0.0.1:2455`), корпоративные модели через copilot-x5.
  Периметр данных держит только диспетчер: x5-исполнители работают в отдельном профиле omp, где нет
  ни одного внешнего провайдера, поэтому и сабагенты, и prewalk, и advisor остаются внутри периметра.
  Интерактивный omp ограничений по пути не получает.
- Скилы переносятся отдельными копиями под инструменты omp, а дрейф копий проверяет pre-commit в omp-setup.
- `codex-review` становится `gpt-review`: тот же раннер с бюджетом, но вызов через `omp -p`.
- Измеримый итог: через неделю после переключения в `evals.replay` нет новых задач `codex/*` и
  `opencode/*`, доля `completed` у `omp/*` не ниже прежней доли codex и opencode, `perimeter_violations = 0`,
  в логах сессий x5-профиля нет запросов к agent-lb.

## Context (from discovery)
- agent-dispatch: `agent_dispatch/executors/{base,codex,opencode,claude,health,result_parser,registry}.py`,
  `doctor.py`, `config.py` (`adapter: Literal["claude","codex","opencode"]` строка 168,
  `fallback_executor = "codex"` строка 79, `review_only {"codex": ["claude"]}` строка 108, наборы
  источников и адаптеров строки 127-128, группировка лимитов opencode по провайдеру строки 193-206),
  `config_default.yaml` (равен `config.example.yaml`, есть тест), `models.py` (`SourceAgent`),
  `routing/guards.py`, `dispatch/{dispatcher,task_package,report_recovery}.py`, `schemas/__init__.py`,
  `evals/replay/harness.py` (хардкод `{"claude","codex"}` строка 243 и `review_only` строка 311).
- Обёртка `scripts/codex-agent-lb` и её тест удаляются: ключ теперь берёт omp сам (`apiKey: "!команда"`).
- Пользовательский конфиг `~/.config/agent-dispatch/config.yaml`: `codex/sol` (gpt-5.6-sol),
  `codex/terra` (gpt-6.1-terra), `codex/luna` (gpt-5.6-luna), `opencode/x5-code-large` (corporate),
  выключенные `opencode/x5-small-coder`, `opencode/copilot-code-flash`, `opencode/copilot-code-flash-thinking`,
  `codex/gpt-5.5`.
- codex: `~/.codex/config.toml`, провайдер `agent-lb` = `http://127.0.0.1:2455/backend-api/codex`,
  `wire_api = responses`; MCP `mcphub`, `agent-dispatch` (`env_vars` AGENT_DISPATCH_*), `context7`.
- opencode: `~/.config/opencode/opencode.json`, провайдер `copilot` (`https://api-copilot.x5.ru/v1`, модели
  с лимитами), MCP `mcphub`, `agent-dispatch`, `agterm-ask`, `sequential-thinking`; instructions
  `salt-qa-agent-kit/AGENTS.md` и `~/.config/opencode/AGENTS.md`; сервис `~/.config/opencode/service.json`.
- Документация omp: `/tmp/claude/omp-src/repo/docs`. Проверено по ней:
  - `omp -p --mode json` пишет NDJSON (заголовок сессии, события, `message_end` с `usage`); код выхода
    ненулевой при сбое хода и `3` при `--max-cost`; есть `--resume <id>`, `--session-dir`, `--model`,
    `--thinking`, `--max-time`, `--tools`, `--approval-mode`;
  - сабагенты `task`, `prewalk` (цель роль `smol`) и `advisor` выбирают модель через `modelRoles`
    (`task-agent-discovery.md`);
  - bash-инструмент omp вычищает только переменные из `.env` проекта, а окружение запускающего процесса
    доходит до shell агента (`environment-variables.md:467`);
  - `apiKey: "!команда"` в `models.yml` получает ключ из вывода команды (`models.md:292`);
  - профили и `PI_CONFIG_DIR` разделяют конфиги (`environment-variables.md:23,573`);
  - пользовательские скилы Claude и Codex omp сам не подхватывает (`enabledProviders`).
- Скилы к переносу (12): gitlab-mr (`~/x5/git_reps/gitlab-mr`, `glflow`), allure-triage, review-refactor,
  test-audit, kaiten, plan-dispatch, russian-developer-style, writing-clearly, agterm, huddle, agdoc, webrev.
- codex-review: `~/my_git_reps/codex-review` (`bin/codex-review`, бюджет команд и таймаут, salvage через
  `codex exec resume`), симлинк `~/.claude/skills/codex-review`.

## Development Approach
- **testing approach**: Regular (код, затем тесты в той же задаче)
- каждая задача доводится до конца перед следующей, изменения маленькие и сфокусированные
- **CRITICAL: каждая задача содержит новые или обновлённые тесты** на изменённый код:
  успешные сценарии и ошибки
- **CRITICAL: все тесты зелёные перед следующей задачей**
- **CRITICAL: план обновляется, если объём работ меняется по ходу**
- гейт agent-dispatch: `uv run pytest tests -q` и `uv run ruff check . && uv run ruff format --check .`
- гейт omp-setup и gpt-review: `make test`; agterm-kit: `make test lint`
- факты о форматах omp берутся из живых прогонов (Task 1), а не из документации
- коммит на каждый репозиторий; удаление codex и opencode (Task 10) идёт последней из кодовых задач,
  потому что до переключения работа делегируется через этот же демон

## Testing Strategy
- **unit**: адаптер (argv и env), разбор ошибок, конфиг и миграция, doctor, drift, раннер gpt-review;
  всё на фикстурах живого вывода omp из Task 1
- **периметр**: тест, что x5-исполнитель запускается в профиле без внешних провайдеров, и живая проверка,
  что сабагент x5-задачи не обращается к agent-lb
- **секреты**: тест, что в env дочернего процесса нет `AGENT_LB_API_KEY`, и живая проверка `env` из bash
  агента
- **живые проверки** (Task 12): `evals.smoke` на каждом новом исполнителе, `doctor --online`, `omp models`,
  `omp -p` через каждую роль, `tools/list` каждого MCP, по одной задаче на каждый скил
- **eval**: `evals.replay` через неделю после переключения, пороги из Overview

## Progress Tracking
- выполненное отмечать `[x]` сразу
- новые задачи с префиксом ➕, блокеры с ⚠️
- при отклонении от плана обновлять план

## Solution Overview
- **Интерактивный omp.** Конфиг в новом репозитории `~/my_git_reps/omp-setup` под git, `install.sh` ставит
  симлинки в `~/.omp/agent`. Провайдеры `agent-lb` (openai-codex-responses, тот же путь, что у codex) и
  `copilot-x5` (openai-completions). Роли: default sol/medium, slow и plan sol/high, smol, commit и tiny luna,
  vision copilot-code-flash, fallback sol→terra.
- **Профиль x5.** Отдельный профиль omp (`omp-setup/profiles/x5`): включён только провайдер `copilot-x5`,
  все роли (default, smol, slow, plan, task, advisor, vision, commit, tiny) указывают на x5-модели.
  x5-исполнители диспетчера всегда запускаются в нём.
- **omp как исполнитель.** Адаптер `executors/omp.py` на `omp -p --mode json`: одноразовый процесс, как у
  codex. RPC-режим отвергнут, долгоживущий процесс диспетчеру не нужен.
- **Ключи.** Обёрток нет. `models.yml` берёт ключи командой (`apiKey: "!…"`), которая читает
  `~/.config/agent-lb/env` или хранилище omp. Ключ не попадает в окружение процесса и не виден bash агента.
- **Скилы.** Копии в `omp-setup/skills`, переписанные под `ask` и `task`, с `source:` и `source_sha:` в шапке;
  `omp-setup drift` работает в pre-commit omp-setup. agent-dispatch об этом не знает.
- **gpt-review.** Новый репо `~/my_git_reps/gpt-review` с историей codex-review: раннер вызывает
  `omp -p --mode json --model agent-lb/gpt-6.1-sol` с инструментами только на чтение, salvage делает через `--resume`.

## Technical Details
- Вызов исполнителя: `omp -p --mode json [--profile x5 | PI_CONFIG_DIR] --session-dir <data_dir>/omp-sessions
  --model <provider/id> --thinking <level> --max-time <timeout>s --cwd <dir> --approval-mode yolo "<Task Package>"`.
  Механизм выбора профиля (флаг или переменная) фиксируется в Task 1.
- Ревью: тот же вызов с `--tools <только чтение>`; имена инструментов снимаются в Task 1.
- `result.meta.session_id` = id из заголовка сессии; follow-up = `--resume <id>` в том же `cwd` и профиле.
- `cost_usd` из `usage` последнего `message_end`, если omp отдаёт стоимость; иначе `null`.
- `health.classify_failure` разбирает структурированную ошибку из JSON (`httpStatus`, `retryable`, `code`,
  `usageLimit`) и stderr:
  - лимит и 429 дают остывание группы с `parse_reset`;
  - ошибки авторизации, включая сбой команды `apiKey`, дают отказ авторизации;
  - ход без единого ответа модели даёт остывание, это аналог `opencode: no model response`, если Task 1
    покажет, что omp в этом случае выходит с кодом 0;
  - текст агента и ошибки MCP остывания не вызывают (правило проекта).
- Конфиг:
  - `adapter: Literal["claude", "omp"]` для новых записей;
  - исполнители `omp/sol` и `omp/terra` (balanced) и `omp/luna` (fast) с `limit_group: agent-lb` и
    `limit_reset: recheck`;
  - `omp/x5-code-large` (balanced, `corporate: true`, профиль x5), `omp/x5-small-coder`, `omp/copilot-code-flash`
    и `omp/copilot-code-flash-thinking` с прежними `enabled`;
  - `resolved_limit_group` для omp строится по провайдеру из `model` (`omp:agent-lb`, `omp:copilot-x5`),
    как сейчас у opencode;
  - `fallback_executor: omp/sol`, цепочки `escalation` переписываются на `omp/*`;
  - `review_only: {omp: [claude]}`.
- `SourceAgent` и наборы источников получают `omp`, а `codex` и `opencode` в них остаются: старые строки БД,
  follow-up по старым задачам и replay их читают.
- `AGENT_DISPATCH_SOURCE_AGENT=omp` в MCP-конфиге omp; проброс `AGENT_DISPATCH_HOP`, `_TASK_ID` и `_ROOT_AGENT`
  проверяет `doctor`.
- Миграция пользовательского конфига: `agent-dispatch migrate-config` (или скрипт в `scripts/`) переписывает
  `codex/*` и `opencode/*` в `omp/*` и сохраняет бэкап рядом. Без миграции демон отказывается стартовать
  с понятной ошибкой, которая называет команду миграции.

## What Goes Where
- **Implementation Steps**: код и тесты в agent-dispatch, omp-setup, gpt-review и agterm-kit, скилы, конфиги
- **Post-Completion**: замер replay через неделю, ручная проверка интерактивного omp на рабочей задаче

## Waves (для plan-dispatch)
- W1: Task 1
- W2 (параллельно): Task 2, Task 4, Task 8, Task 9
- W3: Task 3 (нужны 1 и 2), Task 7 (нужен 2)
- W4: Task 5, затем Task 6
- W5: Task 10 (последняя кодовая), затем Task 11, Task 12, Task 13, Task 14

## Implementation Steps

### Task 1: Установить omp и снять живые форматы
**Depends on:** none
**Files:**
- Create: `tests/fixtures/omp/`
- Create: `docs/research/omp-cli-facts.md`

- [x] `bun install -g @oh-my-pi/pi-coding-agent`, записать версию
- [x] снять `omp -p --mode json` на тестовом репо: заголовок сессии, `message_end`, `usage`, стоимость, коды выхода
- [x] фикстуры: успех, сбой хода, 429 балансера, неверный ключ, недоступный copilot-x5, ход без ответа, `--max-cost`, ревью только на чтение, resume
- [x] снять ошибки: неверный ключ балансера, 429 или лимит аккаунта, недоступный copilot-x5, ход без ответа модели (какой код выхода), `--max-cost`
- [x] проверить `apiKey: "!команда"` и что `env | grep AGENT_LB` из bash агента пуст
- [x] выяснить: как выбирается профиль (флаг или `PI_CONFIG_DIR`), имена инструментов только на чтение, `--resume` в `-p`, API хуков (начало и конец хода, PreToolUse), проброс env в MCP
- [x] проверить, что в профиле только с copilot-x5 сабагент `task` и prewalk не уходят к другим провайдерам
- [x] разложить фикстуры без секретов (`detect-secrets`), записать факты в `docs/research/omp-cli-facts.md`
- ⚠️ не снято: 429 и исчерпанный лимит балансера, ход без ответа модели у x5 (фикстур нет, см. `docs/research/omp-cli-facts.md`)

### Task 2: Репозиторий omp-setup с конфигом и профилем x5
**Depends on:** Task 1
**Repo:** `~/my_git_reps/omp-setup` (cwd раздачи)
**Files:**
- Create: `models.yml`, `config.yml`, `mcp.yml`, `AGENTS.md`, `install.sh`, `Makefile`, `README.md`, `profiles/x5/`, `tests/` (в omp-setup)

- [ ] `models.yml`: провайдеры `agent-lb` и `copilot-x5`, модели и лимиты из opencode, `apiKey: "!…"` без значений в файле
- [ ] `config.yml`: роли, `retry.fallbackChains` sol→terra, `approvalMode: yolo`, `enabledProviders` без Claude и Codex
- [ ] профиль x5: только `copilot-x5`, все роли на x5-моделях
- ➕ профиль x5: `enabledModels: [copilot-x5/*]`, `retry.maxRetries: 1` не нужен интерактиву, только оверлею диспетчера
- ➕ `bin/x5-key` и `bin/lb-key`: команды для `apiKey: "!…"` (ключ x5 из Keychain `copilot-x5`, ключ балансера из `~/.config/agent-lb/env`); перенос ключа x5 в Keychain делает координатор в Task 12
- ➕ выгрузка корпоративного `CN=RootCA` (System.keychain, SHA-1 F75DAB43…) в `~/.omp/agent/x5-root.pem` в `install.sh`; `NODE_EXTRA_CA_CERTS` для интерактивного запуска
- ➕ `dispatch-overlay.yml` для `--config`: `retry: {maxRetries: 1}`
- [ ] MCP: mcphub, agent-dispatch (`AGENT_DISPATCH_SOURCE_AGENT=omp`, проброс `AGENT_DISPATCH_*`), context7
- [ ] `AGENTS.md` из `~/.codex/AGENTS.md` и `~/.config/opencode/AGENTS.md`; `salt-qa-agent-kit/AGENTS.md` только для `~/x5`
- [ ] `install.sh`: симлинки в `~/.omp/agent` с бэкапом существующих файлов, идемпотентно
- [ ] dcg как хук omp, если API хуков позволяет, иначе ⚠️ с причиной
- [ ] тесты: YAML валиден, роли ссылаются на объявленные модели, в профиле x5 нет моделей и провайдеров вне copilot-x5, в репо нет секретов, `install.sh` идемпотентен на временном HOME
- [ ] `make test` зелёный, коммит omp-setup

### Task 3: Адаптер executors/omp.py
**Depends on:** Task 1, Task 2
**Files:**
- Create: `agent_dispatch/executors/omp.py`
- Modify: `agent_dispatch/executors/registry.py`, `agent_dispatch/executors/result_parser.py`, `agent_dispatch/schemas/__init__.py`
- Create: `tests/test_adapter_omp.py`, `tests/fakes/omp_ok.sh`, `tests/fakes/omp_env_capture.sh`

- [ ] сборка команды: модель, thinking, `--max-time`, `--session-dir`, `--cwd`, профиль x5 для `corporate: true`
- ➕ запуск с `stdin=DEVNULL` (без этого omp ждёт EOF на stdin), `--config <dispatch-overlay.yml>`, `NODE_EXTRA_CA_CERTS` для x5
- ➕ из env omp вырезать `AGENT_LB_API_KEY` (его экспортирует shell пользователя, и он доходит до bash агента)
- ➕ режим ревью: `--tools read,grep,glob` (инструмента `find` нет, код 2)
- [ ] `streams_output = True`, разбор NDJSON: `session_id`, итог из последнего `message_end`, `usage` и стоимость
- [ ] режим ревью с инструментами только на чтение; follow-up через `--resume` в том же профиле
- [ ] `last_output_age_s` на жёстком таймауте
- [ ] тесты на фикстурах Task 1: успех, сбой хода, пустой вывод, обрыв потока, ревью, resume
- [ ] тесты argv и env: x5-исполнитель всегда с профилем x5, в env нет `AGENT_LB_API_KEY` и секретов `Settings.secrets`
- [ ] гейт зелёный

### Task 4: Распознавание сбоев omp в health.py
**Depends on:** Task 1
**Files:**
- Modify: `agent_dispatch/executors/health.py`, `tests/test_health.py`

- [x] лимит и 429 → остывание с `parse_reset`; auth и сбой команды `apiKey` → отказ авторизации; сеть → обычный сбой
- ➕ в `-p --mode json` нет `httpStatus`: классификация по `errorMessage` ассистентского `message_end` со `stopReason: error` и по stderr (`No API key found for <provider>.` = auth, `Invalid API key` = auth, `Couldn't connect` = сеть, TLS = сеть)
- ➕ незнакомый `errorMessage` не роняет классификатор (обычный сбой без остывания)
- [x] ход без ответа модели → остывание (аналог `opencode: no model response`), по фактам Task 1
- [x] текст агента и ошибки MCP не вызывают остывания
- [x] тесты на фикстурах Task 1, включая ложные срабатывания (слова rate limit в выводе агента)
- [x] гейт зелёный

### Task 5: Конфиг, guards и источники
**Depends on:** Task 3
**Files:**
- Modify: `agent_dispatch/config.py`, `agent_dispatch/config_default.yaml`, `config.example.yaml`, `agent_dispatch/models.py`, `agent_dispatch/routing/guards.py`, `evals/replay/harness.py`
- Modify: `tests/test_config.py`, `tests/test_config_example.py`, `tests/test_guards.py`, `tests/test_models.py`, `tests/test_limits.py`, `tests/test_replay.py`, `tests/fixtures/config/{full,bad_escalation}.yaml`

- [ ] исполнители `omp/*`, `adapter` omp, `fallback_executor: omp/sol`, цепочки `escalation`, `review_only: {omp: [claude]}`
- [ ] `resolved_limit_group` для omp по провайдеру из `model`; `limit_reset: recheck` для agent-lb
- [ ] `SourceAgent` и наборы источников получают `omp`, `codex` и `opencode` остаются для истории
- [ ] `evals/replay/harness.py`: группы лимитов и `review_only` для omp, исторические `codex/*` и `opencode/*` по-прежнему разбираются
- [ ] тесты: периметр выбирает только `omp/x5-*`, `review_only` для источника `omp`, группы лимитов, replay на исторической задаче `codex/sol` и `opencode/x5-code-large`, example равен default
- [ ] гейт зелёный

### Task 6: Миграция пользовательского конфига и doctor
**Depends on:** Task 5
**Files:**
- Modify: `agent_dispatch/cli.py`, `agent_dispatch/config.py`, `agent_dispatch/doctor.py`
- Modify: `tests/test_cli.py`, `tests/test_doctor.py`

- [ ] команда `agent-dispatch migrate-config` в `cli.py`: `codex/*` и `opencode/*` → `omp/*`, бэкап рядом, идемпотентно, `--dry-run` печатает дифф
- [ ] старый конфиг без миграции даёт ошибку старта с именем команды миграции, а не трейсбек pydantic
- [ ] doctor: `codex-env-vars` → `omp-mcp-env` (проброс `AGENT_DISPATCH_*`), версия omp не ниже зафиксированной, профиль x5 без внешних провайдеров
- [ ] тесты: миграция полного конфига, повторная миграция, неизвестный исполнитель, ошибка старта, каждая проверка doctor в зелёном и красном случае
- [ ] гейт зелёный

### Task 7: Перенос скилов в omp-setup
**Depends on:** Task 2
**Repo:** `~/my_git_reps/omp-setup` (cwd раздачи)
**Files:**
- Create: `skills/`, `bin/omp-setup`, `tests/test_drift.py` (в omp-setup)

- [ ] переписать 12 скилов под `ask` и `task`; пути к скриптам (glflow, huddle.py, agterm) абсолютные, без копий
- [ ] `source:` и `source_sha:` в шапке каждого
- [ ] `omp-setup drift`: пересчёт sha оригиналов, список устаревших, код 1 при дрейфе; хук pre-commit
- [ ] тесты: дрейф есть, дрейфа нет, оригинал удалён, шапки нет
- [ ] `make test` зелёный, коммит omp-setup

### Task 8: gpt-review вместо codex-review
**Depends on:** Task 1
**Repo:** `~/my_git_reps/gpt-review` (cwd раздачи)
**Files:**
- Modify: `bin/gpt-review`, `SKILL.md`, `README.md`, `tests/` (в gpt-review, репо создаётся клоном codex-review до раздачи)
- Ссылки в `~/.claude/skills/plan-dispatch` и `review-refactor` правит координатор: исполнитель вне репо не пишет

- [ ] раннер вызывает `omp -p --mode json --model agent-lb/gpt-6.1-sol` с инструментами только на чтение и `--max-time`
- [ ] бюджет команд считается по событиям tool-call из NDJSON; salvage через `--resume`
- [ ] скил `gpt-review` (триггеры «gpt review», «ревью gpt», «второе мнение»); симлинк `~/.claude/skills/gpt-review`, симлинк `codex-review` убрать
- [ ] ссылки в plan-dispatch и review-refactor поменять на gpt-review
- [ ] тесты раннера на фикстурах omp: полный ответ, обрыв по бюджету, обрыв по времени, битый JSON
- [ ] `make test` зелёный, коммит gpt-review

### Task 9: Интеграция с agterm
**Depends on:** Task 1
**Repo:** `~/my_git_reps/agterm-kit` (cwd раздачи)
**Files:**
- Modify: `ask/`, `tests/` (в agterm-kit)

- [ ] статус omp в сайдбаре agterm через хуки начала и конца хода (если Task 1 подтвердил API, иначе ⚠️)
- [ ] `agterm-kit/ask`: установка рядом с конфигом omp
- [ ] bats-тесты по правилам agterm-kit, `make test lint` зелёный, коммит agterm-kit

### Task 10: Удаление адаптеров codex и opencode (последняя кодовая задача)
**Depends on:** Tasks 3-9
**Files:**
- Delete: `agent_dispatch/executors/codex.py`, `agent_dispatch/executors/opencode.py`, `scripts/codex-agent-lb`
- Delete: `tests/test_adapter_codex.py`, `tests/test_adapter_opencode.py`, `tests/test_codex_agent_lb.py`, `tests/fakes/{codex_ok.sh,opencode_ok.sh,opencode_env_capture.sh,opencode_pwd.py}`, `tests/fixtures/agent_output/codex_exec_json.txt`, `tests/snapshots/prompt_codex_summary.md`
- Modify: `agent_dispatch/dispatch/{dispatcher,task_package,report_recovery}.py`, `agent_dispatch/executors/{base.py,health.py,CLAUDE.md}`, `agent_dispatch/schemas/__init__.py`, `tests/CLAUDE.md`
- Modify (тесты с codex и opencode в данных): `test_api, test_autostart, test_budget, test_capability, test_claude_local_router, test_cli, test_decision, test_dispatcher, test_dispatcher_worktree, test_env, test_escalation, test_evals_harness, test_exploration, test_followup, test_jev_router, test_mcp_server, test_queue_fallback, test_registry, test_report_recovery, test_result_parser, test_review, test_storage, test_task_package, test_verify`; `tests/fakes/claude_router_{capture,ok}.sh`, `tests/fixtures/jev/request_{capability,executor}.json`, `tests/snapshots/prompt_claude_{full,prompt,summary}.md`
- Modify: `evals/{executors,workflow,scheduling}/harness.py`, `evals/launcher/__main__.py`, `evals/routing/cases.jsonl`, `evals/workflow/cases.jsonl`, `evals/executors/corpus.jsonl`, `evals/README.md`, `evals/scheduling/README.md` (отчёты в `evals/reports/` остаются историей)

- [ ] убрать ветки codex и opencode в health, task_package, report_recovery, dispatcher и schemas
- [ ] перевести тестовые данные и фейки на `omp/*`; snapshots пересобрать и просмотреть дифф
- [ ] evals переключить на `omp/*`
- [ ] `grep -rn "codex\|opencode" agent_dispatch evals tests` чист, кроме истории в replay, `SourceAgent` и отчётов
- [ ] гейт зелёный, коммит agent-dispatch

### Task 11: Сервис opencode и ссылки на старые CLI
**Depends on:** Task 10
- [ ] остановить и выгрузить сервис opencode (`~/.config/opencode/service.json`), конфиг не удалять
- [ ] заменить упоминания codex и opencode CLI на omp в `~/.claude/CLAUDE.md`, скилах и AGENTS.md
- [ ] codex CLI не удалять (часть ChatGPT.app), `~/.codex/config.toml` оставить

### Task 12: Переключение одним шагом
**Depends on:** Task 11
- [ ] записать откат в `docs/research/omp-cli-facts.md`: путь к бэкапу конфига, команда установки прежней версии uv tool (коммит до Task 10), рестарт демона
- [ ] `agent-dispatch migrate-config` с бэкапом, переустановка uv tool, перезапуск демона (memory installed-tool-is-a-copy), `doctor` на установленной копии
- [ ] `uv run agent-dispatch doctor --online` зелёный
- [ ] `uv run python -m evals.smoke --executors omp/sol,omp/terra,omp/luna,omp/x5-code-large` зелёный
- [ ] живой периметр: x5-задача с вызовом сабагента, в логе сессии только copilot-x5
- [ ] `omp models agent-lb`, `omp models copilot-x5`; `omp -p` через каждую роль; `tools/list` каждого MCP; каждый из 12 скилов одной задачей

### Task 13: Verify acceptance criteria
**Depends on:** Task 12
- [ ] все пункты Overview выполнены
- [ ] граничные случаи: нет ключа, 429, недоступен copilot-x5, ход без ответа модели, таймаут с изменениями, follow-up, ревью только на чтение, старый конфиг без миграции
- [ ] полный гейт: `uv run pytest tests -q`, ruff, `make test` в omp-setup и gpt-review, `make test lint` в agterm-kit
- [ ] `evals.replay --json` без ошибок разбора на истории

### Task 14: [Final] Документация
**Depends on:** Task 13
- [ ] README и `docs/architecture.md` agent-dispatch: omp вместо codex и opencode
- [ ] `CLAUDE.md` agent-dispatch: переписать codex-правила (`codex-agent-lb`, `codex-env-vars`, флаги до `resume`, `thread_id`, события переподключения, дефолт `review_only`, `opencode: no model response`) на omp
- [ ] `agent_dispatch/executors/CLAUDE.md` и `tests/CLAUDE.md`
- [ ] README omp-setup и gpt-review
- [ ] перенести план в `docs/plans/completed/`

## Post-Completion
- через неделю после переключения: `uv run python -m evals.replay --json`, сравнить долю `completed` у `omp/*`
  с прежней долей codex и opencode, проверить `perimeter_violations = 0`
- ручная работа в интерактивном omp на рабочей задаче в `~/x5`: скилы, MCP, ask, роли
- эксперимент `explore_share` (memory explore-share-experiment) пересчитать: исполнители сменили имена
