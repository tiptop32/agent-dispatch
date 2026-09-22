#!/bin/sh
env > "$FAKE_CAPTURE"
printf '%s\n' '{"result":"I did the work","is_error":false,"modelUsage":{}}'
