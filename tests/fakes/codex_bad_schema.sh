#!/bin/sh
out=""; prev=""
for arg in "$@"; do [ "$prev" = "-o" ] && out="$arg"; prev="$arg"; done
printf '%s\n' '{"status":"completed","summary":"x","extra":1}' > "$out"
