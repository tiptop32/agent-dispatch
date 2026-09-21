#!/bin/sh
printf '%s\n' "$$" > "$FAKE_PID_FILE"
sleep 60
