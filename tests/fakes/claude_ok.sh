#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && { printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"; cat > "$FAKE_CAPTURE.stdin"; }
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/agent_output/claude_print_json.txt"
