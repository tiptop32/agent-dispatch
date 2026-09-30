from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_dispatch.config import load_settings

from .harness import load, render, replay


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Replay dispatcher rules over live telemetry.")
    parser.add_argument("--db", type=Path, default=settings.server.data_dir / "dispatch.db")
    parser.add_argument("--cooldown", type=int, default=settings.routing.failure_cooldown_seconds)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args()
    report = replay(load(args.db), args.cooldown)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
