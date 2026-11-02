# omp executor eval

This deterministic, offline corpus checks the omp adapter's argv and event parsing without running the real CLI.

Task runs get `--approval-mode yolo` unless `extra_args` overrides it. Corporate runs add `--profile x5` and `--model`. Read-only reviews replace the approval flags with `--tools read,grep,glob`.

The outcome cases keep arbitrary text `partial` without a result block, surface key and connection errors, collect usage across turns, preserve CLI error messages, and handle the `--max-cost` stop (exit 3) with the budget metadata.

Run the strict all-cases gate from any directory:

```bash
uv run python -m evals.executors
```

Use `--corpus <path>` to replay another corpus. The default corpus is resolved next to the module, not from the caller's working directory.
