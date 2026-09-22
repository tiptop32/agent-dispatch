#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  [ "$prev" = "-o" ] && out="$arg"
  prev="$arg"
done
[ -n "$FAKE_CAPTURE" ] && { printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"; cat > "$FAKE_CAPTURE.stdin"; }
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/agent_output/codex_last_message.json" > "$out"
cat "$(dirname "$0")/../fixtures/agent_output/codex_exec_json.txt"
