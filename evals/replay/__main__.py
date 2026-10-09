from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from agent_dispatch.config import load_settings

from .harness import default_limit_group, load, render, replay


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Replay dispatcher rules over live telemetry.")
    parser.add_argument("--db", type=Path, default=settings.server.data_dir / "dispatch.db")
    parser.add_argument("--cooldown", type=int, default=settings.routing.failure_cooldown_seconds)
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO date/time: consider only tasks created at or after this moment",
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

    report = replay(
        load(args.db),
        args.cooldown,
        quota_cooldown=settings.routing.quota_cooldown_seconds,
        limit_group=limit_group,
        review_only=settings.routing.review_only,
        recheck=lambda name: name in executors and executors[name].limit_reset == "recheck",
        since=since,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
