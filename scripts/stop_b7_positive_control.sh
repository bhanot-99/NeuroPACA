#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — cancel / tear down the positive control.
#
#   ./scripts/stop_b7_positive_control.sh
#
# The mirror of scripts/schedule_b7_positive_control.sh. Cancels the transient
# timer if the run is still pending, and stops it cleanly if it is in flight:
#   - stop the neuropaca-b7-control timer + service (SIGTERM -> the driver traps
#     it, kills the CPU load, and SIGTERMs the control daemon so it saves its
#     graph);
#   - belt-and-braces: kill any stray driver / control daemon by command line.
#
# It does NOT touch the real B7 soak (neuropaca-b7-soak.service) and does NOT
# delete ~/.cache/neuropaca-b7-control/ — the logs and run_*.summary.json stay
# for inspection. Safe to run any time; exits 0 even if nothing was running.

set -euo pipefail

UNIT="neuropaca-b7-control"
CACHE="${HOME}/.cache/neuropaca-b7-control"

echo "=== stopping the B7 positive control ==="

# ------------------------------------------------------------- transient unit
if systemctl --user cat "${UNIT}.timer" >/dev/null 2>&1; then
    echo "  timer   : stopping ${UNIT}.timer"
    systemctl --user stop "${UNIT}.timer" >/dev/null 2>&1 || true
else
    echo "  timer   : not scheduled"
fi
if systemctl --user is-active --quiet "${UNIT}.service" 2>/dev/null; then
    echo "  service : stopping ${UNIT}.service (SIGTERM -> graceful load + daemon teardown)"
    systemctl --user stop "${UNIT}.service" >/dev/null 2>&1 || true
else
    echo "  service : not running"
fi
systemctl --user reset-failed "${UNIT}.service" "${UNIT}.timer" >/dev/null 2>&1 || true

# --------------------------------------------------------- stray processes
if pgrep -f "b7_positive_control.py" >/dev/null 2>&1; then
    echo "  driver  : killing stray b7_positive_control.py"
    pkill -TERM -f "b7_positive_control.py" || true
    sleep 3
    pkill -KILL -f "b7_positive_control.py" 2>/dev/null || true
fi
if pgrep -f "NEUROPACA_CONFIG=neuropaca.control.toml" >/dev/null 2>&1; then
    echo "  daemon  : killing stray control daemon"
    pkill -TERM -f "NEUROPACA_CONFIG=neuropaca.control.toml" || true
fi

# --------------------------------------------------------------------- state
if [ -d "$CACHE" ]; then
    LATEST="$(ls -t "${CACHE}"/run_*.summary.json 2>/dev/null | head -1 || true)"
    echo "  state   : kept in ${CACHE}"
    [ -n "$LATEST" ] && echo "            latest summary: ${LATEST}"
else
    echo "  state   : no control run has been executed yet"
fi

echo
echo "=== B7 positive control stopped ==="
echo "  The real soak (neuropaca-b7-soak.service) was not touched."
