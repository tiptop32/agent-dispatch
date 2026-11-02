#!/bin/sh
# Fake omp, сохраняющий окружение процесса: тесты проверяют, что до дочернего
# omp не дошли AGENT_LB_API_KEY и секреты Settings.secrets, а у x5-исполнителя
# выставлен NODE_EXTRA_CA_CERTS.
[ -n "$FAKE_CAPTURE" ] && env > "$FAKE_CAPTURE.env"
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/omp/${OMP_FIXTURE:-ok.ndjson}"
