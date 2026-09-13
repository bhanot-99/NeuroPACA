#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S2 Project Collector Spike — Open Question 3 resolution.

Tests read-only git collection logic against real repos and verifies:
- branch, dirty count, last commit time, recent files
- .pytest_cache/v/cache/lastfailed presence and extraction
- .neuropaca/next presence (decision: explicit marker only, no heuristic)
- Read-only guarantee: git status and HEAD are unchanged before and after.

Repos to check are supplied on the command line — this script never hardcodes a
path or repo name, because its output (or a summary of it) may end up in a
public dossier. Only aggregate counts are meant to be published; real repo
paths, names, branches and file names are for the operator's own eyes only and
must not be committed (see spikes/s2_project_collector/README.md).

Usage: run_spike.py REPO_PATH [REPO_PATH ...]
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


async def _run_git_cmd(
    repo_path: Path, *args: str, timeout_seconds: float = 5.0
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="replace").strip()
            err_str = stderr.decode("utf-8", errors="replace").strip()
            return proc.returncode or 0, out_str, err_str
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, "", "timeout"


async def collect_repo_state(repo_path: Path, max_next_chars: int = 200) -> dict[str, Any] | None:
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None

    # Verify inside work tree
    rc, out, _ = await _run_git_cmd(repo_path, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out != "true":
        return None

    # 1. Branch
    rc, branch_out, _ = await _run_git_cmd(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_out if rc == 0 else "unknown"
    if branch == "HEAD":
        # Detached HEAD
        rc, commit_out, _ = await _run_git_cmd(repo_path, "rev-parse", "--short", "HEAD")
        branch = f"detached:{commit_out}" if rc == 0 else "detached"

    # 2. Dirty count
    rc, status_out, _ = await _run_git_cmd(repo_path, "status", "--porcelain")
    dirty_lines = [line for line in status_out.splitlines() if line.strip()]
    dirty_count = len(dirty_lines)

    # 3. Last commit time
    rc, log_out, _ = await _run_git_cmd(repo_path, "log", "-1", "--format=%ct")
    last_commit_ts = int(log_out) if rc == 0 and log_out.isdigit() else None
    last_commit_iso = (
        datetime.fromtimestamp(last_commit_ts, tz=UTC).isoformat()
        if last_commit_ts is not None
        else None
    )

    # 4. Most recently touched files (top 5 by mtime from tracked files)
    rc, ls_out, _ = await _run_git_cmd(repo_path, "ls-files")
    tracked_files = [line.strip() for line in ls_out.splitlines() if line.strip()]
    recent_files: list[tuple[str, float]] = []
    for rel_p in tracked_files[:500]:  # bounded check
        full_p = repo_path / rel_p
        try:
            mtime = full_p.stat().st_mtime
            recent_files.append((rel_p, mtime))
        except OSError:
            continue
    recent_files.sort(key=lambda x: x[1], reverse=True)
    top_recent = [f[0] for f in recent_files[:5]]

    # 5. .pytest_cache/v/cache/lastfailed
    lastfailed_path = repo_path / ".pytest_cache" / "v" / "cache" / "lastfailed"
    last_failing_tests: list[str] = []
    if lastfailed_path.is_file():
        try:
            data = json.loads(lastfailed_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last_failing_tests = list(data.keys())
        except Exception:
            pass

    # 6. .neuropaca/next marker
    next_path = repo_path / ".neuropaca" / "next"
    next_note: str | None = None
    if next_path.is_file():
        try:
            raw = next_path.read_text(encoding="utf-8").strip()
            first_line = raw.splitlines()[0].strip() if raw else ""
            if first_line:
                next_note = first_line[:max_next_chars]
        except Exception:
            pass

    return {
        "path": str(repo_path),
        "name": repo_path.name,
        "branch": branch,
        "dirty_count": dirty_count,
        "last_commit_timestamp": last_commit_ts,
        "last_commit_iso": last_commit_iso,
        "recent_files": top_recent,
        "last_failing_tests": last_failing_tests,
        "next_note": next_note,
    }


def _anonymize(index: int, info: dict[str, Any]) -> dict[str, Any]:
    """Strip everything that identifies a specific real repo (path, name,
    branch, file/test names) — only shape and counts are safe to publish."""
    return {
        "repo": f"repo-{index}",
        "dirty_count": info["dirty_count"],
        "has_recent_commit": info["last_commit_timestamp"] is not None,
        "recent_files_count": len(info["recent_files"]),
        "last_failing_tests_count": len(info["last_failing_tests"]),
        "has_next_note": info["next_note"] is not None,
        "read_only_verified": info["read_only_verified"],
    }


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    candidate_paths = [Path(p).expanduser() for p in sys.argv[1:]]

    results = []
    for p in candidate_paths:
        if not p.is_dir() or not (p / ".git").exists():
            continue

        # Check git status before
        _rc_before, status_before, _ = await _run_git_cmd(p, "status", "--porcelain")
        _rc_head_before, head_before, _ = await _run_git_cmd(p, "rev-parse", "HEAD")

        info = await collect_repo_state(p)

        # Check git status after (read-only verification)
        _rc_after, status_after, _ = await _run_git_cmd(p, "status", "--porcelain")
        _rc_head_after, head_after, _ = await _run_git_cmd(p, "rev-parse", "HEAD")

        assert status_before == status_after, f"Repo {p} modified during collection!"
        assert head_before == head_after, f"Repo {p} HEAD changed during collection!"

        if info:
            info["read_only_verified"] = True
            results.append(info)
            print(
                f"[{p}] branch={info['branch']} dirty={info['dirty_count']} "
                f"next_note={'yes' if info['next_note'] else 'no'}"
            )

    anonymized = [_anonymize(i, info) for i, info in enumerate(results, start=1)]
    out_dir = Path(__file__).resolve().parent.parent.parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "s2_spike_summary.json"
    out_file.write_text(json.dumps(anonymized, indent=2), encoding="utf-8")
    print(f"\nSpike complete over {len(results)} repos.")
    print(f"Anonymized aggregate written to {out_file} (data/ is gitignored — not published).")
    print("Full per-repo detail printed above only; never persisted with real identities.")


if __name__ == "__main__":
    asyncio.run(main())
