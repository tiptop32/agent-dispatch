from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .harness import load_cases, render, replay


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay OpenCode headless adapter corpus")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(__file__).with_name("corpus.jsonl"),
    )
    args = parser.parse_args()
    cases = load_cases(args.corpus)
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-evals-executors-") as raw:
        report = replay(cases, Path(raw))
    print(render(report))
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
