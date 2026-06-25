#!/usr/bin/env bash
#
# debug_setpoint.sh - run the Daikin server with the (temporary) setpoint debug
# logging enabled, then print the set_setpoint trace lines on exit so they're
# easy to copy.
#
# IMPORTANT: must be run from a GRAPHICAL login session (directly on the Mac or
# via Screen Sharing). Bluetooth is denied to plain SSH sessions, so this will
# fail with "Bluetooth is not authorized ... DENIED_BY_UNKNOWN" over SSH.
#
# Usage:
#   ./debug_setpoint.sh [BLE_ADDRESS]
#
#   1. Run it.
#   2. Open the web page and change the temperature by a WHOLE degree
#      (e.g. 24 -> 25). The whole-degree step also tests whether the unit
#      rejects half-degree (x.5) setpoints.
#   3. Press Ctrl+C ONCE and wait for "Disconnected cleanly".
#   4. The script prints the captured set_setpoint: lines (also saved to $LOG).
#
# Overrides (env vars):
#   PYTHON=/path/to/python   interpreter to use (default: python3)
#   LOG=/path/to/log         log file (default: /tmp/daikin-setpoint-debug.log)
set -uo pipefail

cd "$(dirname "$0")"

ADDR="${1:-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
PYTHON="${PYTHON:-python3}"
LOG="${LOG:-/tmp/daikin-setpoint-debug.log}"

print_trace() {
  echo
  echo "================ set_setpoint trace ================"
  if grep -q "set_setpoint:" "$LOG" 2>/dev/null; then
    grep "set_setpoint:" "$LOG"
  else
    echo "(no set_setpoint lines captured - did you change the temperature?)"
  fi
  echo "==================================================="
  echo "Full log saved at: $LOG"
}
trap print_trace EXIT

echo "Starting Daikin server  (address: $ADDR)"
echo "Logging to: $LOG"
echo
echo ">>> Open the web page, change the temperature by a WHOLE degree (e.g. 24 -> 25),"
echo ">>> then press Ctrl+C ONCE and wait for 'Disconnected cleanly'."
echo

: > "$LOG"
"$PYTHON" daikin_server.py --address "$ADDR" 2>&1 | tee -a "$LOG"
