#!/bin/sh
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
printf '%s\n' '{"type":"error","error":{"name":"APIError","data":{"message":"No cookie auth credentials found"}}}'
exit 1
