from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from .harness import run


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic scheduling eval: root coding/review capacity invariants"
    )
    parser.add_argument("--seed", type=int, default=20261006, help="seed for the stress race")
    parser.add_argument("--rounds", type=int, default=12, help="tasks in the stress race")
    parser.add_argument("--report-dir", default="evals/reports")
    args = parser.parse_args()

    verdicts = asyncio.run(run(args.seed, args.rounds))
    for verdict in verdicts:
        print(
            f"{verdict.scenario:40} {'ok' if verdict.ok else 'FAIL'} {'; '.join(verdict.reasons)}"
        )
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    report_path = report_dir / f"scheduling-{datetime.now():%Y%m%d-%H%M%S}.json"
    report_path.write_text(  # noqa: ASYNC240
        json.dumps(
            [{"scenario": v.scenario, "ok": v.ok, "reasons": v.reasons} for v in verdicts],
            ensure_ascii=False,
            indent=2,
        )
    )
    failed = [v.scenario for v in verdicts if not v.ok]
    print(f"report: {report_path}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
