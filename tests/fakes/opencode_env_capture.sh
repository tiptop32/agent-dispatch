#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && env > "$FAKE_CAPTURE.env"
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/agent_output/opencode_run_json.txt"
