#!/usr/bin/env python3
"""Real subprocess fixture for supervision lifecycle tests; never calls a model."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

control = Path(os.environ["AD_CONTROL"])
if "--worker" in sys.argv:
    (control / "worker.pid").write_text(str(os.getpid()))
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); "
            "exec('while not p.exists(): time.sleep(0.02)'); "
            "pathlib.Path(sys.argv[2]).write_text('escaped')",
            str(control / "release-children"),
            str(control / "worker-child-late"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path("a.py").write_text("x = 'wrong scope'\n")
    print(os.environ.get("AD_LOG_TEXT", ""), flush=True)
    print("tool: edited a.py; repeated failure E_SCOPE; no validation was run", flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "provider": "agent-lb",
                    "model": "gpt-6.1-sol",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "working"}],
                    "usage": {"input": 777, "output": 11, "cost": {"total": 0.003}},
                },
            }
        ),
        flush=True,
    )
    time.sleep(float(os.environ.get("AD_WORKER_DELAY", "10")))
    (control / "worker-late").write_text("worker kept writing")
    sys.exit(0)

payload = json.load(sys.stdin)
count_path = control / "checks"
count = int(count_path.read_text()) + 1 if count_path.exists() else 1
count_path.write_text(str(count))
(control / f"payload-{count}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "payload": payload})
)
overlay = Path(sys.argv[sys.argv.index("--config") + 1]).read_text()
(control / f"overlay-{count}.txt").write_text(overlay)
mode = os.environ.get("AD_SUPERVISOR_CASE", "continue-stop")


def respond(text):
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": text}],
                    "provider": "agent-lb",
                    "model": "gpt-6.1-terra" if mode == "wrong-model" else "gpt-6.1-sol",
                    "usage": {"input": 100, "output": 20, "cost": {"total": 0.001}},
                },
            }
        )
    )


if mode == "check-waits":
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); "
            "exec('while not p.exists(): time.sleep(0.02)'); "
            "pathlib.Path(sys.argv[2]).write_text('escaped')",
            str(control / "release-children"),
            str(control / "supervisor-child-late"),
        ]
    )
    (control / "supervisor.pid").write_text(str(os.getpid()))
    time.sleep(10)
if mode == "malformed":
    respond('{"decision":"stop","reason":"guess","evidence":[]}')
elif mode == "exit-error":
    print("429 Too Many Requests; supervisor account cooling down", file=sys.stderr)
    sys.exit(9)
elif (
    mode == "continue"
    or (mode == "continue-stop" and count == 1)
    or (mode == "parent-stop" and payload["immutable_mandate"]["task"].startswith("Child"))
):
    respond(
        json.dumps(
            {"decision": "continue", "reason": "One failure is not stagnation", "evidence": []}
        )
    )
else:
    respond(
        json.dumps(
            {
                "decision": "stop",
                "reason": "Worker keeps editing outside the immutable mandate",
                "evidence": ["log reports edit to a.py despite explicit no-edit mandate"],
            }
        )
    )
