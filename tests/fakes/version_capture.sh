#!/bin/sh
if [ "$1" = "--version" ]; then
  env > "$FAKE_CAPTURE"
  echo 1.2.3
fi
