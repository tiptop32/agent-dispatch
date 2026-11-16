from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from agent_dispatch.config import load_settings
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DispatchClient
from agent_dispatch.models import DispatchRequest

from .harness import TASKS, decisions_counts, decisions_threshold_ok, make_row, prepare_repo, verify


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    executors = args.executors.split(",") if args.executors else list(settings.enabled_executors())
    tasks = args.tasks.split(",")
    state = await ensure_daemon(settings)
    rows = []
    async with DispatchClient(state, timeout=args.timeout + 30) as client:
        for executor in executors:
            for task in tasks:
                runs = getattr(args, "runs", 1) if getattr(args, "decisions", False) else 1
                for _ in range(runs):
                    with tempfile.TemporaryDirectory(prefix="agent-dispatch-smoke-") as raw:
                        repo = Path(raw) / "repo"
                        prepare_repo(
                            Path(__file__).parent / "repo_template",
                            repo,
                            inject_bug=task == "fix",
                        )
                        view = await client.submit(
                            DispatchRequest(
                                task=TASKS[task],
                                cwd=str(repo),
                                executor=executor,
                                source_agent="cli",
                                wait_seconds=args.timeout,
                                allow_escalation=False,
                            )
                        )
                        verdict = verify(repo, view, task)
                        row = make_row(task, executor, verdict, view)
                        rows.append(row)
                        print(
                            f"{executor:20} {task:10} {'ok' if verdict.ok else 'FAIL'} "
                            f"{'; '.join(verdict.reasons)}"
                        )

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    report_path = report_dir / f"smoke-{datetime.now():%Y%m%d-%H%M%S}.json"
    report_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))  # noqa: ASYNC240
    if getattr(args, "decisions", False):
        # Порог считается по готовым строкам отчёта, как в офлайн-тесте.
        runs = getattr(args, "runs", 10)
        counts = decisions_counts(rows)
        ok = decisions_threshold_ok(counts, runs=runs)
        for executor, per_executor in sorted(counts.items()):
            print(
                f"{executor:20} positive={per_executor['positive']}/{runs} "
                f"noise={per_executor['noise']}/{runs} "
                f"fork_runs={per_executor['fork_runs']} none_runs={per_executor['none_runs']} "
                f"failed_runs={per_executor['failed_runs']}"
            )
        print(f"decisions threshold: {'pass' if ok else 'fail'}")
        return 0 if ok else 1
    return 0 if all(row["ok"] for row in rows) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run live AgentDispatch CLI smoke tests")
    parser.add_argument("--executors", default="")
    parser.add_argument("--tasks", default="fix,docstring,rename")
    parser.add_argument("--report-dir", default="evals/reports")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--decisions",
        action="store_true",
        help=(
            "режим eval'а записей решений: N прогонов decisions_fork и decisions_none "
            "на каждого исполнителя (по умолчанию 10), порог 8/2 в итоговом выводе"
        ),
    )
    parser.add_argument(
        "--runs", type=int, default=10, help="прогонов каждой задачи на исполнителя в --decisions"
    )
    args = parser.parse_args()
    invalid = set(args.tasks.split(",")) - set(TASKS)
    if invalid:
        parser.error(f"unknown tasks: {sorted(invalid)}")
    if args.decisions:
        args.tasks = "decisions_fork,decisions_none"
        args.executors = args.executors or "omp/sol,omp/x5-code-large"
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
