# omp executor eval

This deterministic, offline corpus checks the omp adapter's argv and event parsing without invoking the CLI. The corpus includes the configured agent-lb GPT models (`gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-5.6-luna`) and the corporate x5 model, so a hardcoded or missing `--model` is caught.

Task runs get `--approval-mode yolo` unless `extra_args` overrides it. Corporate runs add `--profile x5` and `--model`. Read-only reviews replace the approval flags with `--tools read,grep,glob`. Transport-before-output cases assert the marker used by the bounded dispatcher retry.

The outcome cases keep arbitrary text `partial` without a result block, surface key and connection errors, collect usage across turns, preserve CLI error messages, and handle the `--max-cost` stop (exit 3) with the budget metadata.

Run the strict all-cases gate from any directory:

```bash
uv run python -m evals.executors
```

Use `--corpus <path>` to replay another corpus. The default corpus is resolved next to the module, not from the caller's working directory.
