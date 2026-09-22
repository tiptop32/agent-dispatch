#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/agent_output/opencode_run_json.txt"
