#!/bin/sh
printf '%s\n' "$$" > "$FAKE_PID_FILE"
( trap '' TERM; sleep 60 ) &
sleep 60
