#!/bin/sh
env > "$FAKE_CAPTURE"
printf '%s\n' '{"result":"{\"executor\":\"codex\",\"scores\":{\"codex\":0.8}}","modelUsage":{}}'
