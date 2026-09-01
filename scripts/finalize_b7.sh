#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — unattended finalisation (phases.md B7, D-14).
#
# Run this after the 24 h dry-run soak window closes (~2026-09-02 16:50 IST).
# It validates the soak, and ONLY if the validation passes does it record the
# result, commit, and merge b7-drive-action-l5-l7 into main.
#
#   ./scripts/finalize_b7.sh
#
# `set -e` throughout: any failure halts before the merge. The gate is
# deliberately strict — it refuses to merge on anything it cannot verify
# mechanically:
#
#   * the log must span >= 24 h of real usage;
#   * every attempt must have a matching result line (complete audit);
#   * nothing may have executed (a live effect during dry-run invalidates it);
#   * ZERO high-tier proposals. A high-tier proposal is exactly the thing a
#     human must judge for false positives, and an unattended script cannot do
#     that. If one exists, this halts and hands the judgement back to you —
#     that is a correct outcome, not a bug.
#
# Nothing is pushed. `main` stays local until you review the merge.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
BRANCH="b7-drive-action-l5-l7"
LOG="data/actions.jsonl"
PY="${REPO}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "=== B7 finalisation — $(date -Iseconds) ==="
echo "repo   : ${REPO}"
echo "branch : ${BRANCH}"
echo

# --------------------------------------------------------------- preconditions
if [ ! -f "$LOG" ]; then
    echo "HALT — no ${LOG}."
    echo "  The soak produced no action attempts at all. That is not a pass: the"
    echo "  criterion cannot be signed off on an empty log. Check the daemon is"
    echo "  still up (\`neuropaca health\`) and give it more real usage."
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "HALT — the working tree is dirty. Commit or stash before finalising:"
    git status --short
    exit 1
fi

CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$BRANCH" ]; then
    echo "HALT — on '${CURRENT}', expected '${BRANCH}'."
    exit 1
fi

# ------------------------------------------------------------------ validation
echo "--- validating the soak ---"
if ! "$PY" scripts/validate_b7_dryrun.py \
        --log "$LOG" \
        --require-hours 24 \
        --require-zero-high-tier; then
    echo
    echo "=== B7 NOT FINALISED — the soak did not pass ==="
    echo "  Nothing was committed and nothing was merged."
    echo "  Read the output above. If it halted on high-tier proposals, judge them:"
    echo "    - every one you WANTED  -> re-run without --require-zero-high-tier,"
    echo "      then finish by hand and record your verdict in memory.md;"
    echo "    - any FALSE POSITIVE    -> that is a real finding about the L5"
    echo "      corroboration gate. Do not merge; tune first."
    exit 1
fi

# The suite must still be green — a passing soak on broken code is not a pass.
echo
echo "--- re-running the test suite ---"
"$PY" -m pytest -q
"${REPO}/.venv/bin/ruff" check . >/dev/null 2>&1 || true

# ------------------------------------------------------------ record the result
echo
echo "--- recording the result ---"
SOAK_SUMMARY="$("$PY" scripts/validate_b7_dryrun.py --log "$LOG" --require-hours 24 | tail -4 | tr '\n' ' ')"
export SOAK_SUMMARY

"$PY" - <<'PYEOF'
import os
import pathlib
import subprocess
from datetime import datetime

summary = " ".join(os.environ.get("SOAK_SUMMARY", "").split())
today = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
count = subprocess.run(
    ["wc", "-l", "data/actions.jsonl"], capture_output=True, text=True
).stdout.split()[0]

# ---- memory.md
p = pathlib.Path("memory.md")
s = p.read_text()
s = s.replace(
    "| **Phase** | **B7 · Drive & Action (L5 + L7)** — 🟡 built",
    "| **Phase** | **B7 · Drive & Action (L5 + L7)** — ✅ merged",
    1,
)
old_next = [line for line in s.splitlines() if line.startswith("| **Next** |")][0]
s = s.replace(
    old_next,
    "| **Next** | **B8 · Agents & structural plasticity (L8)** — unblocked by D-15 "
    "(`AgentSupervisor`, `spawn_node`/`kill_node`, apoptosis at `idle_ttl = 14 d`). |",
    1,
)
s = s.replace(
    '--> B7["B7 🟡 built"] --> B8["B8 ⬜"]',
    '--> B7["B7 ✅ merged"] --> B8["B8 ⬜"]',
    1,
)
marker = "  - **Analyse with:**"
insert = (
    f"  - **RESULT — criterion 5 PASSED, {today}.** 24 h dry-run soak on the target box: "
    f"{count} audit lines, every attempt paired with its result, **0 effects** (dry-run held), "
    f"and **0 high-tier proposals** — so there was nothing needing a false-positive verdict and "
    f"the zero-false-positive rule is satisfied mechanically, not by assertion. "
    f"Safe-tier proposals are listed in `data/soak_b7_eval.log` with the L5 reason string each "
    f"traces to. Validator tail: `{summary}`. Finalised unattended by `scripts/finalize_b7.sh`; "
    f"**B7 merged to main.**\n"
)
s = s.replace(marker, insert + marker, 1)
p.write_text(s)

# ---- phases.md
p = pathlib.Path("phases.md")
s = p.read_text()
s = s.replace("| 🟡 | a review period in dry-run with zero false positives", "| ✅ | a review period in dry-run with zero false positives", 1)
s = s.replace(
    "Analysed by `scripts/validate_b7_dryrun.py --require-hours 24`, which gates on window "
    "length and splits high- from safe-tier proposals for judgement. |",
    "Analysed by `scripts/validate_b7_dryrun.py --require-hours 24`, which gates on window "
    f"length and splits high- from safe-tier proposals for judgement. **PASSED {today}: "
    f"{count} audit lines, 0 effects, 0 high-tier proposals** (nothing to judge ⇒ zero false "
    "positives mechanically). |",
    1,
)
s = s.replace(
    "| B7 | Drive & Action (L5 + L7) | 🟡 built",
    "| B7 | Drive & Action (L5 + L7) | ✅ merged",
    1,
)
s = s.replace(
    "**Exit criteria 1–4 validated on the target box**; criterion 5 = the 24 h dry-run soak, "
    "running since 2026-09-01 16:48 IST.",
    "**All 5 exit criteria met** — 1–4 validated on the target box, criterion 5 by the 24 h "
    f"dry-run soak ({today}).",
    1,
)
s = s.replace("| B8–B9 | Agents & structural plasticity → Hardening | ⬜ not started |",
              "| B8 | Agents & structural plasticity (L8) | ⬜ not started — unblocked by D-15 |\n"
              "| B9 | Hardening | ⬜ not started |", 1)
p.write_text(s)
print("docs updated")
PYEOF

# ------------------------------------------------------------- commit and merge
echo
echo "--- committing ---"
git add memory.md phases.md
git commit -m "feat(L7): pass 24h dry-run and finalize B7"

echo
echo "--- merging into main ---"
git checkout main
git merge --no-ff "$BRANCH" -m "Merge branch '${BRANCH}': B7 · Drive & Action (L5 + L7)"

echo
echo "=== B7 FINALISED ==="
git log --oneline -3
echo
echo "Merged locally; nothing pushed. Review, then: git push origin main"
echo "The soak daemon is still running — stop it with: pkill -f neuropacad"
