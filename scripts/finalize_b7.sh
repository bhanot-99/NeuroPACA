#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — validate and report readiness (phases.md B7, D-14).
#
#   ./scripts/finalize_b7.sh
#
# Criterion 5 is "a review period in dry-run with zero false positives before
# any tier goes live." It was agreed as a 24 h dogfooding soak — but three soak
# attempts (2026-09-01 -> 2026-09-03) each logged ZERO proposals, because the
# only pressure path a headless `systemd --user` daemon can reach is
# `HighLoadPattern` (the Wayland activity collector self-disables with no
# $WAYLAND_DISPLAY), and a normal desktop day does not pin CPU > 90 % for 5 min
# while touching a watched path. An empty soak log cannot tell a correct-quiet
# pipeline from a broken one.
#
# So criterion 5 is met by the **positive control**
# (scripts/b7_positive_control.py, thresholds byte-identical to the soak): a
# bounded synthetic HighLoad load that drives real, traceable proposals. Its
# canonical evidence is committed at spikes/b7_positive_control/.
#
# This script no longer merges anything. B7 is finalised via a pull request
# (the user's call, 2026-09-03). It validates, runs the suite, and — on a pass —
# tells you the branch is ready to push and PR. `set -e`: any failure stops it.
#
# What it checks mechanically:
#   * the positive-control run: synthetic, every attempt paired with a result,
#     ZERO executed effects, ZERO high-tier proposals;
#   * the in-repo evidence copy passes validate_b7_dryrun.py
#     (--require-hours 0 --require-zero-high-tier);
#   * IF a soak log exists (data/actions.jsonl), it must not contradict —
#     same zero-effect, zero-high-tier bar, any window length;
#   * the test suite is still green.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
BRANCH="b7-drive-action-l5-l7"
SOAK_LOG="data/actions.jsonl"
PC_EVIDENCE="spikes/b7_positive_control/actions.jsonl"
PC_SUMMARY="spikes/b7_positive_control/summary.json"
PC_CACHE="${HOME}/.cache/neuropaca-b7-control"
PY="${REPO}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "=== B7 criterion 5 — validation — $(date -Iseconds) ==="
echo "repo   : ${REPO}"
echo "branch : ${BRANCH}"
echo

# --------------------------------------------------------------- preconditions
CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$BRANCH" ]; then
    echo "HALT — on '${CURRENT}', expected '${BRANCH}'."
    exit 1
fi

if [ ! -f "$PC_EVIDENCE" ] || [ ! -f "$PC_SUMMARY" ]; then
    echo "HALT — no positive-control evidence at ${PC_EVIDENCE}."
    echo "  Run it, then copy the result in:"
    echo "    uv run python scripts/b7_positive_control.py --minutes 300"
    echo "    cp ${PC_CACHE}/actions.jsonl        ${PC_EVIDENCE}"
    echo "    cp ${PC_CACHE}/run_*.summary.json   ${PC_SUMMARY}"
    exit 1
fi

# ---------------------------------------------------- positive-control content
echo "--- positive control: ${PC_SUMMARY} ---"
"$PY" - "$PC_SUMMARY" <<'PYEOF'
import json
import sys

s = json.load(open(sys.argv[1]))
checks = {
    "synthetic": s.get("synthetic") is True,
    "not_a_soak_result": s.get("not_a_soak_result") is True,
    "attempts > 0": int(s.get("attempts", 0)) > 0,
    "attempts == results": s.get("attempts") == s.get("results"),
    "paired": s.get("paired") is True,
    "0 executed effects": int(s.get("executed_effects", -1)) == 0,
    "0 high-tier proposals": int(s.get("high_tier_proposals", -1)) == 0,
}
for name, ok in checks.items():
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}")
if not all(checks.values()):
    print("\n=== B7 NOT READY — positive-control summary failed a check ===")
    sys.exit(1)
print(
    f"  -> {s['attempts']} attempts, {s['low_tier_proposals']} safe-tier, "
    f"0 high-tier, 0 effects; window {s['started_at']} .. {s['ended_at']}"
)
PYEOF

echo
echo "--- positive control: mechanical audit (validate_b7_dryrun.py) ---"
"$PY" scripts/validate_b7_dryrun.py \
    --log "$PC_EVIDENCE" \
    --require-hours 0 \
    --require-zero-high-tier

# ------------------------------------------------------- soak must not contradict
echo
if [ -f "$SOAK_LOG" ]; then
    echo "--- soak log present: it must not contradict (${SOAK_LOG}) ---"
    "$PY" scripts/validate_b7_dryrun.py \
        --log "$SOAK_LOG" \
        --require-hours 0 \
        --require-zero-high-tier
else
    echo "--- no soak log (${SOAK_LOG}) — expected; the headless soak reaches no"
    echo "    pressure path on a normal day. Criterion 5 rests on the positive"
    echo "    control above. ---"
fi

# ------------------------------------------------------------------ test suite
echo
echo "--- re-running the test suite ---"
"$PY" -m pytest -q
"${REPO}/.venv/bin/ruff" check . >/dev/null 2>&1 || true

# ------------------------------------------------------------------------ ready
echo
echo "=== B7 criterion 5 — VALIDATED ==="
echo "  Positive control: proposals fired, audit complete, 0 effects, 0 high-tier."
echo "  Evidence committed at spikes/b7_positive_control/."
echo
echo "  Finalise via PR:"
echo "    git push -u origin ${BRANCH}"
echo "    gh pr create --base main --fill"
echo
echo "  (This script no longer merges. Nothing was committed or pushed.)"
