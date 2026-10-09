# Тесты AgentDispatch

- Фикстуры: `tests/fixtures/jev/` (живые ответы Jev), `tests/fixtures/agent_output/` (живые выводы claude/codex/opencode), fake-CLI в `tests/fakes/`. Session-фикстура прогревает fake-скрипты (первый exec на macOS медленный).
- `tests/conftest.py:git_repo` чистит `GIT_*` из env: внутри git-хука вложенный `git init` иначе делает основную репу bare.
