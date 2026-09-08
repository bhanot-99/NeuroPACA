#!/usr/bin/env bash
# The 1-hour live gate that must pass BEFORE the 7-day soak starts (BL-5).
#
# WHY THIS EXISTS
#
# B7 ran three soaks and L5 fired zero times; B9's first gate saw ~4 app switches
# in a full hour of real use. Both were blamed on "the collector cannot see
# Wayland under systemd --user". B15 found the real cause (B15_PLAN.md §2a): the
# `zcosmic_toplevel_handle_v1` proxy carrying "which window is focused" was kept
# in a local variable and GC'd non-deterministically — ~1 in 3 daemon starts came
# up permanently deaf. Fixed (strong-ref dict + one shared connection), and this
# gate is what proves the fix holds on THIS machine, this boot, not just in the
# reasoning. The 2026-09-03 gate pass and the "soak running" state it produced
# were a deaf sensor the whole time and do not count.
#
# Without the gate, a 7-day soak that generates no pressure is indistinguishable
# from a 7-day soak of a working system that happened to be idle — and it is this
# soak that is supposed to subsume the carried B1 T2, B2 T3 and B4 windows. A week
# is too expensive to spend finding that out at the end.
#
# THE POST-B15 BAR (check 5). B15_PLAN.md §6 criterion 1: the daemon must record
# "dozens of focus/tab switches per hour, not ~4". So a pass now needs a real
# switch rate (>= 20/h), a Wayland connection that is not thrashing (<= 1
# reconnect, 0 pump-errors in the window), and window✓ live — not merely "one of
# three liveness signals moved".
#
# EXIT
#   0  gate passed — the 7-day soak may start
#   1  gate failed — fix the cause; do NOT start the soak
#
# USAGE
#   scripts/soak_gate.sh [minutes]      # default 60; use a small value for a dry run

set -euo pipefail

MINUTES="${1:-60}"
UNIT="neuropacad.service"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${REPO}/data/soak_gate_${STAMP}.log"

mkdir -p "${REPO}/data"
exec > >(tee -a "$OUT") 2>&1

echo "=== soak gate · ${STAMP} · ${MINUTES} min ==="

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
PROBE="${REPO}/scripts/soak_probe.py"

"$PY" "$PROBE" | "$PY" -c '
import json, sys
sample = json.load(sys.stdin)
if not sample.get("daemon_up"):
    sys.exit("the daemon did not answer on the L9 socket")
if "activity" in sample.get("degraded", []):
    sys.exit("the activity collector reports itself degraded -- it self-disabled")
if not sample.get("window_ok"):
    sys.exit("window is not live (window not ok) -- the B15 focus sensor is deaf right now")
' || fail "the activity collector is not healthy (see above)"
echo "ok: the activity collector reports healthy, window✓ live over the socket"

# --- 4. the CLI works under the hardened unit (BL-1) --------------------------
# ProtectSystem=strict made $XDG_RUNTIME_DIR read-only, so the L9 socket could
# not be bound and every verb died — `confirm` included, which is the only
# approval path for a dangerous action. Prove the socket is live before spending
# a week soaking a daemon nobody can talk to.
"${REPO}/.venv/bin/neuropaca" health >/dev/null \
  || fail "\`neuropaca health\` failed — the L9 socket is not reachable under the
  unit (check ReadWritePaths=%t in neuropacad.service)"
echo "ok: neuropaca health answers over the socket"

# --- 5. the post-B15 sensing bar over the window -----------------------------
# The counters are cumulative, so the question is whether they MOVED across the
# window. Post-B15 the bar is a real switch RATE, not "any one liveness signal
# moved once": B15_PLAN.md §6 crit 1 is "dozens per hour, not ~4".
#
# PASS needs ALL of:
#   switches   >= NEED_SWITCHES (20/h, scaled to the window)   -- the desk moved
#              OR  graph grew AND a signal correlated           -- L2/L3 alive in
#                                                                  a genuine
#                                                                  single-window
#                                                                  hour
#   reconnects <= 1 in the window   -- the shared connection is not thrashing
#   pump_errors == 0 in the window  -- no swallowed pump exceptions
#   window_ok  -- the daemon's own live is_alive for the focus sensor
NEED_SWITCHES=$(( 20 * MINUTES / 60 ))
[ "$NEED_SWITCHES" -lt 1 ] && NEED_SWITCHES=1

