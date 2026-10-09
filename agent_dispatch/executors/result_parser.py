from __future__ import annotations

import json
import re

from agent_dispatch.executors.process import ProcessOutcome
from agent_dispatch.models import ExecutionResult, TestsInfo
from agent_dispatch.schemas import validate_agent_result

RESULT_TAG = "agent-dispatch-result"
_BLOCK = re.compile(rf"^```{re.escape(RESULT_TAG)}\s*$([\s\S]*?)^```\s*$", re.MULTILINE)


def extract_result_block(text: str) -> dict | None:
    matches = list(_BLOCK.finditer(text))
    if not matches:
        return None
    try:
        value = json.loads(matches[-1].group(1).strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _tail(value: str) -> str:
    return value[-2000:]


def normalize(
    raw: dict | None,
    outcome: ProcessOutcome,
    changed_files: list[str] | None,
    executor: str,
    model: str | None,
) -> ExecutionResult:
    meta = {"exit_code": outcome.exit_code, "duration_ms": outcome.duration_ms}
    if getattr(outcome, "stalled", False):
        seconds = getattr(outcome, "idle_seconds", None) or 0
        meta["idle_timeout_seconds"] = seconds
        return ExecutionResult(
            status="failed",
            executor=executor,
            model=model,
            summary=_tail(outcome.stdout),
            changed_files=changed_files or [],
            error=f"stalled: no output for {int(seconds)}s",
            meta=meta,
        )
    if outcome.timed_out:
        if changed_files:
            meta["timed_out"] = True
            return ExecutionResult(
                status="partial",
                executor=executor,
                model=model,
                summary=_tail(outcome.stdout),
                changed_files=changed_files,
                error="timeout",
                meta=meta,
            )
        return ExecutionResult(
            status="failed",
            executor=executor,
            model=model,
            summary=_tail(outcome.stdout),
            changed_files=[],
            error="timeout",
            meta=meta,
        )
    if outcome.exit_code not in (0, None):
        return ExecutionResult(
            status="failed",
            executor=executor,
            model=model,
            summary=_tail(outcome.stdout),
            changed_files=changed_files or [],
            error=_tail(outcome.stderr),
            meta=meta,
        )
    if raw is None:
        meta["parse_error"] = "no result block"
        return ExecutionResult(
            status="partial",
            executor=executor,
            model=model,
            summary=_tail(outcome.stdout),
            changed_files=changed_files or [],
            meta=meta,
        )
    raw, coerced = _coerce(raw)
    if coerced:
        meta["coerced"] = coerced
    errors = validate_agent_result(raw)
    if errors:
        meta["parse_error"] = "; ".join(errors)
        return ExecutionResult(
            status="partial",
            executor=executor,
            model=model,
            summary=raw.get("summary") or _tail(outcome.stdout),
            changed_files=changed_files or [],
            meta=meta,
        )
    tests = raw.get("tests")
    return ExecutionResult(
        status=raw["status"],
        executor=executor,
        model=model,
        summary=raw["summary"],
        changed_files=changed_files if changed_files is not None else raw.get("changed_files", []),
        tests=TestsInfo(command=tests.get("command"), result=tests.get("result"))
        if tests
        else None,
        confidence=raw.get("confidence"),
        needs_escalation=raw.get("needs_escalation", False),
        meta=meta,
    )


def _coerce(raw: dict) -> tuple[dict, list[str]]:
    """Мягко привести поля, которые модели пишут на естественном языке.

    Синонимы `status` и `tests.result` вроде «1 passed» превращаются в enum;
    список приведений возвращается для `meta.coerced`, чтобы телеметрия видела,
    что агент не соблюдал формат.
    """
    coerced: list[str] = []
    status = raw.get("status")
    if isinstance(status, str):
        status_aliases = {
            "success": "completed",
            "succeeded": "completed",
            "successful": "completed",
            "done": "completed",
            "complete": "completed",
            "ok": "completed",
            "error": "failed",
            "failure": "failed",
            "fail": "failed",
        }
        mapped_status = status_aliases.get(status.strip().lower())
        if mapped_status is not None:
            raw = {**raw, "status": mapped_status}
            coerced.append(f"status: {status!r} -> {mapped_status}")
    # Strict-схема codex требует `null` в необязательных полях, а обычная схема
    # его не принимает. `null` значит «нет значения»: поле убирается, а не
    # превращает готовую работу в `partial`.
    for key in _OPTIONAL_FIELDS:
        if key in raw and raw[key] is None:
            raw = {name: value for name, value in raw.items() if name != key}
            coerced.append(f"{key}: None -> dropped")
    tests = raw.get("tests")
    if isinstance(tests, dict) and None in (tests.get("command", ""), tests.get("result", "")):
        dropped = [key for key in ("command", "result") if key in tests and tests[key] is None]
        tests = {name: value for name, value in tests.items() if name not in dropped}
        raw = {**raw, "tests": tests}
        coerced.extend(f"tests.{key}: None -> dropped" for key in dropped)
    confidence = raw.get("confidence")
    if confidence is not None and not _is_unit_number(confidence):
        mapped_confidence = _coerce_confidence(confidence)
        if mapped_confidence is None:
            raw = {name: value for name, value in raw.items() if name != "confidence"}
        else:
            raw = {**raw, "confidence": mapped_confidence}
        shown = "dropped" if mapped_confidence is None else mapped_confidence
        coerced.append(f"confidence: {confidence!r} -> {shown}")
    if isinstance(tests, dict) and isinstance(tests.get("result"), str):
        value = tests["result"].strip().lower()
        if value not in ("passed", "failed", "not_run"):
            mapped = "not_run"
            if "fail" in value or "error" in value:
                mapped = "failed"
            elif "pass" in value or value in ("ok", "green", "success"):
                mapped = "passed"
            raw = {**raw, "tests": {**tests, "result": mapped}}
            coerced.append(f"tests.result: {tests['result']!r} -> {mapped}")
    return raw, coerced


_OPTIONAL_FIELDS = ("changed_files", "tests", "confidence", "needs_escalation")

#: Уверенность словами: модели пишут «high» вместо числа.
_CONFIDENCE_WORDS = {"high": 0.9, "medium": 0.6, "moderate": 0.6, "low": 0.3}


def _is_unit_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


def _coerce_confidence(value: object) -> float | None:
    """Уверенность в [0, 1] из слова, строки с числом или процентов; иначе None.

    Уверенность в отчёте только советует. Негодное значение отбрасывается, а
    отчёт остаётся в силе: терять из-за неё весь результат дороже.
    """
    if isinstance(value, str):
        text = value.strip().lower().rstrip("%").strip()
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if 1 < value <= 100:
        value = value / 100
    return float(value) if 0 <= value <= 1 else None
