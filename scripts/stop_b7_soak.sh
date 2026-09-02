#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — stop the 24 h dry-run soak.
#
#   ./scripts/stop_b7_soak.sh
#
# The mirror of scripts/start_b7_soak.sh. Since the daemon is now a
# persistent systemd --user service (survives reboot/logout), "stop" means
# **disable --now**, not just stop: disabling also removes it from
# default.target, so it will NOT come back on the next login/boot either.
# `systemctl ... stop` alone would leave it enabled and it would silently
# restart at the next login, which is not what "stop the soak" means here.
#
# The stop itself sends SIGTERM (systemd's default), which the orchestrator
# already traps to save data/graph.json before exiting — no separate save
# step needed.
#
# It does NOT touch data/actions.jsonl — the audit log is left in place so
# you can still inspect what the partial run proposed. start_b7_soak.sh
# archives it on the next run, so a stale log will not fake the window.
#
# Safe to run at any time: it reports what it found and exits 0 even if
# nothing was running or the units were never installed.

set -euo pipefail

cd "$(dirname "$0")/.."
LOG="data/actions.jsonl"
SOAK_UNIT="neuropaca-b7-soak"
CHECK_UNIT="neuropaca-b7-finalize-check"

echo "=== stopping the B7 soak ==="

# ------------------------------------------------------------------- the daemon
if systemctl --user list-unit-files "${SOAK_UNIT}.service" >/dev/null 2>&1 \
   && systemctl --user cat "${SOAK_UNIT}.service" >/dev/null 2>&1; then
    if systemctl --user is-active --quiet "${SOAK_UNIT}.service" 2>/dev/null; then
        echo "  daemon    : stopping + disabling ${SOAK_UNIT}.service (SIGTERM -> graceful save)"
    else
        echo "  daemon    : already stopped — disabling so it will not restart on next login"
    fi
    systemctl --user disable --now "${SOAK_UNIT}.service" >/dev/null 2>&1 || true
    echo "  daemon    : stopped and disabled"
else
    echo "  daemon    : no persistent unit installed (nothing to do)"
fi

# ---------------------------------------------------------- the finalize-check timer
if systemctl --user list-unit-files "${CHECK_UNIT}.timer" >/dev/null 2>&1 \
   && systemctl --user cat "${CHECK_UNIT}.timer" >/dev/null 2>&1; then
    echo "  timer     : stopping + disabling ${CHECK_UNIT}.timer"
    systemctl --user disable --now "${CHECK_UNIT}.timer" >/dev/null 2>&1 || true
    systemctl --user reset-failed "${CHECK_UNIT}.service" >/dev/null 2>&1 || true
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
