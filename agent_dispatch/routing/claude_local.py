from __future__ import annotations

import json
import re

from agent_dispatch.config import Settings
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.process import run_cli
from agent_dispatch.models import DispatchRequest, RouteDecision, RouterKind

from .base import RouterError


class ClaudeLocalRouter:
    name = "claude_local"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def decide(self, req: DispatchRequest, candidates: dict[str, str]) -> RouteDecision:
        cfg = self.settings.router.claude_local
        prompt = "Choose the best executor for this coding task.\n" + "\n".join(
            f"- {k}: {v}" for k, v in candidates.items()
        )
        prompt += f"\nTask: {req.task}\nRespond with JSON only: "
        prompt += '{"executor": "<name>", "scores": {"<name>": <0..1>}}'
        argv = [cfg.command, "-p", "--output-format", "json"]
        if cfg.model:
            argv += ["--model", cfg.model]
        log_path = self.settings.server.data_dir / "logs" / "router-claude-local.log"
        env = child_env(self.settings)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            outcome = await run_cli(
                argv, cwd=req.cwd, env=env, stdin=prompt, timeout_seconds=60, log_path=log_path
            )
        except OSError as exc:
            raise RouterError(f"cannot start {cfg.command}: {exc}") from exc
        if outcome.exit_code != 0:
            raise RouterError(f"claude exited with {outcome.exit_code}: {outcome.stderr[:300]}")
        try:
            wrapper = json.loads(outcome.stdout)
            text = wrapper["result"]
            matches = list(re.finditer(r"\{.*\}", text, re.DOTALL))
            data = json.loads(matches[-1].group(0)) if matches else json.loads(text)
            executor = data["executor"]
            scores = {str(k): float(v) for k, v in data["scores"].items()}
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RouterError(f"invalid claude response: {exc}") from exc
        if executor not in candidates:
            raise RouterError(f"unknown executor: {executor}")
        return RouteDecision(
            router=RouterKind.claude_local,
            executor=executor,
            confidence=scores.get(executor, 0.0),
            scores=scores,
            latency_ms=outcome.duration_ms,
        )
