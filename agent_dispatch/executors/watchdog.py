from __future__ import annotations

import hashlib
import json
import time


class OmpWatchdog:
    """Track model progress and actual tool lifetimes, never stderr chatter."""

    def __init__(self, idle_seconds: float | None, tool_idle_seconds: float, repeat_limit: int):
        self.idle_seconds = idle_seconds
        self.tool_idle_seconds = tool_idle_seconds
        self.repeat_limit = repeat_limit
        self.last_activity_at = time.monotonic()
        self.open_tools: dict[str, tuple[object, object]] = {}
        self.last_digest: bytes | None = None
        self.last_call: str | None = None
        self.repeats = 0

    def on_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind in {
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            self.last_activity_at = time.monotonic()
        call_id = event.get("toolCallId")
        if not isinstance(call_id, str):
            return
        if kind == "tool_execution_start":
            call = (event.get("toolName"), event.get("args"))
            self.open_tools[call_id] = call
            # Другой вызов (инструмент или аргументы) — выход из петли уже начался: серия
            # обнуляется сразу, а не только когда этот вызов закончится.
            if _canonical(call) != self.last_call:
                self.repeats = 0
                self.last_digest = None
        elif kind == "tool_execution_end":
            call = self.open_tools.pop(call_id, None)
            if call is None:
                return
            result = event.get("result")
            if isinstance(result, dict) and isinstance(result.get("details"), dict):
                details = {k: v for k, v in result["details"].items() if k != "wallTimeMs"}
                result = {**result, "details": details}
            payload = json.dumps(
                [*call, result], sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            digest = hashlib.sha256(payload.encode()).digest()
            self.repeats = self.repeats + 1 if digest == self.last_digest else 1
            self.last_digest = digest
            self.last_call = _canonical(call)

    def check(self) -> dict | None:
        tool_open = bool(self.open_tools)
        if self.repeat_limit > 0 and self.repeats >= self.repeat_limit:
            return {"reason": "looping", "repeats": self.repeats, "tool_open": tool_open}
        limit = self.tool_idle_seconds if tool_open else self.idle_seconds
        idle = time.monotonic() - self.last_activity_at
        if limit and limit > 0 and idle >= limit:
            return {"reason": "idle", "idle_s": round(idle, 1), "tool_open": tool_open}
        return None


def _canonical(call: tuple[object, object]) -> str:
    return json.dumps(list(call), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
