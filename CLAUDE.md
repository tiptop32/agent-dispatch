# AgentDispatch

Сервис маршрутизации coding-задач между Claude Code, Codex и OpenCode. Архитектура в `docs/architecture.md`, план и история решений в `docs/plans/`.

## Команды

- `uv sync`; `uv run pytest tests -q` (gate, без сети и без настоящих CLI, ~1 мин из-за вложенного pytest в тестах evals-харнесса)
- `uv run ruff check . && uv run ruff format --check .`
- `uv run pre-commit install` (ruff, detect-secrets, pytest gate)
- `uv run agent-dispatch doctor [--online]`, `uv run agent-dispatch serve`
- Evals (платные): `uv run python -m evals.routing`, `uv run python -m evals.smoke --executors codex`

## Правила проекта

- Jev не делает side effects: он только выбирает исполнителя. Guards, таймауты, запуск живут в коде.
- Ключи никогда не попадают в `os.environ` и в env дочерних процессов: `Settings.secrets` (SecretStr) и `executors/env.py:child_env`.
- Env дочерних CLI также без `CLAUDECODE`/`CLAUDE_CODE_*`, иначе `claude -p` отказывается работать вложенно.
- MCP и демон используют установленный `agent-dispatch` как non-editable копию uv tool. После изменений кода переустановите её перед добавлением новых ключей в пользовательский конфиг: проверка `install` в `doctor` находит устаревшую копию.
- Инкремент hop только в демоне (`AGENT_DISPATCH_HOP = hop + 1`); прокси передаёт значение без изменений.
- Исполнители и модели задаются только в config.yaml (`executors`, `escalation`); в коде нет списка моделей.
- Jev выбирает уровень работы (`capability`: fast/balanced/strong), а не имя модели: имена неразличимы для модели и дают плоское распределение. Соответствие уровня и исполнителя живёт в `routing/capability.py` и поле `tier` конфига.
- `corporate: true` у исполнителя это периметр данных, а не цена: он выигрывает у `judgment` при выборе.
- Ответ Jev о корпоративных данных это `noul`, и его уверенность (`|p - 0.5| * 2`) бывает около нуля. Порог `routing.corporate_min_confidence` (по умолчанию 0, то есть прежнее поведение) не даёт сужать пул по монетке. Поднятие порога ослабляет периметр данных — это решение человека, а не дефолт.
- `routing.corporate_perimeter: false` выключает периметр целиком: вопрос `corporate_data` Jev не задаётся, пул не сужается. По умолчанию `true`; выключение это решение владельца данных в его конфиге.
- Если внутри периметра нет исполнителя нужного тира, `select` пишет это в `selection_notes`, а не понижает уровень молча.
- `partial` с `parse_error` и пустым `changed_files` эскалируется (`no_result`): исполнитель не отчитался и ничего не тронул. При непустом `changed_files` эскалации нет — работа есть, судит о ней вызывающий по дифу, повторный запуск лёг бы поверх.
- В режиме `execution.workspace_mode: worktree` исполнитель работает в отдельном git worktree от HEAD, а результат возвращается патчем; lock на `cwd` берётся только на время интеграции.
- Исполнитель не коммитит (запрещено в Task Package), коммитит демон: `worktree.commit` на ветке задачи, с `--no-verify`. Хуки репозитория на служебном коммите не гоняются осознанно — pre-commit с полным прогоном тестов падал бы на недоделанной задаче. Гейт — коммит вызывающего после ревью дифа.
- `worktree.build_patch` считает диф от `Worktree.base`, а не от HEAD: после промежуточного коммита `diff --cached HEAD` вернул бы пустоту (есть тест на равенство патча до и после коммита).
- `integrate: branch` оставляет ветку без worktree. Такие ветки показывает `worktree.list_branches`, иначе они копились бы незаметно.
- «Демон запущен» решает порт, а не pid: `serve_state.is_running` это живой pid И занятый порт. `os.kill(pid, 0)` истинен и для зомби, и для процесса, застрявшего в shutdown, а такой pid в `serve.json` запирал старт нового демона навсегда.
- Отмена задачи, перезапуск демона и сбой интеграции worktree не удаляют: в нём лежит работа исполнителя, и выбрасывать её демон не вправе. Но путь и ветка обязаны попасть в `result.meta` (`_note_kept_worktree` на срыве, `_worktree_meta` в `recover_stale`), иначе дерево не видно ни в `status`, ни вызывающему. Единственный сборщик мусора — `agent-dispatch worktrees --clean`, его запускает человек.
- `config.example.yaml` равен `agent_dispatch/config_default.yaml` (есть тест).
- Фикстуры: `tests/fixtures/jev/` (живые ответы Jev), `tests/fixtures/agent_output/` (живые выводы claude/codex/opencode), fake-CLI в `tests/fakes/`. Session-фикстура прогревает fake-скрипты (первый exec на macOS медленный).
- Codex strict-schema: `schemas/agent_result.strict.schema.json`, обычную схему Codex отвергает.
- `tests/conftest.py:git_repo` чистит `GIT_*` из env: внутри git-хука вложенный `git init` иначе делает основную репу bare.
