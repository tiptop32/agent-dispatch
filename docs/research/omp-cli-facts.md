# omp CLI: факты из живых прогонов

Снято 2026-11-02 на omp 18.15.0 (bun 1.4.4, macOS). Пробы и сырые выводы лежат в
`/tmp/claude/omp-probe` (`probe.py`), фикстуры для тестов в `tests/fixtures/omp/`. Источник фактов
здесь живой прогон, а не документация; где факт не снят, это сказано.

## Установка

- `bun install -g @oh-my-pi/pi-coding-agent` ставит `omp` в `~/.bun/bin/omp`, сам пакет лежит в
  `~/node_modules/@oh-my-pi/pi-coding-agent`.
- omp 18.15.0 требует bun ≥ 1.4.4, на 1.3.14 он не стартует (`Bun runtime must be >= 1.4.4`).
- bun заблокировал два postinstall-скрипта пакета, omp работает и без них.

## Запуск без TTY

- Если stdin не TTY, omp ждёт EOF на stdin даже при промпте в аргументе
  (`Reading prompt from piped stdin (waiting for EOF…)`) и висит до таймаута. Адаптер обязан
  запускать его с `stdin=DEVNULL`.
- Логи пишутся в `~/.omp/logs/` даже при `PI_CODING_AGENT_DIR`.

## Поток `-p --mode json`

- Первая строка `{"type":"session","version":3,"id":"<uuid7>","cwd":…}`, это id для `--resume`.
- События: `advisor_cost_changed`, `agent_start`, `turn_start`, `message_start`, `message_update`,
  `message_end`, `tool_execution_start|update|end`, `turn_end`, `agent_end`, `auto_retry_start|end`, `notice`.
- Итог берётся из последнего `message_end` с `message.role == "assistant"`: `content[].text`,
  `stopReason` (`stop`, `toolUse`, `error`), `errorMessage`, `provider`, `model`, `usage`.
- `usage` = `{input, output, cacheRead, cacheWrite, totalTokens, reasoningTokens?, cost: {…, total}}`.
  У agent-lb `cost.total` в долларах ненулевой, у copilot-x5 он всегда 0.

## Коды выхода и ошибки

| Случай | Код | Где видно | Фикстура |
|---|---|---|---|
| успех | 0 | `stopReason: stop` | `ok.ndjson`, `x5_ok.ndjson` |
| неверный ключ | 1 | `stopReason: error`, `errorMessage: "Invalid API key"` | `badkey.ndjson` |
| провайдер недоступен | 1 | `errorMessage: "Couldn't connect to … (connection refused)…"`, события `auto_retry_*` | `down.ndjson` |
| команда `apiKey` упала | 1 | только stderr: `error: No API key found for <provider>.` и стек, в stdout одна строка `session` | `nokey.stderr.txt` |
| `--max-cost` | 3 | событие `notice`, последний ход `toolUse` | `maxcost.ndjson` |
| TLS без корпоративного CA | 1 | `errorMessage: "TLS certificate verification failed for api-copilot.x5.ru (UNABLE_TO_GET_ISSUER_CERT_LOCALLY)…"` | (не сохранена) |

- В режиме `-p --mode json` структурированных полей `httpStatus`, `retryable` и `usageLimit` нет
  (по документации они есть только в RPC). Классифицировать приходится по `errorMessage`
  ассистентского `message_end` со `stopReason: error` и по stderr. Это не текст агента, поэтому
  правило проекта «слова агента не вызывают остывания» соблюдается.
- **Не снято:** 429 и исчерпанный лимит балансера, ход без единого ответа модели у x5 (аналог
  `opencode: no model response`). На них сейчас нет фикстур, и `health.py` обязан не падать на
  незнакомом `errorMessage`.

## Повторы

- По умолчанию `retry.maxRetries: 10` с экспоненциальной паузой (до 8 с на шаг), поэтому сбой
  провайдера тянется минутами. Для диспетчера нужен оверлей `--config <yaml>` с `retry: {maxRetries: 1}`:
  повторами занимается эскалация диспетчера.

## Ключи

- `apiKey: "!<команда>"` работает: ключ берётся из stdout команды. Сложную команду лучше выносить
  в скрипт, потому что кавычки внутри YAML ломают разбор файла (`models.yml validation failed`).
