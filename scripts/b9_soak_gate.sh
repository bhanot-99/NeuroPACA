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
# Asked of the daemon, not of the journal. Measured on the target box
# 2026-09-03, `journalctl --user -u neuropacad` returns "No journal files were
# found" -- journald ships Storage=auto and /var/log/journal does not exist, so
# the journal is volatile and holds no per-user files. A grep against that is
# unconditionally empty, which reads as "nothing wrong" here and as "nothing
# happening" in check 5. Both are the apparatus lying, and this gate exists
# precisely because B7 spent three soaks believing an apparatus that lied.
PY="${REPO}/.venv/bin/python"
PROBE="${REPO}/scripts/b9_soak_probe.py"

"$PY" "$PROBE" | "$PY" -c '
import json, sys
sample = json.load(sys.stdin)
if not sample.get("daemon_up"):
    sys.exit("the daemon did not answer on the L9 socket")
if "activity" in sample.get("degraded", []):
    sys.exit("the activity collector reports itself degraded -- it self-disabled")
' || fail "the activity collector is not healthy (see above)"
echo "ok: the activity collector reports healthy over the socket"

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
# The counters are cumulative, so the question is whether they MOVED across the
# window -- a snapshot of "3 switches" proves only that something happened once,
# possibly before the gate started.
echo "watching for activity for ${MINUTES} min..."
BEFORE="$("$PY" "$PROBE")"
sleep $(( MINUTES * 60 ))
AFTER="$("$PY" "$PROBE")"

# Liveness is asked THREE ways, not one. The desk-shaped check (idle edges and
# app switches) stays, but it is no longer the only way to pass: it needs the
# user to have switched app or walked away inside the window, and someone
# working an hour in a single window produces neither. That failed a
# demonstrably healthy daemon on 2026-09-04 -- 121 snapshots collected, 2
# signals correlated, the graph advancing, and the gate still said "sensing is
# dead". phases.md recorded the narrowness when the gate was written and named
# this fix; this is it.
#
# Any ONE of the three proves L2/L3 are producing:
#   desk   -- idle transitions + app switches   (the original check)
#   graph  -- nodes or edges advancing          (L2/L3 wrote something)
#   signal -- L3 correlated a signal            (L2 collected AND L3 ran)
read -r DESK GRAPH SIGNALS PRESSURE <<<"$("$PY" -c '
import json, sys
before, after = json.loads(sys.argv[1]), json.loads(sys.argv[2])
def moved(key):
    # A daemon restart mid-window resets the counter; the after-value is then
    # the honest count for the life that is still running, and is never negative.
    delta = after.get(key, 0) - before.get(key, 0)
    return after.get(key, 0) if delta < 0 else delta
print(
    moved("activity_edges") + moved("app_switches"),
    moved("graph_nodes") + moved("graph_edges"),
    moved("signals"),
    moved("pressure_events"),
)
' "$BEFORE" "$AFTER")"

ACTIVITY=$(( DESK + GRAPH + SIGNALS ))

echo "idle edges + app switches:          ${DESK}"
echo "graph nodes + edges added:          ${GRAPH}"
echo "signals correlated by L3:           ${SIGNALS}"
echo "pressure contributions:             ${PRESSURE}"

[ "$ACTIVITY" -gt 0 ] || fail "no sign of life in ${MINUTES} min — every one of
  the three liveness signals stayed flat, so the sensing path really is not
  producing and a 7-day soak would prove nothing.
  (If the box was genuinely untouched AND idle for the whole window, that is
  indistinguishable from a dead collector; use the machine and re-run.)"

# The stamp is the gate's only durable output, and scripts/b9_soak_7day.sh
# refuses to start without it. Writing it HERE -- after all five checks and not
# one line earlier -- is what makes "the soak cannot run ungated" a property of
# the filesystem rather than of someone remembering the running order.
STAMP_FILE="${REPO}/data/b9_soak/gate-passed"
mkdir -p "$(dirname "$STAMP_FILE")"
{
  echo "gate passed ${STAMP}"
  echo "window_minutes ${MINUTES}"
  echo "daemon_pid ${PID}"
  echo "activity_edges ${ACTIVITY}"
  echo "desk_events ${DESK}"
  echo "graph_growth ${GRAPH}"
  echo "signals ${SIGNALS}"
  echo "pressure_mentions ${PRESSURE}"
  echo "log ${OUT}"
} > "$STAMP_FILE"

echo
echo "=== GATE PASSED ==="
echo "log:   $OUT"
echo "stamp: $STAMP_FILE"
echo
echo "Start the 7-day soak with sleep inhibited — a laptop that suspends does not"
echo "accumulate runtime, which is what ended the B2 soak at 11 h of a 24 h window:"
echo
echo "  systemctl --user enable --now neuropaca-b9-soak"
echo
echo "The unit wraps the driver in systemd-inhibit itself, starts with the"
echo "graphical session and stops when the machine does -- so the week survives"
echo "reboots and is accumulated across them."
