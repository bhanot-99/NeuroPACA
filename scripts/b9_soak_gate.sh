#!/usr/bin/env bash
# B9 · the 1-hour live gate that must pass BEFORE the 7-day soak starts (BL-5).
#
# WHY THIS EXISTS
#
# B7 ran three soaks and L5 fired zero times in all three. The recorded cause
# was "the Wayland activity collector cannot see Wayland under systemd --user",
# which was wrong: measured on the target box 2026-09-03 the variable is present
# in the manager environment, and the daemon simply started before the compositor
# imported it (see scripts/systemd/neuropacad.service). The unit now binds to
# graphical-session.target, and this gate is what proves that fix actually works
# on this machine rather than only in the reasoning.
#
# Without the gate, a 7-day soak that generates no pressure is indistinguishable
# from a 7-day soak of a working system that happened to be idle — and it is this
# soak that is supposed to subsume the carried B1 T2, B2 T3 and B4 windows. A week
# is too expensive to spend finding that out at the end.
#
# EXIT
#   0  gate passed — the 7-day soak may start
#   1  gate failed — fix the cause; do NOT start the soak
#
# USAGE
#   scripts/b9_soak_gate.sh [minutes]      # default 60

set -euo pipefail

MINUTES="${1:-60}"
UNIT="neuropacad.service"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${REPO}/data/b9_gate_${STAMP}.log"

mkdir -p "${REPO}/data"
exec > >(tee -a "$OUT") 2>&1

echo "=== B9 soak gate · ${STAMP} · ${MINUTES} min ==="

fail() { echo "GATE FAILED: $*"; exit 1; }

# --- 1. the unit is running under the graphical session -----------------------
systemctl --user is-active --quiet "$UNIT" \
  || fail "$UNIT is not active — run 'systemctl --user start $UNIT' first"

systemctl --user show -p WantedBy -p After "$UNIT" | grep -q graphical-session.target \
  || fail "$UNIT is not bound to graphical-session.target — the B7 blindspot is back"

# --- 2. the daemon actually has WAYLAND_DISPLAY -------------------------------
# The whole point of the gate. Read it from the live process, not from
# `systemctl --user show-environment`: the manager's environment is what the old
# unit *also* had, while the process environment is what the daemon inherited at
# exec time, which is precisely where B7 went wrong.
PID="$(systemctl --user show -p MainPID --value "$UNIT")"
[ -n "$PID" ] && [ "$PID" != "0" ] || fail "cannot resolve the daemon MainPID"

tr '\0' '\n' < "/proc/${PID}/environ" | grep -q '^WAYLAND_DISPLAY=' \
  || fail "the daemon process has no WAYLAND_DISPLAY — it started before the
  compositor imported the session environment. This is exactly the B7 failure;
  the graphical-session.target binding did not take."

echo "ok: daemon pid ${PID} has WAYLAND_DISPLAY"

# --- 3. the activity collector did not self-disable ---------------------------
if journalctl --user -u "$UNIT" --since "-10 min" 2>/dev/null \
     | grep -qi 'no \$WAYLAND_DISPLAY\|collector-disabled'; then
  fail "the activity collector self-disabled — check the journal"
fi
echo "ok: no collector-disabled in the last 10 minutes"

# --- 4. the CLI works under the hardened unit (BL-1) --------------------------
# ProtectSystem=strict made $XDG_RUNTIME_DIR read-only, so the L9 socket could
# not be bound and every verb died — `confirm` included, which is the only
# approval path for a dangerous action. Prove the socket is live before spending
# a week soaking a daemon nobody can talk to.
"${REPO}/.venv/bin/neuropaca" health >/dev/null \
  || fail "\`neuropaca health\` failed — the L9 socket is not reachable under the
  unit (check ReadWritePaths=%t in neuropacad.service)"
echo "ok: neuropaca health answers over the socket"

# --- 5. watch for real sensing/pressure activity over the window --------------
echo "watching for activity/pressure for ${MINUTES} min..."
START_EPOCH="$(date +%s)"
sleep $(( MINUTES * 60 )) &
SLEEP_PID=$!
trap 'kill "$SLEEP_PID" 2>/dev/null || true' EXIT
wait "$SLEEP_PID" || true

SINCE="@${START_EPOCH}"
ACTIVITY="$(journalctl --user -u "$UNIT" --since "$SINCE" 2>/dev/null \
            | grep -ci 'ACTIVITY_DETECTED\|IDLE_DETECTED' || true)"
PRESSURE="$(journalctl --user -u "$UNIT" --since "$SINCE" 2>/dev/null \
            | grep -ci 'pressure' || true)"

echo "activity/idle edges: ${ACTIVITY}"
echo "pressure mentions:   ${PRESSURE}"

[ "$ACTIVITY" -gt 0 ] || fail "zero activity/idle edges in ${MINUTES} min — the
  sensing path is not producing events, so a 7-day soak would prove nothing.
  (Use the machine normally during the gate; an untouched box is legitimately
  idle, and the gate cannot tell that apart from a dead collector.)"

echo
echo "=== GATE PASSED ==="
echo "log: $OUT"
echo
echo "Start the 7-day soak with sleep inhibited — a laptop that suspends does not"
echo "accumulate runtime, which is what ended the B2 soak at 11 h of a 24 h window:"
echo
echo "  systemd-inhibit --what=sleep:idle --who=NeuroPACA --why='B9 7-day soak' \\"
echo "    scripts/b9_soak_7day.sh"