- Окружение запускающего процесса доходит до bash агента: `AGENT_LB_API_KEY`, выставленный
  родителем, агент видит (`env | grep -c` = 1). Без ключа в окружении и с `apiKey: "!…"` агент видит 0
  (`leak_none.ndjson`). Сейчас `AGENT_LB_API_KEY` экспортирует профиль shell пользователя, поэтому
  диспетчер обязан вырезать его из env omp. Интерактивный omp, запущенный из такого shell, ключ видит.
- Ключ copilot-x5 сейчас лежит в `~/.local/share/opencode/auth.json` (`copilot.key`). Для omp его
  лучше перенести в Keychain (`security find-generic-password -s copilot-x5 -w`), чтобы не зависеть от opencode.

## TLS copilot-x5

- `api-copilot.x5.ru` подписан цепочкой `sre-vault.x5.ru` → `CN=RootCA` (корпоративный, в
  `/Library/Keychains/System.keychain`, SHA-1 `F75DAB43B5F21B4C2AB09CC4EFE0D6FA0ECF9D32`, действует до 2030-07-09).
- bun по умолчанию системному Keychain не доверяет. Два рабочих способа: `NODE_USE_SYSTEM_CA=1` (или `BUN_OPTIONS=--use-system-ca`) — bun читает Keychain, как opencode, собранный с `--use-system-ca`; выбран он (проверено 2026-11-03, PONG через профиль x5). Прежний способ: omp учитывает `NODE_EXTRA_CA_CERTS`: с выгруженным PEM
  x5 отвечает. omp-setup должен выгружать этот CA в PEM, а адаптер и интерактивный запуск должны
  передавать путь к нему.

## Модели и профили

- Профиль выбирается флагом `--profile <name>` (`~/.omp/profiles/<name>/agent/`) или `OMP_PROFILE`;
  `PI_CODING_AGENT_DIR` действует только на профиль по умолчанию.
- Профиль x5 с `modelRoles` целиком на copilot-x5 и `enabledModels: [copilot-x5/*]`: x5-задача,
  запустившая сабагента `task`, во всех четырёх файлах сессии (включая сессию сабагента) обращалась
  только к `copilot-x5` (`x5_task.ndjson`). Периметр держится.
- Из 11 моделей opencode omp показывает 9: варианты `*-thinking` склеиваются с базовой моделью, а
  уровень рассуждений задаётся `--thinking`.
- Пользователь экспортирует `ANTHROPIC_BASE_URL` и `ANTHROPIC_CUSTOM_HEADERS`. В профиле x5 они не
  дают доступа к другим провайдерам из-за `enabledModels`, это проверено прогоном выше.

## Режим только чтения и продолжение

- `--tools read,grep,glob`: агент не может писать (`readonly.ndjson`, файл не создан).
  Инструмента `find` нет: `--tools …,find` даёт код 2 и `Built-in tool unavailable in this session: find`.
- `--resume <id>` в `-p` с тем же `--session-dir` и `cwd` продолжает сессию: id в заголовке тот же,
  модель помнит прошлый ответ (`resume.ndjson`).

## Хуки и MCP

- Хуки делаются TS-расширениями с `pi.on(event)`: `agent_start`, `agent_end`, `turn_start`, `turn_end`,
  `tool_call` (может вернуть `{block: true, reason}`). На этом строятся статус agterm и dcg.
- MCP пользователя: `~/.omp/agent/mcp.json` (у профиля `~/.omp/profiles/<name>/agent/mcp.json`).
  Значение `env` вида `"AGENT_DISPATCH_HOP": "AGENT_DISPATCH_HOP"` копируется из окружения omp,
  `"!команда"` выполняется. Сабагенты наследуют MCP родителя (по документации, не проверено).

## Откат переключения (Task 12)

- Бэкап пользовательского конфига диспетчера: `~/.config/agent-dispatch/config.yaml.bak-20261102-pre-omp`.
- Прежний код: ветка `main` (до переключения там нет адаптера omp); последний коммит ветки
  `omp-migration`, где адаптеры codex и opencode ещё в коде: `d477bdf`.
- Откат:
  ```sh
  cp ~/.config/agent-dispatch/config.yaml.bak-20261102-pre-omp ~/.config/agent-dispatch/config.yaml
  git -C ~/my_git_reps/agent-dispatch worktree add /tmp/agent-dispatch-main main
  uv tool install --force --reinstall /tmp/agent-dispatch-main
  kill "$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.local/share/agent-dispatch/serve.json")))["pid"])')"
  # следующий вызов dispatch/route поднимет демон заново
  ```
- omp-конфиг: `install.sh` omp-setup переносит чужие файлы в `*.bak-<дата>` рядом в `~/.omp/agent`.
