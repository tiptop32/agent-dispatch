#!/bin/sh
# Fake omp, сохраняющий окружение процесса: тесты проверяют, что до дочернего
# omp не дошли AGENT_LB_API_KEY и секреты Settings.secrets, а у x5-исполнителя
# omp видит системное хранилище сертификатов (NODE_USE_SYSTEM_CA).
[ -n "$FAKE_CAPTURE" ] && env > "$FAKE_CAPTURE.env"
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
echo changed >> a.py
cat "$(dirname "$0")/../fixtures/omp/${OMP_FIXTURE:-ok.ndjson}"
