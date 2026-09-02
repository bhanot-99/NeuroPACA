#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — start (or resume) the 24 h dry-run soak.
#
#   ./scripts/start_b7_soak.sh
#
# The daemon now runs as a **persistent systemd --user service**
# (neuropaca-b7-soak.service), not a detached process: it is enabled, so it
# starts automatically every time you log in (this box has lingering on, so
# that includes right at boot, before login) and it receives a real SIGTERM
# on shutdown/logout, which the orchestrator already traps to save
# data/graph.json and stop cleanly (rules.md — no code change needed there).
#
# A second unit, neuropaca-b7-finalize-check.timer, replaces the old
# "compute one deadline 24 h5 min from now" transient timer. Because the
# daemon can now be off for stretches (laptop shut down overnight), there is
# no single instant to schedule for — validate_b7_dryrun.py measures the
# window from the first to the last audit line, in calendar time, not
# uptime. So instead the timer re-checks every 30 min (scripts/
# check_and_finalize_b7.sh) and finalizes the moment the log actually
# qualifies, however many power-cycles that took.
#
# Safe to re-run: refuses if the service is already active, and — same as
# before — archives (never deletes) any existing data/actions.jsonl first,
# so re-running this deliberately always starts a clean, unambiguous
# measurement window. A reboot alone does NOT go through this script and
# does NOT archive anything — systemd just restarts the same persistent
# service, so the log keeps accumulating across power-cycles, which is the
# whole point.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
CONFIG="neuropaca.soak.toml"
LOG="data/actions.jsonl"
DAEMON="${REPO}/.venv/bin/neuropacad"
UNIT_DIR="${HOME}/.config/systemd/user"
SOAK_UNIT="neuropaca-b7-soak"
CHECK_UNIT="neuropaca-b7-finalize-check"

[ -f "$CONFIG" ] || { echo "HALT — ${CONFIG} is missing."; exit 1; }
[ -x "$DAEMON" ] || { echo "HALT — ${DAEMON} not found; run 'uv pip install -e .'"; exit 1; }

if systemctl --user is-active --quiet "${SOAK_UNIT}.service" 2>/dev/null; then
    echo "HALT — ${SOAK_UNIT}.service is already active."
    echo "  Stop it first (./scripts/stop_b7_soak.sh) if you mean to restart the soak."
    exit 1
fi

mkdir -p data "$UNIT_DIR"
if [ -s "$LOG" ]; then
    ARCHIVE="data/actions.$(date +%Y%m%dT%H%M%S).jsonl"
    mv "$LOG" "$ARCHIVE"
    echo "archived the previous audit log -> ${ARCHIVE}"
    echo "  (a stale log would inflate the measured window; nothing is deleted)"
fi

echo "installing the systemd --user units…"
sed "s|__REPO__|${REPO}|g" scripts/systemd/neuropaca-b7-soak.service.template \
    >"${UNIT_DIR}/${SOAK_UNIT}.service"
sed "s|__REPO__|${REPO}|g" scripts/systemd/neuropaca-b7-finalize-check.service.template \
    >"${UNIT_DIR}/${CHECK_UNIT}.service"
sed "s|__REPO__|${REPO}|g" scripts/systemd/neuropaca-b7-finalize-check.timer.template \
    >"${UNIT_DIR}/${CHECK_UNIT}.timer"

systemctl --user daemon-reload
systemctl --user enable --now "${SOAK_UNIT}.service"
systemctl --user enable --now "${CHECK_UNIT}.timer"

sleep 3
if ! systemctl --user is-active --quiet "${SOAK_UNIT}.service"; then
    echo "HALT — the daemon did not stay up. Last lines:"
    tail -15 data/soak_b7.log
    exit 1
fi

echo
echo "=== B7 soak running (persistent) ==="
echo "  daemon    : systemctl --user status ${SOAK_UNIT}.service  (config ${CONFIG}, dry-run, both tiers)"
echo "  survives  : reboot / logout — enabled, restarts automatically, no manual step needed"
echo "  on stop   : SIGTERM -> the daemon saves data/graph.json and exits cleanly before power-off"
echo "  finalize  : ${CHECK_UNIT}.timer re-checks every 30 min; log at data/soak_b7_finalize_check.log"
echo "  proposals : tail -f ${LOG}"
echo "  cancel    : ./scripts/stop_b7_soak.sh"
echo
