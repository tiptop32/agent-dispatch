#!/bin/sh
env > "$FAKE_CAPTURE"
printf '%s\n' '{"result":"{\"executor\":\"omp\",\"scores\":{\"omp\":0.8}}","modelUsage":{}}'
