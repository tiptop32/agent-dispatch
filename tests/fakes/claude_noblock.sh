#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && { printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"; cat > "$FAKE_CAPTURE.stdin"; }
printf '%s\n' '{"result":"I did the work","is_error":false,"modelUsage":{}}'
