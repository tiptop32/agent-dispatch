#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
printf '%s\n' '{"type":"text","part":{"type":"text","text":"done"}}'
