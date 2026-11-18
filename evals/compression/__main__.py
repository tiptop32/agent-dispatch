"""Default: free, offline replay. Paid lanes require an explicit CLI selection."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agent_dispatch.mcp.client import DaemonUnavailable

from .harness import (
    TRUSTED_ROOT,
    live_settings,
    load_corpus,
    report_document,
    report_target,
    run_offline,
    run_paired,
    run_semantic,
    write_report,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true", help="Free replay of all 70 frozen cases")
    modes.add_argument(
        "--live",
        "--live-semantic",
        dest="live",
        action="store_true",
        help="Paid synthetic Jev only",
    )
    modes.add_argument(
        "--paired", action="store_true", help="Paid 20 cases x 3 repetitions x 2 arms"
    )
    result.add_argument(
        "--split",
        choices=("calibration", "holdout", "all"),
        # Preserve whether the flag was supplied, including explicit --split all.
        # run() resolves an omitted selection to all; main() rejects it on other lanes.
        default=None,
        help="Live semantic selection only (default: all); subsets do not pass the full gate",
    )
    result.add_argument(
        "--executor", help="Explicit configured omp executor, required for --paired"
    )
    result.add_argument(
        "--timeout", type=int, default=900, help="Same executor timeout for both arms"
    )
    result.add_argument(
        "--report",
        type=Path,
        default=None,
        help="JSON report path under /tmp (never in repository)",
    )
    return result


async def run(args: argparse.Namespace) -> int:
    lane = "paired" if args.paired else "live-semantic" if args.live else "offline"
    split = args.split or "all"
    report_path = args.report or Path(
        f"/tmp/compression-{lane}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.json"
    )
    rows = []
    failure = None

    def checkpoint() -> None:
        write_report(report_path, report_document(lane, rows, split=split))

    try:
        # Reject an unsafe report destination before any possible paid call.
        report_path = await asyncio.to_thread(report_target, report_path)
        if args.split is not None and lane != "live-semantic":
            raise ValueError("--split is only valid with --live-semantic")
        cases = load_corpus()
        if lane == "offline":
            with tempfile.TemporaryDirectory(
                prefix="compression-offline-", dir="/tmp"
            ) as directory:
                rows = await run_offline(cases, Path(directory))
        else:
            settings = live_settings()
            if lane == "live-semantic":
                await run_semantic(cases, settings, split=split, rows=rows, checkpoint=checkpoint)
            else:
                # Preserve isolated repositories and report artifacts for independent review.
                directory = tempfile.mkdtemp(prefix="paired-", dir=TRUSTED_ROOT)
                await run_paired(
                    cases,
                    settings,
                    args.executor,
                    args.timeout,
                    Path(directory),
                    rows=rows,
                    checkpoint=checkpoint,
                )
    except (DaemonUnavailable, ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    report = report_document(lane, rows, split=split if lane == "live-semantic" else "all")
    if failure is not None:
        report["ok"] = False
        report["full_gate_passed"] = False
        report["error"] = failure
    try:
        write_report(report_path, report)
    except (ValueError, OSError) as exc:
        print(f"Report failed: {exc}")
        return 1
    for row in rows:
        if not row["ok"]:
            print(f"FAIL {row['id']}: {'; '.join(row['reasons'])}")
    print(f"{lane}: {sum(row['ok'] for row in rows)}/{len(rows)} passed; report={report_path}")
    if failure is not None:
        print(failure)
    return 0 if report["ok"] else 1


def main() -> None:
    cli = parser()
    args = cli.parse_args()
    if args.paired and not args.executor:
        cli.error("--paired requires an explicit --executor")
    if args.executor and not args.paired:
        cli.error("--executor is only valid with --paired")
    if args.split is not None and not args.live:
        cli.error("--split is only valid with --live-semantic")
    if args.timeout <= 0:
        cli.error("--timeout must be positive")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
