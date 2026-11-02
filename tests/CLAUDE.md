# Тесты AgentDispatch

- Фикстуры живых выводов omp лежат в `tests/fixtures/omp/`; это выводы omp 18.15.0, зафиксированные в `docs/research/omp-cli-facts.md`. Общие фикстуры Jev лежат в `tests/fixtures/jev/`, fake-CLI — в `tests/fakes/`.
- Тесты адаптера проверяют `stdin=DEVNULL`, `--config ~/.omp/agent/dispatch-overlay.yml`, `retry.maxRetries: 1`, `AGENT_LB_API_KEY` в env, профиль x5 и `NODE_EXTRA_CA_CERTS`.
- Тесты потока omp используют id из первой строки `{"type":"session"}`, `--resume` в том же cwd, session-dir и профиле, а usage и cost суммируют по всем ассистентским `message_end`.
- Тесты `doctor` проверяют `omp-mcp-env`, `omp-version` и `omp-x5-profile`; старый конфиг проверяется через ошибку с подсказкой `agent-dispatch migrate-config`.
- `tests/conftest.py:git_repo` чистит `GIT_*` из env: внутри git-хука вложенный `git init` иначе делает основную репу bare.
