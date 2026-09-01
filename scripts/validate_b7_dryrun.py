#!/usr/bin/env python3
"""B7 · Exit Criterion 5 — the dry-run review period (phases.md B7, D-14).

The last B7 criterion is not a test, it is a *period*: "a review period in
dry-run with zero false positives before any tier goes live." This script is how
that period is measured. It reads the daemon's real audit log — the one written
while you used the machine normally with `action_dry_run = True` — and reports
what the action layer *would* have done.

    uv run python scripts/validate_b7_dryrun.py [--log data/actions.jsonl] \
        [--require-hours 24] [--days 7]

**Acceptance (agreed 2026-09-01):** a 24 h soak under `neuropaca.soak.toml`
(`action_dry_run = true`, *both* tiers enabled), running the normal daily
desktop workflow, with **zero false positives among high-tier proposals**, and
every safe-tier proposal logically traceable to the diagnosis spike underneath
it.

A **false positive** is any dry-run attempt you would not have wanted:

  * a `pressure:high` prompt you would have dismissed (**these are the ones that
    must be zero** — the high tier is what gates dangerous actions), or
  * a `pressure:low` write whose reason does not map to a real spike.

The script cannot judge that for you — it lists high-tier proposals separately
from safe-tier ones, each with the reason string L5 carried, so you can. What it
*does* check mechanically:

  * the window is at least `--require-hours` long (default 24) — an eight-hour
    log cannot satisfy a 24 h criterion however clean it is;
  * every attempt has a matching result line (the audit is complete);
  * **nothing executed** during the review period — a live effect while
    `action_dry_run` is on would invalidate the whole review;
  * no dangerous action reached execution without a recorded confirmation;
  * a per-day rate, so "quiet" is distinguishable from "never ran".

Exit 0 = the log is mechanically sound and long enough. Exit 1 = a violation.
Reading the listed proposals and declaring zero false positives is yours.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

_DEFAULT_LOG = "data/actions.jsonl"


def _load(path: Path, since: datetime | None) -> list[dict]:
    if not path.exists():
        print(f"no audit log at {path} — has the daemon run with actions enabled?")
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            print(f"  WARN — unparseable audit line skipped: {line[:80]}")
            continue
        if since is not None:
            try:
                if datetime.fromisoformat(str(record["ts"])) < since:
                    continue
            except (KeyError, ValueError):
                pass
        records.append(record)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=_DEFAULT_LOG)
    parser.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    parser.add_argument(
        "--require-hours",
        type=float,
        default=24.0,
        help="minimum window the log must span (0 = no duration requirement)",
    )
    args = parser.parse_args(argv)

    since = datetime.now(UTC) - timedelta(days=args.days) if args.days else None
    records = _load(Path(args.log), since)
    if not records:
        print("=== RESULT (INCONCLUSIVE) — no audit records in range ===")
        return 1

    attempts = [r for r in records if r.get("phase") == "attempt"]
    results = [r for r in records if r.get("phase") == "result"]
    result_ids = {r.get("request_id") for r in results}
    executed = [r for r in results if r.get("ok") and not r.get("dry_run")]
    dry_runs = [r for r in results if r.get("dry_run")]
    refused = [r for r in results if not r.get("ok")]
    autonomous = [a for a in attempts if str(a.get("trigger", "")).startswith("pressure:")]
    user_driven = [a for a in attempts if str(a.get("trigger", "")).startswith("user:")]

    first = min(str(r.get("ts", "")) for r in records)
    last = max(str(r.get("ts", "")) for r in records)
    span_hours = (
        datetime.fromisoformat(last) - datetime.fromisoformat(first)
    ).total_seconds() / 3600
    span_days = max(span_hours / 24, 1 / 24)

    print(f"log             : {args.log}")
    print(f"window          : {first} .. {last}  ({span_hours:.1f} h)")
    print(
        f"attempts        : {len(attempts)}  ({len(autonomous)} autonomous, "
        f"{len(user_driven)} user-driven)"
    )
    print(f"                : {len(autonomous) / span_days:.1f} autonomous/day")
    print(f"dry-run results : {len(dry_runs)}")
    print(f"executed        : {len(executed)}")
    print(f"refused         : {len(refused)}")
    print(f"by action       : {dict(Counter(a.get('action', '?') for a in attempts))}")
    print(f"by tier         : {dict(Counter(a.get('tier', '?') for a in attempts))}")

    high = [a for a in autonomous if str(a.get("trigger", "")).startswith("pressure:high")]
    low = [a for a in autonomous if str(a.get("trigger", "")).startswith("pressure:low")]

    print("\nHIGH-TIER proposals — these must be ZERO false positives:")
    if not high:
        print("  (none — the corroboration gate never opened during the window)")
    for attempt in high:
        print(
            f"  {attempt.get('ts', '')[:19]}  {attempt.get('action', '?'):<13} "
            f"{attempt.get('trigger', '')}"
        )
        print(f"      reason: {attempt.get('reason', '')}")
        print("      verdict: [ ] wanted   [ ] FALSE POSITIVE")

    print("\nsafe-tier proposals — each must map to a real diagnosis spike:")
    if not low:
        print("  (none)")
    for attempt in low[:50]:
        print(
            f"  {attempt.get('ts', '')[:19]}  {attempt.get('action', '?'):<13} "
            f"{attempt.get('trigger', '')}"
        )
        print(f"      reason: {attempt.get('reason', '')}")
    if len(low) > 50:
        print(f"  … and {len(low) - 50} more (all in {args.log})")

    fails: list[str] = []
    if args.require_hours and span_hours < args.require_hours:
        fails.append(
            f"window is {span_hours:.1f} h, criterion needs >= {args.require_hours:.0f} h "
            "of real usage"
        )
    for attempt in attempts:
        if attempt.get("request_id") and attempt["request_id"] not in result_ids:
            fails.append(f"attempt {attempt['request_id']} has no result line (incomplete audit)")
    if dry_runs and executed:
        fails.append(
            f"{len(executed)} action(s) executed during a dry-run review period — "
            "the review is invalid until the log is clean"
        )
    for record in executed:
        if record.get("tier") == "dangerous" and record.get("confirmed") is not True:
            fails.append(
                f"dangerous {record.get('action')} executed without a recorded confirmation"
            )

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(
        f"=== RESULT (PASS, mechanical) — {span_hours:.1f} h window, {len(attempts)} attempts, "
        f"{len(attempts)}/{len(results)} attempt+result pairs, 0 effects.\n"
        f"    Now judge {len(high)} high-tier and {len(low)} safe-tier proposal(s) above. "
        "Zero high-tier false positives = criterion 5 signed off ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