echo "watching for ${MINUTES} min (need >= ${NEED_SWITCHES} app switches, or graph+signal growth)..."
BEFORE="$("$PY" "$PROBE")"
sleep $(( MINUTES * 60 ))
AFTER="$("$PY" "$PROBE")"

read -r SWITCHES GRAPH SIGNALS RECONNECTS PUMP_ERRS WINDOW_OK RATE <<<"$("$PY" -c '
import json, sys
before, after = json.loads(sys.argv[1]), json.loads(sys.argv[2])
minutes = float(sys.argv[3])
def moved(key):
    # A daemon restart mid-window resets a cumulative counter; the after-value is
    # then the honest count for the life still running, and is never negative.
    delta = after.get(key, 0) - before.get(key, 0)
    return after.get(key, 0) if delta < 0 else delta
switches = moved("app_switches")
rate = switches * 60.0 / minutes if minutes else 0.0
print(
    switches,
    moved("graph_nodes") + moved("graph_edges"),
    moved("signals"),
    moved("reconnects"),
    moved("pump_errors"),
    "1" if after.get("window_ok") else "0",
    f"{rate:.1f}",
)
' "$BEFORE" "$AFTER" "$MINUTES")"

echo "app switches:                        ${SWITCHES}  (${RATE}/h)"
echo "graph nodes + edges added:           ${GRAPH}"
echo "signals correlated by L3:            ${SIGNALS}"
echo "wayland reconnects in window:        ${RECONNECTS}"
echo "wayland pump errors in window:       ${PUMP_ERRS}"
echo "window✓ at end:                      ${WINDOW_OK}"

[ "$WINDOW_OK" = "1" ] || fail "the focus sensor was not live at the end of the window (window✗).
  B15 §2a deafness or a burnt reconnect budget. Restart the daemon and re-run."

[ "$PUMP_ERRS" -eq 0 ] || fail "${PUMP_ERRS} Wayland pump error(s) during the window — the
  connection is raising and reconnecting. Check the daemon log before a 7-day run."

[ "$RECONNECTS" -le 1 ] || fail "${RECONNECTS} Wayland reconnects in ${MINUTES} min — the
  connection is thrashing (B15_PLAN.md §7: watchdog insufficient). Do not start the soak."

if [ "$SWITCHES" -ge "$NEED_SWITCHES" ]; then
  echo "ok: switch rate ${RATE}/h clears the post-B15 bar"
elif [ "$GRAPH" -gt 0 ] && [ "$SIGNALS" -gt 0 ]; then
  echo "ok: only ${SWITCHES} switches, but the graph grew (${GRAPH}) and L3 correlated ${SIGNALS} signal(s) — L2/L3 are alive"
else
  fail "only ${SWITCHES} app switches in ${MINUTES} min (${RATE}/h, need ${NEED_SWITCHES}) and no
  graph+signal growth to fall back on. Either the sensor is still slow (re-check B15)
  or the box was genuinely idle/single-window — use the machine and re-run."
fi

# The stamp is the gate's only durable output, and scripts/soak_7day.sh
# refuses to start without it. Writing it HERE -- after all five checks and not
# one line earlier -- is what makes "the soak cannot run ungated" a property of
# the filesystem rather than of someone remembering the running order.
STAMP_FILE="${REPO}/data/soak/gate-passed"
mkdir -p "$(dirname "$STAMP_FILE")"
{
  echo "gate passed ${STAMP}"
  echo "window_minutes ${MINUTES}"
  echo "daemon_pid ${PID}"
  echo "app_switches ${SWITCHES}"
  echo "switch_rate_per_hour ${RATE}"
  echo "graph_growth ${GRAPH}"
  echo "signals ${SIGNALS}"
  echo "reconnects ${RECONNECTS}"
  echo "pump_errors ${PUMP_ERRS}"
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
echo "  systemctl --user enable --now neuropaca-soak"
echo
echo "The unit wraps the driver in systemd-inhibit itself, starts with the"
echo "graphical session and stops when the machine does -- so the week survives"
echo "reboots and is accumulated across them."
