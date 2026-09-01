#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — start (or restart) the 24 h dry-run soak.
#
#   ./scripts/start_b7_soak.sh
#
# Starts the daemon detached under neuropaca.soak.toml (dry-run, BOTH tiers —
# everything proposed, nothing possible) and arms a systemd --user timer to run
# scripts/finalize_b7.sh 24 h 5 min from now.
#
# Safe to re-run: it refuses if a daemon is already up, and it re-arms the timer
# from *now*, so the window always matches the run that is actually happening.
#
# The soak measures a 24 h window from the FIRST audit line to the LAST, so a
# stale actions.jsonl from an earlier, abandoned attempt would fake the window.
# This script archives any existing log rather than deleting it (rules.md §5.7)
# so each attempt is measured on its own.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
CONFIG="neuropaca.soak.toml"
LOG="data/actions.jsonl"
DAEMON="${REPO}/.venv/bin/neuropacad"
UNIT="neuropaca-b7-finalize"

[ -f "$CONFIG" ] || { echo "HALT — ${CONFIG} is missing."; exit 1; }
[ -x "$DAEMON" ] || { echo "HALT — ${DAEMON} not found; run 'uv pip install -e .'"; exit 1; }

if pgrep -f "bin/neuropacad" >/dev/null 2>&1; then
    echo "HALT — a neuropacad is already running (pid $(pgrep -f 'bin/neuropacad' | head -1))."
    echo "  Stop it first (pkill -f neuropacad) if you mean to restart the soak."
    exit 1
fi

mkdir -p data
if [ -s "$LOG" ]; then
    ARCHIVE="data/actions.$(date +%Y%m%dT%H%M%S).jsonl"
    mv "$LOG" "$ARCHIVE"
    echo "archived the previous audit log -> ${ARCHIVE}"
    echo "  (a stale log would inflate the measured window; nothing is deleted)"
fi

echo "starting the daemon…"
NEUROPACA_CONFIG="$CONFIG" setsid nohup "$DAEMON" >> data/soak_b7.log 2>&1 < /dev/null &
sleep 8

if ! pgrep -f "bin/neuropacad" >/dev/null 2>&1; then
    echo "HALT — the daemon did not stay up. Last lines:"
    tail -15 data/soak_b7.log
    exit 1
fi

FINISH="$(date -d '+24 hours 5 minutes' '+%Y-%m-%d %H:%M:%S')"
systemctl --user stop "${UNIT}.timer" >/dev/null 2>&1 || true
systemctl --user reset-failed "${UNIT}.service" >/dev/null 2>&1 || true
systemd-run --user --on-calendar="$FINISH" --unit="$UNIT" \
    --working-directory="$REPO" \
    /bin/bash -c './scripts/finalize_b7.sh >> data/soak_b7_eval.log 2>&1' >/dev/null

echo
echo "=== B7 soak running ==="
echo "  daemon    : pid $(pgrep -f 'bin/neuropacad' | head -1) (config ${CONFIG}, dry-run, both tiers)"
echo "  window    : now -> $(date -d '+24 hours' '+%Y-%m-%d %H:%M %Z')"
echo "  finalises : ${FINISH} (systemd --user timer '${UNIT}')"
echo
echo "  status    : neuropaca health"
echo "  proposals : tail -f ${LOG}"
echo "  cancel    : ./scripts/stop_b7_soak.sh"
echo
echo "NOTE: shutting the laptop down kills both — the transient timer does not"
echo "      survive a reboot. Re-run this script after booting."
