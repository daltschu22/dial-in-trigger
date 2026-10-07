#!/bin/sh
set -eu
printf '%s call=%s host=%s\n' "$(date -u +%FT%TZ)" "$TRIGGER_CALL_SID" "$(hostname)" >> "$DIAL_IN_STATE_DIR/demo-runs.log"
printf 'Demo script ran on %s\n' "$(hostname)"
