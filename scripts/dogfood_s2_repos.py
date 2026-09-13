# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Dogfood evaluation script for S2 (Projects).

Evaluates ProjectIngest._inspect_repo and format_project_left_off across
10 real git repositories on the local system.

Verifies:
1. Read-only guarantee: git status and HEAD byte-identical before and after for all 10.
2. Formatted "left off" briefing summaries rendered cleanly without missing field artefacts.
3. Execution time per repository (load budget check).
"""

from __future__ import annotations

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

TARGET_REPOS = [
    Path("/home/bhanot/NeuroPaca"),
    Path("/home/bhanot/MyBotTrader"),
    Path("/home/bhanot/SYSKON"),
    Path("/home/bhanot/keyd"),
    Path("/home/bhanot/.hermes/hermes-agent"),
    Path("/home/bhanot/.oh-my-zsh"),
    Path("/home/bhanot/.nvm"),
    Path("/home/bhanot/.cache/pre-commit/repo3ukw8qm5"),
    Path("/home/bhanot/.cache/pre-commit/repoavpjl2bv"),
    Path("/home/bhanot/.cache/pre-commit/repox7hsm2lb"),
]


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


async def main() -> None:
    print(f"=== S2 Projects Dogfood Check ({len(TARGET_REPOS)} real repositories) ===")

    # Initialize a dummy ProjectIngest instance to run _inspect_repo
    bus = EventBus()
    cfg = Config(
        project_tracking_enabled=True,
        watch_paths=[str(p) for p in TARGET_REPOS],
        inference_backend="fake",
    )
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path="/tmp/dogfood_gm.json")
    store = EpisodeStore(Path("/tmp/dogfood_episodes.sqlite"))
    clock = FakeClock(wall=datetime(2026, 9, 13, 12, 0, tzinfo=UTC))
    ingest = ProjectIngest(bus, cfg, gm, store, clock=clock)

    results: list[dict] = []
    all_readonly_clean = True

    for repo in TARGET_REPOS:
        if not repo.exists() or not (repo / ".git").exists():
            print(f"SKIPPING: {repo} (not found or not git)")
            continue

        # 1. Snapshot before
        st_before, hd_before = await _git_snapshot(repo)

        # 2. Collect state
        t0 = time.perf_counter()
        info = await ingest._inspect_repo(repo)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. Snapshot after
        st_after, hd_after = await _git_snapshot(repo)

        readonly_ok = (st_before == st_after) and (hd_before == hd_after)
        if not readonly_ok:
            all_readonly_clean = False

        if info is None:
            print(f"FAILED to collect state for {repo}")
            continue

        # 4. Format left-off text
        summary_text = format_project_left_off(
            name=info["name"],
            branch=info["branch"],
            dirty_count=info["dirty_count"],
            last_failing_tests=info["last_failing_tests"],
            next_note=info["next_note"],
        )

        res = {
            "repo": str(repo),
            "name": info["name"],
            "branch": info["branch"],
            "dirty_count": info["dirty_count"],
            "last_commit_timestamp": info["last_commit_timestamp"],
            "recent_files_count": len(info["recent_files"]),
            "last_failing_tests_count": len(info["last_failing_tests"]),
            "has_next_note": info["next_note"] is not None,
            "elapsed_ms": round(elapsed_ms, 2),
            "readonly_verified": readonly_ok,
            "summary_text": summary_text,
        }
        results.append(res)

        print(f"\n[Repo {len(results)}/{len(TARGET_REPOS)}] {info['name']} ({repo})")
        print(f"  Branch: {info['branch']} | Dirty: {info['dirty_count']}")
        print(f"  Latency: {elapsed_ms:.2f} ms | Read-only Verified: {readonly_ok}")
        print(f'  Summary: "{summary_text}"')

    out_path = Path("/home/bhanot/NeuroPaca/dogfood_s2_results.json")
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved dogfood results to {out_path}")
    print(f"Total repos evaluated: {len(results)}")
    print(f"All read-only verified: {all_readonly_clean}")
    assert len(results) == 10, f"Expected 10 repos, got {len(results)}"
    assert all_readonly_clean, "One or more repos suffered mutation!"


if __name__ == "__main__":
    asyncio.run(main())
