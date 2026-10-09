# Адаптеры исполнителей

- OpenCode в режиме `run` прерывает сессию при отклонённом запросе разрешения; адаптер сообщает `failed` с `opencode permission rejected: ...` вместо молчаливого `partial`.
- Codex strict-schema: `agent_dispatch/schemas/agent_result.strict.schema.json`, обычную схему Codex отвергает.
