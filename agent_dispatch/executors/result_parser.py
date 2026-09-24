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
        return ExecutionResult(
            status="failed",
            executor=executor,
            model=model,
            summary=_tail(outcome.stdout),
            changed_files=changed_files or [],
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
    tests = raw.get("tests")
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
