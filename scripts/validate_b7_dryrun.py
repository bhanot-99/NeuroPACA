#!/usr/bin/env python3
"""B7 · Exit Criterion 5 — the dry-run review period (phases.md B7, D-14).

The last B7 criterion is not a test, it is a *period*: "a review period in
dry-run with zero false positives before any tier goes live." This script is how
that period is measured. It reads the daemon's real audit log — the one written
while you used the machine normally with `action_dry_run = True` — and reports
what the action layer *would* have done.

    uv run python scripts/validate_b7_dryrun.py [--log data/actions.jsonl] [--days 7]

A **false positive** is any dry-run attempt you would not have wanted:

  * an autonomous (`trigger` starting `pressure:`) attempt against a node you
    consider irrelevant, or
  * a `pressure:high` prompt you would have dismissed.

The script cannot judge that for you — it lists every autonomous attempt with its
reason and its pressure so you can. What it *does* check mechanically:

  * every attempt has a matching result line (the audit is complete);
  * **nothing executed** during the review period — a live effect while
    `action_dry_run` is on would invalidate the whole review;
  * no dangerous action reached execution without a recorded confirmation;
  * a per-day rate, so "quiet" is distinguishable from "never ran".

Exit 0 = the log is consistent and effect-free. Exit 1 = a mechanical violation.
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
    span_days = max((datetime.fromisoformat(last) - datetime.fromisoformat(first)).days, 1)

    print(f"log             : {args.log}")
    print(f"window          : {first} .. {last}  ({span_days} day(s))")
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

    if autonomous:
        print("\nautonomous proposals — judge these for false positives:")
        for attempt in autonomous:
            print(
                f"  {attempt.get('ts', '')[:19]}  {attempt.get('tier', '?'):<9} "
                f"{attempt.get('action', '?'):<13} {attempt.get('trigger', '')}"
            )
            print(f"      reason: {attempt.get('reason', '')}")

    fails: list[str] = []
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
        f"=== RESULT (PASS) — {len(attempts)} attempts, {len(attempts)}/{len(results)} "
        f"attempt+result pairs, 0 effects. Review the "
        f"{len(autonomous)} autonomous proposal(s) above and sign off the criterion ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
