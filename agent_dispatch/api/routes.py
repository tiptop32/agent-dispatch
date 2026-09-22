from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from agent_dispatch import __version__
from agent_dispatch.models import FINAL_STATUSES, DispatchRequest, TaskRecord, TaskView


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal[
        "success", "failure", "manual_override", "escalated", "user_accepted", "user_reworked"
    ]
    note: str | None = None


MAX_WAIT_SECONDS = 600


def to_view(record: TaskRecord) -> TaskView:
    path = Path(record.log_path)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 4096))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        tail = ""
    return TaskView(**record.model_dump(), log_tail=tail)


def _deps(request: Request):
    return request.app.state.deps


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request):
        deps = _deps(request)
        return {
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": max(0.0, time.monotonic() - deps.started_at),
            "router_backend": deps.settings.router.backend,
        }

    @router.get("/executors")
    async def executors(request: Request):
        deps = _deps(request)
        result = []
        for name, config in deps.settings.executors.items():
            availability = deps.availability.get(name)
            result.append(
                {
                    "name": name,
                    "adapter": config.adapter,
                    "model": config.model,
                    "enabled": config.enabled,
                    "available": availability.available if availability else None,
                    "version": availability.version if availability else None,
                    "checked_at": availability.checked_at if availability else None,
                    "error": availability.error if availability else None,
                }
            )
        return result

    @router.post("/route")
    async def route(req: DispatchRequest, request: Request):
        decision, _ = await _deps(request).dispatcher.route_only(req)
        return decision

    @router.post("/tasks")
    async def submit(req: DispatchRequest, request: Request, response: Response):
        deps = _deps(request)
        record = await deps.dispatcher.submit(req)
        record = await deps.dispatcher.wait(record.task_id, min(req.wait_seconds, MAX_WAIT_SECONDS))
        response.status_code = 200 if record.status in FINAL_STATUSES else 202
        return to_view(record)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        record = await _deps(request).dispatcher.get(task_id)
        if record is None:
            raise HTTPException(404, "task not found")
        return to_view(record)

    @router.delete("/tasks/{task_id}")
    async def cancel(task_id: str, request: Request):
        deps = _deps(request)
        try:
            record = await deps.dispatcher.cancel(task_id)
        except KeyError:
            raise HTTPException(404, "task not found") from None
        except ValueError:
            raise HTTPException(409, "task already finished") from None
        return to_view(record)

    @router.post("/tasks/{task_id}/feedback")
    async def feedback(task_id: str, body: FeedbackRequest, request: Request):
        deps = _deps(request)
        if not await deps.storage.task_exists(task_id):
            raise HTTPException(404, "task not found")
        event_id = await deps.storage.add_event(task_id, "feedback", body.model_dump())
        return {"ok": True, "event_id": event_id}

    @router.get("/export")
    async def export(request: Request, since: str | None = None):
        parsed = _parse_since(since)

        async def body():
            async for item in _deps(request).storage.export(parsed):
                yield json.dumps(item, ensure_ascii=False) + "\n"

        return StreamingResponse(body(), media_type="text/plain")

    return router


_SINCE_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    match = _SINCE_RE.fullmatch(value)
    if not match:
        raise HTTPException(422, "invalid since")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return datetime.now(UTC) - timedelta(seconds=seconds)
