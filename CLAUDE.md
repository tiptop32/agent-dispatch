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
- Инкремент hop только в демоне (`AGENT_DISPATCH_HOP = hop + 1`); прокси передаёт значение без изменений.
- Исполнители и модели задаются только в config.yaml (`executors`, `escalation`); в коде нет списка моделей.
- `config.example.yaml` равен `agent_dispatch/config_default.yaml` (есть тест).
- Фикстуры: `tests/fixtures/jev/` (живые ответы Jev), `tests/fixtures/agent_output/` (живые выводы claude/codex/opencode), fake-CLI в `tests/fakes/`. Session-фикстура прогревает fake-скрипты (первый exec на macOS медленный).
- Codex strict-schema: `schemas/agent_result.strict.schema.json`, обычную схему Codex отвергает.
- `tests/conftest.py:git_repo` чистит `GIT_*` из env: внутри git-хука вложенный `git init` иначе делает основную репу bare.
