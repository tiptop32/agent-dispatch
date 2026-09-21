from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load_agent_result_schema() -> dict[str, Any]:
    path = Path(__file__).with_name("agent_result.schema.json")
    return json.loads(path.read_text())


def validate_agent_result(obj: Any) -> list[str]:
    schema = load_agent_result_schema()
    return [error.message for error in Draft202012Validator(schema).iter_errors(obj)]


def load_agent_result_strict_schema() -> dict[str, Any]:
    """Схема для `codex exec --output-schema`: OpenAI strict mode требует все ключи
    в `required` и `additionalProperties: false` на каждом уровне; необязательные
    поля объявлены nullable. Проверено вживую 2026-09-21."""
    path = Path(__file__).with_name("agent_result.strict.schema.json")
    return json.loads(path.read_text())
