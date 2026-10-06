# OpenCode executor eval

This deterministic, offline corpus checks the OpenCode adapter's argv and event parsing without running the real CLI.

Task runs add `--auto` only when `extra_args` does not configure auto approval. Explicit `--no-auto`, `--auto=false`, `--auto false`, and enabled forms remain unchanged. Read-only reviews force `--agent plan`, remove enabled auto forms, and preserve explicit opt-outs.

The outcome cases keep arbitrary text `partial`, surface permission rejections, collect usage, and preserve CLI error messages.

Run the strict all-cases gate from any directory:

```bash
uv run python -m evals.executors
```

Use `--corpus <path>` to replay another corpus. The default corpus is resolved next to the module, not from the caller's working directory.
