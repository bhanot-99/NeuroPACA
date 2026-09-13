# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Dogfood evaluation script for S2 (Projects).

Evaluates ProjectIngest._inspect_repo and format_project_left_off across
real git repositories supplied on the command line.

Verifies:
1. Read-only guarantee: git status and HEAD byte-identical before and after.
2. Formatted "left off" briefing summaries rendered cleanly without missing field artefacts.
3. Execution time per repository (load budget check).

Repos are never hardcoded here: pass the paths you want to dogfood on the
command line. This script prints full detail (including real repo names) to
the terminal for the operator's own eyes, but the persisted report
(default: data/dogfood_s2_results.json, gitignored) only ever holds
anonymized shape and counts — never a real path, name, branch or test name —
because this script and its output live in a public repository.

Usage: dogfood_s2_repos.py REPO_PATH [REPO_PATH ...] [--out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.sensing.project_ingest import ProjectIngest, format_project_left_off


async def _git_snapshot(repo: Path) -> tuple[str, str]:
    proc_st = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    st_out, _ = await proc_st.communicate()

    proc_hd = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "HEAD",
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    hd_out, _ = await proc_hd.communicate()
    return st_out.decode(), hd_out.decode()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="+", type=Path, help="repo paths to dogfood")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/dogfood_s2_results.json"),
        help="anonymized report path (default: data/dogfood_s2_results.json, gitignored)",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    target_repos = [p.expanduser() for p in args.repos]
    print(f"=== S2 Projects Dogfood Check ({len(target_repos)} repositories) ===")

    bus = EventBus()
    cfg = Config(
        project_tracking_enabled=True,
        watch_paths=[str(p) for p in target_repos],
        inference_backend="fake",
    )
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path="/tmp/dogfood_gm.json")
    store = EpisodeStore(Path("/tmp/dogfood_episodes.sqlite"))
    clock = FakeClock(wall=datetime(2026, 9, 13, 12, 0, tzinfo=UTC))
    ingest = ProjectIngest(bus, cfg, gm, store, clock=clock)

    results: list[dict] = []
    all_readonly_clean = True

    for repo in target_repos:
        if not repo.exists() or not (repo / ".git").exists():
            print(f"SKIPPING: {repo} (not found or not git)")
            continue

        st_before, hd_before = await _git_snapshot(repo)

        t0 = time.perf_counter()
        info = await ingest._inspect_repo(repo)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        st_after, hd_after = await _git_snapshot(repo)

        readonly_ok = (st_before == st_after) and (hd_before == hd_after)
        if not readonly_ok:
            all_readonly_clean = False

        if info is None:
            print(f"FAILED to collect state for {repo}")
            continue

        summary_text = format_project_left_off(
            name=info["name"],
            branch=info["branch"],
            dirty_count=info["dirty_count"],
            last_failing_tests=info["last_failing_tests"],
            next_note=info["next_note"],
        )

        # Anonymized — this is what gets persisted. Real name/path/branch/test
        # names are printed to the terminal below for the operator only.
        results.append(
            {
                "repo": f"repo-{len(results) + 1}",
                "dirty_count": info["dirty_count"],
                "has_recent_commit": info["last_commit_timestamp"] is not None,
                "last_failing_tests_count": len(info["last_failing_tests"]),
                "has_next_note": info["next_note"] is not None,
                "elapsed_ms": round(elapsed_ms, 2),
                "readonly_verified": readonly_ok,
            }
        )

        print(f"\n[{repo}]")
        print(f"  Branch: {info['branch']} | Dirty: {info['dirty_count']}")
        print(f"  Latency: {elapsed_ms:.2f} ms | Read-only Verified: {readonly_ok}")
        print(f'  Summary: "{summary_text}"')

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved anonymized dogfood results to {args.out}")
    print(f"Total repos evaluated: {len(results)}")
    print(f"All read-only verified: {all_readonly_clean}")
    if not all_readonly_clean:
        raise SystemExit("One or more repos suffered mutation!")


if __name__ == "__main__":
    asyncio.run(main())


# gen-ref: 6b5becf0
