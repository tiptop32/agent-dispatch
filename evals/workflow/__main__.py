from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from evals.smoke.harness import prepare_repo

from .harness import evaluate, load_cases, render


def run(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.cases))
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-workflow-") as directory:
        repo = prepare_repo(
            Path(__file__).parents[1] / "smoke" / "repo_template",
            Path(directory) / "repo",
        )
        (repo / "README.md").write_text("# workflow contract fixture\n\nchanged\n")
        verdicts = evaluate(cases, repo)
    print(render(verdicts))
    return 0 if all(verdict.ok for verdict in verdicts) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check contracts in prompts emitted from real TaskPackage inputs"
    )
    parser.add_argument("--cases", default="evals/workflow/cases.jsonl")
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
