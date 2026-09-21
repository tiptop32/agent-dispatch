from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, RouteDecision, RouterKind

from .base import RouterError
from .questions import build_questions, build_state, parse_answers


class JevRouter:
    name = "jev"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.settings, self.client, self.sleep = settings, client, sleep

    async def decide(self, req: DispatchRequest, candidates: dict[str, str]) -> RouteDecision:
        cfg = self.settings.router.jev
        api_key = self.settings.secret(cfg.api_key_env)
        if not api_key:
            raise RouterError(f"missing API key: {cfg.api_key_env}")
        body = {
            "model": cfg.model,
            "state": build_state(req),
            "questions": build_questions(candidates),
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        started = time.monotonic()
        attempts = cfg.retries + 1
        for attempt in range(attempts):
            try:
                response = await self.client.post(
                    cfg.base_url, headers=headers, json=body, timeout=cfg.timeout_seconds
                )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "server error", request=response.request, response=response
                    )
                if response.status_code >= 400:
                    raise RouterError(response.text[:300])
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RouterError("invalid JSON from router") from exc
                executor, confidence, scores, judgments = parse_answers(
                    payload.get("answers", {}), list(candidates)
                )
                usage = payload.get("usage") or {}
                return RouteDecision(
                    router=RouterKind.jev,
                    executor=executor,
                    confidence=confidence,
                    scores=scores,
                    judgments=judgments,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cost_usd=usage.get("cost"),
                    meta={"jev_id": payload.get("id"), "model": payload.get("model", cfg.model)},
                )
            except RouterError:
                raise
            except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if attempt >= attempts - 1:
                    raise RouterError(str(exc)) from exc
                await self.sleep(0.5 * (2**attempt))
        raise RouterError("request failed")
