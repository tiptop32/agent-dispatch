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

from .harness import TASKS, prepare_repo, verify


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    executors = args.executors.split(",") if args.executors else list(settings.enabled_executors())
    tasks = args.tasks.split(",")
    state = await ensure_daemon(settings)
    rows = []
    async with DispatchClient(state, timeout=args.timeout + 30) as client:
        for executor in executors:
            for task in tasks:
                with tempfile.TemporaryDirectory(prefix="agent-dispatch-smoke-") as raw:
                    repo = Path(raw) / "repo"
                    prepare_repo(Path(__file__).parent / "repo_template", repo)
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
                    rows.append(
                        {
                            "executor": executor,
                            "task": task,
                            "ok": verdict.ok,
                            "reasons": verdict.reasons,
                        }
                    )
                    print(
                        f"{executor:20} {task:10} {'ok' if verdict.ok else 'FAIL'} "
                        f"{'; '.join(verdict.reasons)}"
                    )
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    report_path = report_dir / f"smoke-{datetime.now():%Y%m%d-%H%M%S}.json"
    report_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))  # noqa: ASYNC240
    return 0 if all(row["ok"] for row in rows) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run live AgentDispatch CLI smoke tests")
    parser.add_argument("--executors", default="")
    parser.add_argument("--tasks", default="fix,docstring,rename")
    parser.add_argument("--report-dir", default="evals/reports")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    invalid = set(args.tasks.split(",")) - set(TASKS)
    if invalid:
        parser.error(f"unknown tasks: {sorted(invalid)}")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
