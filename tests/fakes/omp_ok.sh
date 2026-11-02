#!/bin/sh
# Fake omp для тестов адаптера: пишет argv (как у других фейков адаптеров) и
# пустой stdin как доказательство DEVNULL, затем проигрывает фикстуру из
# tests/fixtures/omp/, выбранную OMP_FIXTURE. Код выхода — как у живого omp:
# сбой хода 1, --max-cost 3.
[ -n "$FAKE_CAPTURE" ] && printf '%s\n' "$@" > "$FAKE_CAPTURE.argv"
[ -n "$FAKE_CAPTURE" ] && cat > "$FAKE_CAPTURE.stdin"
fixture="${OMP_FIXTURE:-ok.ndjson}"
case "$fixture" in
  empty) ;;
  truncated)
    printf '%s\n' '{"type":"session","version":3,"id":"01fixed-truncated-session-id"}'
    printf '%s' '{"type":"message_end","mess'
    ;;
  badkey.ndjson|down.ndjson)
    cat "$(dirname "$0")/../fixtures/omp/$fixture"
    exit 1
    ;;
  maxcost.ndjson)
    cat "$(dirname "$0")/../fixtures/omp/$fixture"
    exit 3
    ;;
  *)
    echo changed >> a.py
    cat "$(dirname "$0")/../fixtures/omp/$fixture"
    ;;
esac
