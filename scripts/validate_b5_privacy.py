#!/usr/bin/env python3
"""B5 · Exit Criterion 3 — conversation history is RAM-only (phases.md B5, rules.md §6).

`scripts/validate_b5_latency.py` fires a `$ <canary>` query at the daemon. That
text lives only in `InterfaceLayer._conversation_history` (a `list[Message]` in
RAM) and every IPC log line is `redact()`-ed. This script proves it never
reached disk: it scans every file the daemon writes under `--data-dir` (plus any
explicit `--log`) and asserts the canary — and, as a coarser net, raw
conversation history — is absent.

    uv run python scripts/validate_b5_privacy.py --data-dir <dir> --canary CANARY-...

Exit 0 = clean. Exit 1 = the canary (or raw history) is on disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_KNOWN = ("graph.json", "actions.jsonl", "neuropaca.log")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--canary", required=True)
    ap.add_argument("--log", action="append", default=[], help="extra log path(s) to scan")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    targets: list[Path] = [data_dir / name for name in _KNOWN]
    targets += [Path(p) for p in args.log]
    # also sweep anything else the daemon dropped in the dir (temp files, rotations)
    if data_dir.is_dir():
        targets += [p for p in data_dir.rglob("*") if p.is_file() and p.suffix != ".sock"]

    scanned: list[str] = []
    hits: list[str] = []
    for path in dict.fromkeys(targets):  # dedupe, keep order
        if not path.is_file():
            continue
        blob = path.read_bytes()
        scanned.append(f"{path}  ({len(blob)} B)")
        if args.canary.encode() in blob:
            hits.append(f"{path}: contains the canary {args.canary!r}")
        if b"conversation_history" in blob:
            hits.append(f"{path}: contains raw conversation history")

    print("scanned:")
    for s in scanned:
        print(f"  {s}")
    if not scanned:
        print(f"\nFAIL — nothing to scan under {data_dir} (did step 2 run?)", file=sys.stderr)
        return 1

    if hits:
        print("\n=== RESULT (FAIL) ===")
        for h in hits:
            print(f"  {h}")
        return 1
    print(f"\n=== RESULT (PASS) — canary {args.canary} absent from every file on disk ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
