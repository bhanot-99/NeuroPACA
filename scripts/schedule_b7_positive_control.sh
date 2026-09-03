#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — schedule the positive control off-hours.
#
#   ./scripts/schedule_b7_positive_control.sh <when> [minutes]
#
#   <when>    a systemd time spec. Two shapes:
#               relative : "+3h", "90min", "tomorrow"   -> --on-active
#               absolute : "2026-09-03 02:00", "02:00"  -> --on-calendar
#   minutes   hard wall-clock bound for the run (default 300 = 5 h)
#
# Examples:
#   ./scripts/schedule_b7_positive_control.sh "2026-09-03 02:00" 300
#   ./scripts/schedule_b7_positive_control.sh "+6h" 360
#
# It registers a transient systemd --user timer (unit: neuropaca-b7-control)
# that runs scripts/b7_positive_control.py once, then disappears. The service
# gets RuntimeMaxSec = minutes + 15 min grace, so even if the driver wedges,
# systemd SIGTERMs it (which the driver traps to kill the load and stop its
# daemon). The CPU workers run at nice 19 — if you sit down mid-run, your
# foreground work still preempts them instantly.
#
#   status : systemctl --user list-timers neuropaca-b7-control.timer
#            journalctl --user -u neuropaca-b7-control.service -f
#   cancel : ./scripts/stop_b7_positive_control.sh

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
UNIT="neuropaca-b7-control"
PY="${REPO}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

WHEN="${1:-}"
MINUTES="${2:-300}"
if [ -z "$WHEN" ]; then
    echo "usage: $0 <when> [minutes]" >&2
    echo "  e.g. $0 \"2026-09-03 02:00\" 300   |   $0 \"+6h\" 360" >&2
    exit 1
fi
case "$MINUTES" in
    ''|*[!0-9]*) echo "HALT — minutes must be an integer, got '${MINUTES}'." >&2; exit 1 ;;
esac

[ -f "${REPO}/neuropaca.control.toml" ] || { echo "HALT — neuropaca.control.toml missing." >&2; exit 1; }
[ -x "${REPO}/.venv/bin/neuropacad" ] || { echo "HALT — .venv/bin/neuropacad missing; run 'uv pip install -e .'" >&2; exit 1; }

if systemctl --user list-timers --all 2>/dev/null | grep -q "${UNIT}.timer" \
   || systemctl --user is-active --quiet "${UNIT}.service" 2>/dev/null; then
    echo "HALT — ${UNIT} is already scheduled or running. Cancel it first:" >&2
    echo "  ./scripts/stop_b7_positive_control.sh" >&2
    exit 1
fi

# Relative specs (leading '+', or a bare duration/keyword) -> --on-active.
# Everything else is treated as an OnCalendar timestamp.
if [[ "$WHEN" == +* ]]; then
    TIMER_FLAG=(--on-active="${WHEN#+}")
elif [[ "$WHEN" =~ ^[0-9]+(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours|d|day|days)?$ || "$WHEN" == "tomorrow" ]]; then
    TIMER_FLAG=(--on-active="$WHEN")
else
    TIMER_FLAG=(--on-calendar="$WHEN")
fi

GRACE=$(( MINUTES * 60 + 900 ))

echo "=== scheduling B7 positive control ==="
echo "  when          : ${WHEN}   (${TIMER_FLAG[*]})"
echo "  run bound     : ${MINUTES} min   (RuntimeMaxSec ${GRACE}s incl. 15 min grace)"
echo "  unit          : ${UNIT}"
echo "  driver        : scripts/b7_positive_control.py --minutes ${MINUTES}"
echo "  writes        : ~/.cache/neuropaca-b7-control/  (nothing under the repo)"
echo

systemd-run --user \
    --unit="$UNIT" \
    "${TIMER_FLAG[@]}" \
    --working-directory="$REPO" \
    --property=RuntimeMaxSec="${GRACE}" \
    --property=KillSignal=SIGTERM \
    --property=TimeoutStopSec=120 \
    --setenv=NEUROPACA_CONFIG=neuropaca.control.toml \
    "$PY" scripts/b7_positive_control.py --minutes "$MINUTES"

echo
echo "scheduled. inspect with:"
echo "  systemctl --user list-timers '${UNIT}.timer'"
echo "  journalctl --user -u '${UNIT}.service' -f"
echo "cancel with:"
echo "  ./scripts/stop_b7_positive_control.sh"
