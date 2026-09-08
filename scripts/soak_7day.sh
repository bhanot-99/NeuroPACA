#!/usr/bin/env bash
# The 7-day soak (B9 exit criterion 4, rebuilt for B15). Run it, do not babysit it.
#
# WHAT THIS PROVES
#
# Nothing in the test suite can catch what only appears with process age:
# RSS that creeps 2 MiB/hour (invisible in a 10 s test, 340 MiB after a week),
# a buffer that turns out not to be bounded under a real event mix, relevance
# scores that oscillate instead of settling, a race that fires once every
# 50 000 events. Time is the assertion here. This run also subsumes the carried
# B1 T2 (60 min, conditional), B2 T3 (24 h, died at 11 h on a suspend) and B4
# (1 h, never run) windows -- four debts, one week.
#
# AND, post-B15: that the Wayland focus sensor stays alive for a week. B15_PLAN.md
# §2a found ~1 in 3 daemon starts came up permanently deaf (GC'd cosmic proxies) —
# the real reason B7's soaks fired L5 zero times. That is fixed; the residual
# ~1/20 flaky start is only *self-healed* by a liveness watchdog. `soak_state.py
# assess` fails the run on a week mostly deaf, on the watchdog reconnecting more
# than ~1/day (B15_PLAN.md §7 — escalate to a dedicated Wayland thread), or on any
# SIGSEGV marker (the two-connection exit-139 crash B15 §2b removed).
#
# HOW IT RUNS (changed deliberately from the original single-invocation design)
#
# It starts with the graphical session and stops when the machine does, driven
# by scripts/systemd/neuropaca-soak.service. A week therefore arrives as a
# *sequence* of sessions across power cycles rather than one uninterrupted
# process, and scripts/soak_state.py adds them up.
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
#   scripts/soak_7day.sh              # normal (what the unit runs)
#   scripts/soak_7day.sh --no-popup   # for a headless or scripted run
#   scripts/soak_7day.sh --status     # print the summary and exit
#   scripts/soak_7day.sh --assess     # print the PASS/FAIL verdict and exit
#
# EXIT
#   0  the session ended cleanly (stopped, or the 7 days completed)
#   1  the soak could not start -- the daemon is absent or the gate never passed

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="neuropacad.service"
SOAK_DIR="${REPO}/data/soak"
STATE="${SOAK_DIR}/state.json"
SAMPLES="${SOAK_DIR}/samples.jsonl"
LOG="${SOAK_DIR}/soak.log"
GATE_STAMP="${SOAK_DIR}/gate-passed"
SAMPLE_INTERVAL="${NEUROPACA_SOAK_INTERVAL:-60}"
PY="${REPO}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
STATE_TOOL="${REPO}/scripts/soak_state.py"

POPUP=1
for arg in "$@"; do
  case "$arg" in
    --no-popup) POPUP=0 ;;
    --status)
      "$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES"
      exit 0
      ;;
    --assess)
      set +e
      "$PY" "$STATE_TOOL" assess --state "$STATE" --samples "$SAMPLES"
      exit $?
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$SOAK_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

# --- refuse to start without the gate ----------------------------------------
# scripts/soak_gate.sh writes this stamp only after proving, on this machine,
# that the daemon has WAYLAND_DISPLAY, the collector did not self-disable, the
# L9 socket answers, and real activity edges appear. Spending a week without
# that is spending a week to learn nothing -- the whole B7 lesson.
if [ ! -f "$GATE_STAMP" ] && [ "${NEUROPACA_SOAK_SKIP_GATE:-0}" != "1" ]; then
  log "REFUSING TO START: no gate stamp at ${GATE_STAMP}"
  log "Run scripts/soak_gate.sh first (1 hour). It is the cheap version of"
  log "finding out that the sensing path is dead."
  exit 1
fi

# --- session bookkeeping ------------------------------------------------------
"$PY" "$STATE_TOOL" open --state "$STATE"
log "=== soak session opened ==="

