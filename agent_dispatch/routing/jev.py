from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import DispatchRequest, Judgment, RouteDecision, RouterKind

from .base import RouterError
from .capability import has_corporate, select, tiers_present
from .questions import build_questions, build_state, parse_answers


class JevRouter:
    name = "jev"

    def _corporate_verdict(self, corporate: Judgment | None) -> tuple[bool, str | None]:
        """Сужать ли пул до периметра, и что об этом сказать.

        `noul`-ответ вида «скорее да» с уверенностью 0.06 это монетка, а не
        решение, и отсекать по нему весь пул вслепую нельзя. Порог живёт в
        `routing.corporate_min_confidence` и по умолчанию равен нулю: ослабление
        периметра данных должен включить человек, а не дефолт.
        """
        if corporate is None or not corporate.value:
            return False, None
        threshold = self.settings.routing.corporate_min_confidence
        if corporate.confidence >= threshold:
            return True, None
        return False, (
            f"corporate_data answered yes but only at confidence {corporate.confidence:.2f}, "
            f"below routing.corporate_min_confidence={threshold:.2f}: "
            "the perimeter filter was not applied"
        )

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.settings, self.client, self.sleep = settings, client, sleep

    async def decide(
        self, req: DispatchRequest, candidates: dict[str, ExecutorSettings]
    ) -> RouteDecision:
        cfg = self.settings.router.jev
        api_key = self.settings.secret(cfg.api_key_env)
        if not api_key:
            raise RouterError(f"missing API key: {cfg.api_key_env}")
        capabilities = tiers_present(candidates)
        ask_corporate = has_corporate(candidates)
        body = {
            "model": cfg.model,
            "state": build_state(req),
            "questions": build_questions(capabilities, ask_corporate=ask_corporate),
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
                capability, confidence, tier_scores, judgments = parse_answers(
                    payload.get("answers", {}), capabilities
                )
                judgment = judgments.get("judgment")
                corporate = judgments.get("corporate_data")
                narrow, extra_note = self._corporate_verdict(corporate)
                selection = select(
                    capability,
                    candidates,
                    judgment=bool(judgment and judgment.value),
                    corporate=narrow,
                    corporate_confidence=corporate.confidence if corporate else None,
                    probabilities=tier_scores,
                )
                if extra_note:
                    selection.notes.append(extra_note)
                usage = payload.get("usage") or {}
                meta: dict[str, Any] = {
                    "jev_id": payload.get("id"),
                    "model": payload.get("model", cfg.model),
                    "capability_scores": tier_scores,
                }
                if selection.notes:
                    meta["selection_notes"] = selection.notes
                return RouteDecision(
                    router=RouterKind.jev,
                    executor=selection.executor,
                    capability=capability,
                    confidence=confidence,
                    scores=selection.scores,
                    judgments=judgments,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cost_usd=usage.get("cost"),
                    meta=meta,
                )
            except RouterError:
                raise
            except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if attempt >= attempts - 1:
                    raise RouterError(str(exc)) from exc
                await self.sleep(0.5 * (2**attempt))
        raise RouterError("request failed")
