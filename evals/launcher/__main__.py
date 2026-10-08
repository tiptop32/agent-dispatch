from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

LAUNCHER = Path(__file__).parents[2] / "scripts" / "codex-agent-lb"


def run_case(root: Path, config: str | None, parent: str | None) -> tuple[int, dict | None]:
    home = root / "home"
    native = home / ".codex/packages/standalone/current/bin/codex"
    native.parent.mkdir(parents=True)
    native.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "json.dump({'key': os.environ.get('AGENT_LB_API_KEY'), "
        "'args': sys.argv[1:]}, open(os.environ['CAPTURE'], 'w'))\n"
        "raise SystemExit(0 if os.environ.get('AGENT_LB_API_KEY') else 78)\n"
    )
    native.chmod(native.stat().st_mode | stat.S_IXUSR)
    if config is not None:
        env_file = home / ".config/agent-lb/env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(config)
    capture = root / "capture"
    env = {"HOME": str(home), "CAPTURE": str(capture), "PATH": os.environ["PATH"]}
    if parent is not None:
        env["AGENT_LB_API_KEY"] = parent
    result = subprocess.run([LAUNCHER, "space arg", "", "$value"], env=env, capture_output=True)
    return result.returncode, json.loads(capture.read_text()) if capture.exists() else None


def main() -> int:
    cases = [
        (
            "file quoted",
            'AGENT_LB_API_KEY="from-file"\n',  # pragma: allowlist secret
            None,
            0,
            "from-file",
        ),
        ("empty then valid", "AGENT_LB_API_KEY=\nAGENT_LB_API_KEY=second\n", None, 0, "second"),
        ("parent wins", "AGENT_LB_API_KEY=file\n", "parent", 0, "parent"),
        ("missing fails", None, None, 78, None),
    ]
    failures = []
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-eval-launcher-") as raw:
        for name, config, parent, expected_code, expected_key in cases:
            code, observed = run_case(Path(raw) / name.replace(" ", "-"), config, parent)
            expected_observed = expected_key is not None
            observed_ok = (
                observed is not None
                and observed["key"] == expected_key
                and observed["args"] == ["space arg", "", "$value"]
            )
            if code != expected_code or observed_ok != expected_observed:
                failures.append((name, code, observed))
        # Negative control: direct native invocation receives no provider key.
        negative = Path(raw) / "negative-control"
        run_case(negative, None, None)
        direct_capture = negative / "direct-capture"
        native = negative / "home/.codex/packages/standalone/current/bin/codex"
        direct_env = {"CAPTURE": str(direct_capture), "PATH": os.environ["PATH"]}
        direct = subprocess.run([native], env=direct_env)
        direct_observed = json.loads(direct_capture.read_text())
        if direct.returncode != 78 or direct_observed["key"] is not None:
            failures.append(
                ("negative control accepted missing key", direct.returncode, direct_observed)
            )
    print(f"launcher eval: {len(cases)} cases, {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"FAIL: {failure}")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
