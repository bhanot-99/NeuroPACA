#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — stop the 24 h dry-run soak.
#
#   ./scripts/stop_b7_soak.sh
#
# The mirror of scripts/start_b7_soak.sh: it stops the detached daemon and
# disarms the systemd --user timer that would otherwise run finalize_b7.sh.
# This is the two-command "cancel:" snippet that start_b7_soak.sh prints,
# packaged so it is hard to get wrong.
#
# It does NOT touch data/actions.jsonl — the audit log is left in place so you
# can still inspect what the partial run proposed. start_b7_soak.sh archives it
# on the next run, so a stale log will not fake the window.
#
# Safe to run at any time: it reports what it found and exits 0 even if nothing
# was running.

set -euo pipefail

cd "$(dirname "$0")/.."
UNIT="neuropaca-b7-finalize"
LOG="data/actions.jsonl"

echo "=== stopping the B7 soak ==="

# ------------------------------------------------------------------- the daemon
if pgrep -f "bin/neuropacad" >/dev/null 2>&1; then
    PIDS="$(pgrep -f 'bin/neuropacad' | tr '\n' ' ')"
    echo "  daemon    : stopping neuropacad (pid ${PIDS})"
    pkill -f "bin/neuropacad" || true
    sleep 3
    if pgrep -f "bin/neuropacad" >/dev/null 2>&1; then
        echo "  daemon    : still up — sending SIGKILL"
        pkill -9 -f "bin/neuropacad" || true
    fi
    echo "  daemon    : stopped"
else
    echo "  daemon    : not running"
fi

# ---------------------------------------------------------- the finalise timer
if systemctl --user cat "${UNIT}.timer" >/dev/null 2>&1 \
   || systemctl --user list-timers --all 2>/dev/null | grep -q "${UNIT}"; then
    echo "  timer     : disarming '${UNIT}'"
    systemctl --user stop "${UNIT}.timer" >/dev/null 2>&1 || true
    systemctl --user stop "${UNIT}.service" >/dev/null 2>&1 || true
    systemctl --user reset-failed "${UNIT}.service" >/dev/null 2>&1 || true
    echo "  timer     : disarmed"
else
    echo "  timer     : not armed"
fi

# --------------------------------------------------------------------- the log
if [ -s "$LOG" ]; then
    echo "  audit log : ${LOG} kept ($(wc -l < "$LOG") lines) — start_b7_soak.sh archives it on the next run"
else
    echo "  audit log : none written"
fi

echo
echo "=== B7 soak stopped ==="
echo "  Resume with: ./scripts/start_b7_soak.sh  (restarts the 24 h window from then)"
