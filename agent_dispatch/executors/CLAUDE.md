# Адаптеры исполнителей

- OpenCode в режиме `run` прерывает сессию при отклонённом запросе разрешения; адаптер сообщает `failed` с `opencode permission rejected: ...` вместо молчаливого `partial`.
- OpenCode, в котором модель не ответила ни разу (нет `text`, `tool_use`, `step_finish`; без изменений и без события ошибки), даёт `failed` с `opencode.NO_RESPONSE` вместо `partial`: так opencode молча выходит с кодом 0, когда запрос к модели не вернулся (x5-code-large 2026-10-19…21). `health.classify_failure` считает строку с начала `network`, и вся группа лимита остывает.
- Codex strict-schema: `agent_dispatch/schemas/agent_result.strict.schema.json`, обычную схему Codex отвергает.
