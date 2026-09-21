#!/bin/sh

input=$(cat)
printf 'IN:%s\n' "$input"
printf 'warn\n' >&2
exit 0
