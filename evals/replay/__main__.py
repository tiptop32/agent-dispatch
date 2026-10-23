from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from agent_dispatch.config import load_settings

from .harness import (
    GitLocator,
    acceptance,
    children_refusals,
    default_limit_group,
    delegations,
    exploration_comparison,
    load,
    load_decisions,
    perimeter_violations,
    render,
    replay,
    router_vs_static,
    timeouts,
)


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Replay dispatcher rules over live telemetry.")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--cooldown", type=int, default=settings.routing.failure_cooldown_seconds)
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO date/time: consider only tasks created at or after this moment",
    )
    parser.add_argument(
        "--accept-window-hours",
        type=float,
        default=48,
        help="a diff counts as accepted if the caller committed its files within this window",
    )
    parser.add_argument(
        "--no-git", action="store_true", help="skip the acceptance check (it runs git log)"
    )
    parser.add_argument(
        "--baseline-tier",
        choices=("fast", "balanced", "strong"),
        default="balanced",
        help="tier of the static rule Jev is compared with",
    )
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args()
    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            parser.error(f"--since: not an ISO date/time: {args.since}")
        if since.tzinfo is None:
            since = since.astimezone()
    executors = settings.executors

    def limit_group(name: str) -> str:
        item = executors.get(name)
        return item.resolved_limit_group(name) if item else default_limit_group(name)

    tasks = load(args.db)
    if since is not None:
        tasks = [task for task in tasks if task.created_at >= since]
    report = replay(
        tasks,
        args.cooldown,
        quota_cooldown=settings.routing.quota_cooldown_seconds,
        limit_group=limit_group,
        review_only=settings.routing.review_only,
        recheck=lambda name: name in executors and executors[name].limit_reset == "recheck",
    )
    report["perimeter_violations"] = perimeter_violations(
        tasks, lambda name: name in executors and executors[name].corporate
    )
    report["children_refusals"] = children_refusals(tasks, settings.routing.max_children)
    report["delegations"] = delegations(tasks)
    report["timeouts"] = timeouts(tasks)
    # Один locator на прогон: дифы эксперимента судятся второй раз из кэша.
    locator = GitLocator()
    if not args.no_git:
        report["acceptance"] = acceptance(tasks, locator, window_hours=args.accept_window_hours)
    decisions = load_decisions(args.db)
    report["router_vs_static"] = router_vs_static(
        decisions,
        tasks,
        executors,
        baseline_tier=args.baseline_tier,
        corporate_min_confidence=settings.routing.corporate_min_confidence,
        corporate_perimeter=settings.routing.corporate_perimeter,
    )
    report["exploration"] = exploration_comparison(
        decisions,
        tasks,
        locator,
        window_hours=args.accept_window_hours,
        with_acceptance=not args.no_git,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
