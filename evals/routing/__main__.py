from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx

from agent_dispatch.config import load_settings
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DispatchClient
from agent_dispatch.models import SourceAgent
from agent_dispatch.routing.decision import build_routers, decide_with_fallback
from agent_dispatch.routing.guards import candidates

from .harness import build_request, load_cases, render, score


async def run(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.cases))[: args.limit]
    settings = load_settings()
    results = []
    if args.router == "daemon":
        state = await ensure_daemon(settings)
        async with DispatchClient(state) as client:
            results = await _run_daemon(cases, args, client)
    else:
        async with httpx.AsyncClient() as http_client:
            routers = select_routers(settings, args.router, http_client)
            results = await _run_in_process(cases, args, settings, routers)
    summary = score(results)
    print(render(summary))
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    report = {"router_mode": args.router, "results": results, "summary": summary.__dict__}
    report_path = report_dir / f"routing-{datetime.now():%Y%m%d-%H%M%S}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))  # noqa: ASYNC240
    return 0 if summary.accuracy >= args.threshold else 1


def select_routers(settings, router_mode: str, client: httpx.AsyncClient | None = None):
    if router_mode == "daemon":
        return []
    if client is None:
        client = httpx.AsyncClient()
    configured = settings.model_copy(deep=True)
    configured.router.backend = router_mode
    return [router for router in build_routers(configured, client) if router.name == router_mode]


async def _run_daemon(cases, args, client):
    rows = []
    for case in cases:
        request = build_request(case, args.source_agent)
        started = time.perf_counter()
        decision = await client.route(request)
        elapsed = (time.perf_counter() - started) * 1000
        rows.append(_result_row(case, decision, elapsed))
    return rows


async def _run_in_process(cases, args, settings, routers):
    rows = []
    source = SourceAgent(args.source_agent)
    candidate_names = candidates(settings, set(), source, 0)
    if not candidate_names:
        raise SystemExit(
            f"no candidate executors for source_agent={args.source_agent}: "
            "enable executors in config.yaml or pick another --source-agent"
        )
    candidate_map = {name: settings.executors[name].description for name in candidate_names}
    for case in cases:
        request = build_request(case, args.source_agent)
        started = time.perf_counter()
        decision, _events = await decide_with_fallback(request, candidate_map, settings, routers)
        elapsed = (time.perf_counter() - started) * 1000
        rows.append(_result_row(case, decision, elapsed))
    return rows


def _result_row(case, decision, elapsed):
    return {
        "id": case["id"],
        "expected": case["expected_executor"],
        "got": decision.executor,
        "confidence": decision.confidence,
        "router": decision.router,
        "reason": decision.reason,
        "latency_ms": decision.latency_ms or elapsed,
        "cost_usd": decision.cost_usd,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure AgentDispatch routing accuracy")
    parser.add_argument("--cases", default="evals/routing/cases.jsonl")
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--report-dir", default="evals/reports")
    parser.add_argument("--source-agent", default="cli")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--router", choices=("daemon", "jev", "claude_local"), default="daemon")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
