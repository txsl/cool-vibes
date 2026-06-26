#!/usr/bin/env bash
#
# probe_range.sh - discover the setpoint range the controller actually accepts.
#
# Talks to a RUNNING daikin_server.py over its HTTP API (no Bluetooth needed
# here - the server does the BLE), so run the server first, then this. It is
# safe to run remotely / over SSH since it only makes HTTP calls to the server.
#
# For each whole degree in the sweep it sets the temperature and reads the value
# back: if the controller kept it, that degree is "accepted"; if the value snaps
# elsewhere, it's "rejected/clamped". It then prints the accepted min/max and
# restores your original setpoint.
#
# Usage:
#   ./probe_range.sh [LOW] [HIGH]      # defaults: 14 34
#
# Env overrides:
#   BASE=http://host:8000   server URL (default http://127.0.0.1:8000)
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
LOW="${1:-14}"
HIGH="${2:-34}"

# pull a field out of an /api/status-shaped JSON blob on stdin
field() { python3 -c "import sys,json; print(json.load(sys.stdin).get('$1'))"; }

status() { curl -fs --max-time 12 "$BASE/api/status"; }
set_temp() {
  curl -fs --max-time 20 -X POST "$BASE/api/setpoint" \
    -H 'Content-Type: application/json' -d "{\"temp\":$1}"
}

echo "Server: $BASE"
init="$(status)" || { echo "ERROR: cannot reach server at $BASE - is it running?"; exit 1; }
connected="$(printf '%s' "$init" | field connected)"
if [ "$connected" != "True" ]; then
  echo "ERROR: server reports controller not connected:"; printf '%s\n' "$init"; exit 1
fi

orig="$(printf '%s' "$init" | field setpoint)"
rep_min="$(printf '%s' "$init" | field min_setpoint)"
rep_max="$(printf '%s' "$init" | field max_setpoint)"
mode="$(printf '%s' "$init" | field mode)"
echo "Mode: $mode   original setpoint: ${orig}C"
echo "Controller-reported window: ${rep_min}C .. ${rep_max}C"
echo "Sweeping ${LOW}C .. ${HIGH}C ..."
echo

# Track the accepted range with plain scalars (macOS ships bash 3.2, which has
# no negative array subscripts).
first_ok=""; last_ok=""; n_ok=0
for t in $(seq "$LOW" "$HIGH"); do
  resp="$(set_temp "$t")" || { printf "  %2sC  -> API rejected (HTTP error)\n" "$t"; continue; }
  back="$(printf '%s' "$resp" | field setpoint)"
  if [ "$back" = "${t}.0" ] || [ "$back" = "$t" ]; then
    printf "  %2sC  -> accepted\n" "$t"
    [ -z "$first_ok" ] && first_ok="$t"; last_ok="$t"; n_ok=$((n_ok+1))
  else
    printf "  %2sC  -> rejected (stayed at %sC)\n" "$t" "$back"
  fi
done

echo
if [ "$n_ok" -gt 0 ]; then
  echo "Empirical accepted range: ${first_ok}C .. ${last_ok}C"
  if [ "${first_ok}.0" = "$rep_min" ] && [ "${last_ok}.0" = "$rep_max" ]; then
    echo "(matches the controller-reported window)"
  else
    echo "(NOTE: differs from reported window ${rep_min}..${rep_max} - worth a look)"
  fi
else
  echo "No degree in the sweep was accepted - check mode/power."
fi

echo
echo "Restoring original setpoint (${orig}C) ..."
set_temp "${orig%.*}" >/dev/null && echo "Restored." || echo "WARN: could not restore - set it manually."