finish() {
  local reason="${1:-stopped}"
  # Every step best-effort. This runs from a signal handler while the machine is
  # shutting down; a non-zero return under `set -e` would skip the steps after it
  # and hand systemd a failure exit -> Restart=on-failure churns the sample file.
  # The `close` is the one that matters -- a missed summary line is not worth an
  # aborted shutdown.
  "$PY" "$STATE_TOOL" close --state "$STATE" --reason "$reason" || true
  log "=== soak session closed (${reason}) ===" || true
  "$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES" | tee -a "$LOG" || true
}

# SIGTERM is what systemd sends at shutdown and at `systemctl --user stop`.
# Catching it is the difference between a session that is added to the total and
# one the next boot has to heal from a heartbeat -- but the unit must use
# `KillMode=control-group` for this trap to fire at all, because the driver runs
# as a child of `systemd-inhibit` (see neuropaca-soak.service). `|| true` so a
# hiccup in finish() still reaches `exit 0`.
trap 'finish sigterm || true; exit 0' TERM
trap 'finish sigint || true; exit 0' INT

# --- the popup ----------------------------------------------------------------
show_popup() {
  [ "$POPUP" = "1" ] || return 0
  command -v zenity >/dev/null 2>&1 || { log "zenity absent -- popup skipped"; return 0; }
  local body
  body="$("$PY" "$STATE_TOOL" summary --state "$STATE" --samples "$SAMPLES")"
  # At completion, put the PASS/FAIL verdict in front of the numbers.
  if [ "${1:-}" = "--with-verdict" ]; then
    body="$("$PY" "$STATE_TOOL" assess --state "$STATE" --samples "$SAMPLES" 2>/dev/null)

${body}"
  fi
  # Detached and disowned: the dialog waits for a human to click OK, and the
  # soak must not wait with it. No --timeout, by request and by sense -- a
  # notification that vanishes while you are getting coffee has told you nothing.
  setsid zenity --info \
    --title="NeuroPACA · 7-day soak" \
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
  metrics="$("$PY" "${REPO}/scripts/soak_probe.py" \
               --actions-log "${REPO}/data/actions.jsonl" 2>/dev/null)" || return 1
  [ -n "$metrics" ] || return 1
  "$PY" "$STATE_TOOL" sample --state "$STATE" --samples "$SAMPLES" --metrics "$metrics"
}

# --- segfault watch ---------------------------------------------------------
# The B15 §2b bug was two Wayland Display connections crashing the daemon on
# teardown (exit 139). It is fixed, but a week is exactly how long you wait to be
# sure. `Restart=on-failure` brings the daemon straight back, so a crash is only
# visible as NRestarts ticking up with ExecMainStatus 139 — catch that edge and
# write a marker row the `assess` verdict fails on.
LAST_NRESTARTS=-1
check_daemon_crash() {
  local n status
  n="$(systemctl --user show -p NRestarts --value "$UNIT" 2>/dev/null || echo 0)"
  status="$(systemctl --user show -p ExecMainStatus --value "$UNIT" 2>/dev/null || echo 0)"
  [ "$LAST_NRESTARTS" -lt 0 ] && LAST_NRESTARTS="$n"
  if [ "$n" -gt "$LAST_NRESTARTS" ] && [ "$status" = "139" ]; then
    log "SIGSEGV: neuropacad exited 139 (B15 §2b signature) — restart #${n}"
    "$PY" "$STATE_TOOL" sample --state "$STATE" --samples "$SAMPLES" \
      --metrics "{\"daemon_up\": false, \"segfault\": true, \"nrestarts\": ${n}}"
  fi
  LAST_NRESTARTS="$n"
}

# --- run ----------------------------------------------------------------------
show_popup

while true; do
  sleep "$SAMPLE_INTERVAL" &
  wait $! || true          # `wait` so the TERM trap fires during the sleep,
                           # not $SAMPLE_INTERVAL seconds after shutdown began
  sample_once || log "sample failed (continuing -- one lost measurement is not a lost soak)"
  check_daemon_crash || true
  if "$PY" "$STATE_TOOL" status --state "$STATE"; then
    log "TARGET REACHED -- 7 days of accrued runtime"
    finish completed
    "$PY" "$STATE_TOOL" assess --state "$STATE" --samples "$SAMPLES" | tee -a "$LOG" || true
    show_popup --with-verdict
    exit 0
  fi
done
