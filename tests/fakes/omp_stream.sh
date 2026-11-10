#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "omp 18.16.0"
    exit 0
fi
exec "${FAKE_PYTHON:-python3}" -c '
import json
import os
import sys
import time

mode = os.environ.get("FAKE_STREAM_MODE", "silent")
def emit(event):
    print(json.dumps(event), flush=True)

emit({"type": "session", "id": "stream-session"})
if mode in ("open", "long_tool", "closed"):
    emit({"type": "tool_execution_start", "toolCallId": "one", "toolName": "bash", "args": {"command": "build"}})
    if mode == "long_tool":
        time.sleep(float(os.environ.get("FAKE_TOOL_SECONDS", "0.8")))
    if mode != "open":
        emit({"type": "tool_execution_end", "toolCallId": "one", "toolName": "bash", "result": {"content": []}})
if mode == "fixture":
    with open(os.environ["FAKE_STREAM_FIXTURE"]) as source:
        for line in source:
            print(line, end="", flush=True)
    if os.environ.get("FAKE_STREAM_EXIT") == "1":
        sys.exit(0)
if mode == "long_tool":
    emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "```agent-dispatch-result\n{\"status\":\"completed\",\"summary\":\"build finished\"}\n```"}]}})
    sys.exit(0)
while True:
    print("quiet MCP diagnostic", file=sys.stderr, flush=True)
    emit({"type": "agent_start"})
    time.sleep(0.02)
'
