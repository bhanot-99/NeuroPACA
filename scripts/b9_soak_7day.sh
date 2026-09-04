#!/usr/bin/env bash
# B9 · the 7-day soak (exit criterion 4). Run it, do not babysit it.
#
# WHAT THIS PROVES
#
# Nothing in the 342-test suite can catch what only appears with process age:
# RSS that creeps 2 MiB/hour (invisible in a 10 s test, 340 MiB after a week),
# a buffer that turns out not to be bounded under a real event mix, relevance
# scores that oscillate instead of settling, a race that fires once every
# 50 000 events. Time is the assertion here. This run also subsumes the carried
# B1 T2 (60 min, conditional), B2 T3 (24 h, died at 11 h on a suspend) and B4
# (1 h, never run) windows -- four debts, one week.
#
# HOW IT RUNS (changed deliberately from the original single-invocation design)
#
# It starts with the graphical session and stops when the machine does, driven
# by scripts/systemd/neuropaca-b9-soak.service. A week therefore arrives as a
# *sequence* of sessions across power cycles rather than one uninterrupted
# process, and scripts/b9_soak_state.py adds them up.
#
#   Completion is 7 days of ACCRUED RUNTIME, not 7 days on the calendar.
#   A box powered off overnight ages no process, and counting those hours would
#   let a 3.5-day soak claim a 7-day result. That is precisely how the B2 soak
#   got to 11 h of 24 and had to be thrown away.
#
# systemd-inhibit --what=sleep:idle is still applied, for the same reason: a
# laptop that suspends mid-session accrues nothing while it sleeps. Explicit
# shutdown is not inhibited -- SIGTERM arrives, the session closes cleanly, and
# the next boot picks the total back up.
#
# THE POPUP
#
# Every start raises a zenity dialog with the results so far and an OK button,
# and it never auto-dismisses. This is not decoration. B7 burned three soaks
# before anyone noticed L5 had fired zero times, because a soak that is silently
# producing nothing looks exactly like a soak of a working-but-idle system. A
# dialog in front of your face at every login makes a week of accumulating
# nothing impossible to not notice on day two.
#
# USAGE
#   scripts/b9_soak_7day.sh              # normal (what the unit runs)
#   scripts/b9_soak_7day.sh --no-popup   # for a headless or scripted run
#   scripts/b9_soak_7day.sh --status     # print the summary and exit
#
# EXIT
#   0  the session ended cleanly (stopped, or the 7 days completed)
#   1  the soak could not start -- the daemon is absent or the gate never passed

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="neuropacad.service"
SOAK_DIR="${REPO}/data/b9_soak"
STATE="${SOAK_DIR}/state.json"
SAMPLES="${SOAK_DIR}/samples.jsonl"
LOG="${SOAK_DIR}/soak.log"
GATE_STAMP="${SOAK_DIR}/gate-passed"
SAMPLE_INTERVAL="${NEUROPACA_SOAK_INTERVAL:-60}"
PY="${REPO}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
STATE_TOOL="${REPO}/scripts/b9_soak_state.py"

POPUP=1
for arg in "$@"; do
  case "$arg" in
    --no-popup) POPUP=0 ;;
    --status)
      "$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES"
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$SOAK_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

# --- refuse to start without the gate ----------------------------------------
# scripts/b9_soak_gate.sh writes this stamp only after proving, on this machine,
# that the daemon has WAYLAND_DISPLAY, the collector did not self-disable, the
# L9 socket answers, and real activity edges appear. Spending a week without
# that is spending a week to learn nothing -- the whole B7 lesson.
if [ ! -f "$GATE_STAMP" ] && [ "${NEUROPACA_SOAK_SKIP_GATE:-0}" != "1" ]; then
  log "REFUSING TO START: no gate stamp at ${GATE_STAMP}"
  log "Run scripts/b9_soak_gate.sh first (1 hour). It is the cheap version of"
  log "finding out that the sensing path is dead."
  exit 1
fi

# --- session bookkeeping ------------------------------------------------------
"$PY" "$STATE_TOOL" open --state "$STATE"
log "=== soak session opened ==="

finish() {
  local reason="${1:-stopped}"
  "$PY" "$STATE_TOOL" close --state "$STATE" --reason "$reason"
  log "=== soak session closed (${reason}) ==="
  "$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES" | tee -a "$LOG"
}

# SIGTERM is what systemd sends at shutdown and at `systemctl --user stop`.
# Catching it is the difference between a session that is added to the total and
# one the next boot has to heal from a heartbeat.
trap 'finish sigterm; exit 0' TERM
trap 'finish sigint; exit 0' INT

# --- the popup ----------------------------------------------------------------
show_popup() {
  [ "$POPUP" = "1" ] || return 0
  command -v zenity >/dev/null 2>&1 || { log "zenity absent -- popup skipped"; return 0; }
  local body
  body="$("$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES")"
  # Detached and disowned: the dialog waits for a human to click OK, and the
  # soak must not wait with it. No --timeout, by request and by sense -- a
  # notification that vanishes while you are getting coffee has told you nothing.
  setsid zenity --info \
    --title="NeuroPACA · B9 7-day soak" \
    --width=560 \
    --ok-label="OK" \
    --text="<tt>$(printf '%s' "$body" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')</tt>" \
    >/dev/null 2>&1 &
  disown 2>/dev/null || true
  log "popup raised"
}

# --- sampling -----------------------------------------------------------------
# Measurements come from `neuropaca health` over the L9 socket, NOT from
# journalctl. Measured on this box 2026-09-03, `journalctl --user -u neuropacad`
# returns "No journal files were found": journald ships Storage=auto with no
# /var/log/journal, so there are no per-user journal files to grep. A sampler
# reading that would have recorded a week of zeros for a healthy system and
# called it a finding -- the B7 mistake, wearing a different hat.
sample_once() {
  local metrics
  metrics="$("$PY" "${REPO}/scripts/b9_soak_probe.py" \
               --actions-log "${REPO}/data/actions.jsonl" 2>/dev/null)" || return 1
  [ -n "$metrics" ] || return 1
  "$PY" "$STATE_TOOL" sample --state "$STATE" --samples "$SAMPLES" --metrics "$metrics"
}

# --- run ----------------------------------------------------------------------
show_popup

while true; do
  sleep "$SAMPLE_INTERVAL" &
  wait $! || true          # `wait` so the TERM trap fires during the sleep,
                           # not $SAMPLE_INTERVAL seconds after shutdown began
  sample_once || log "sample failed (continuing -- one lost measurement is not a lost soak)"
  if "$PY" "$STATE_TOOL" status --state "$STATE"; then
    log "TARGET REACHED -- 7 days of accrued runtime"
    finish completed
    show_popup
    exit 0
  fi
done
