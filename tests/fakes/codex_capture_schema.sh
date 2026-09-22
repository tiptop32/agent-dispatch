#!/bin/sh
out=""; schema=""; prev=""
for arg in "$@"; do
  [ "$prev" = "-o" ] && out="$arg"
  [ "$prev" = "--output-schema" ] && schema="$arg"
  prev="$arg"
done
[ -n "$FAKE_CAPTURE" ] && cp "$schema" "$FAKE_CAPTURE.schema"
printf '%s\n' '{"status":"completed","summary":"ok"}' > "$out"
