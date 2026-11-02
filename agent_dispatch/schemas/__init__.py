from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load_agent_result_schema() -> dict[str, Any]:
    path = Path(__file__).with_name("agent_result.schema.json")
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def _agent_result_validator() -> Draft202012Validator:
    """Валидатор строится один раз: схема разбирается на каждый результат исполнителя."""
    return Draft202012Validator(load_agent_result_schema())


def validate_agent_result(obj: Any) -> list[str]:
    return [error.message for error in _agent_result_validator().iter_errors(obj)]


def load_omp_session_schema() -> dict[str, Any]:
    """Схема конверта NDJSON-событий `omp -p --mode json` (факты Task 1).

    Проверяет фикстуры `tests/fixtures/omp/`: известные `type`, объект `message`
    с ролью, контентом и `usage`. Поля событий не перечислены полностью: omp
    добавляет новые, незнакомые поля фикстуру ломать не должны.
    """
    path = Path(__file__).with_name("omp_session.schema.json")
    return json.loads(path.read_text())
