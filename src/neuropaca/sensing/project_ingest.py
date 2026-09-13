# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S2 · `ProjectIngest` — software project collector and thread context (VISION_PHASES.md).

Periodically inspects repositories under `config.watch_paths` using read-only local git
plumbing and filesystem checks:
- Branch, dirty file count, last commit time, top recently modified files
- Last failing pytest tests (`.pytest_cache/v/cache/lastfailed`)
- Explicit next step note (`.neuropaca/next`), capped at `project_next_max_chars`

Emits:
- `EpisodeKind.PROJECT_STATE_FACT` — superseding fact per repo, recorded only when state changes.
- `NodeType.PROJECT` graph nodes (`project:<slug>`), linked `PART_OF` `domain:engineering`.
- `Moment(kind="thread")` proposed when returning to a repository after `project_stale_days`.

Zero repo mutation: all operations are strictly read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment

_log = logging.getLogger(__name__)

_NUM_WORDS: dict[int, str] = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def project_slug(path: Path | str) -> str:
    """Derive a canonical node slug from a repository path or name."""
    raw = str(path).removeprefix("project:").strip()
    name = Path(raw).name or raw
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", name).strip("-").lower()
    return slug or "project"


def format_project_left_off(
    name: str,
    branch: str,
    dirty_count: int,
    last_failing_tests: list[str],
    next_note: str | None,
) -> str:
    """Format the extractive "where you left off" summary.

    Only clauses that are true get included:
    - If dirty_count > 0: includes "with N uncommitted files"
    - If last_failing_tests: includes "the last failing test was <name>"
    - If next_note: includes "your note says: <note>"
    """
    clauses: list[str] = []
    base = f"You left {name} on {branch}"
    if dirty_count > 0:
        count_str = _NUM_WORDS.get(dirty_count, str(dirty_count))
        file_str = "file" if dirty_count == 1 else "files"
        base += f" with {count_str} uncommitted {file_str}"
    clauses.append(base)

    if last_failing_tests:
        last_test = last_failing_tests[-1]
        test_name = last_test.split("::")[-1]
        clauses.append(f"the last failing test was {test_name}")

    if next_note:
        clauses.append(f"your note says: {next_note}")

    return "; ".join(clauses) + "."


class ProjectIngest(BaseModule):
    """Daemon-side project sensor inspecting git repositories under watch_paths."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        episode_store: EpisodeStore | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("project_ingest", event_bus, config)
        self._gm = graph_memory
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()
        self._poll_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

        self._last_seen_state: dict[str, tuple[Any, ...]] = {}
        self._repo_states: dict[str, dict[str, Any]] = {}
        self._last_focus_time: dict[str, datetime] = {}
        self._facts_written = 0
        self._moments_proposed = 0

    # ---------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        await self.poll_tick()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=True,
            detail=(
                f"tracked_repos={len(self._repo_states)}, "
                f"facts_written={self._facts_written}, "
                f"thread_moments={self._moments_proposed}"
            ),
        )

    # -------------------------------------------------------------- poll loop
    async def _poll_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(self.config.project_poll_interval_seconds)
                if not self.is_running:
                    break
                await self.poll_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("ProjectIngest error in poll loop")

    def find_watched_repos(self) -> list[Path]:
        """Find all git repositories located directly or 1 level under watch_paths."""
        repos: list[Path] = []
        seen: set[str] = set()
        for p_str in self.config.watch_paths:
            p = Path(p_str).expanduser().resolve()
            if not p.exists() or not p.is_dir():
                continue
            if (p / ".git").exists():
                rp_str = str(p)
                if rp_str not in seen:
                    seen.add(rp_str)
                    repos.append(p)
            else:
                try:
                    for child in p.iterdir():
                        if child.is_dir() and (child / ".git").exists():
                            child_str = str(child)
                            if child_str not in seen:
                                seen.add(child_str)
                                repos.append(child)
                except OSError:
                    continue
        return repos

    async def poll_tick(self) -> int:
        """Run a single polling cycle over all discovered repositories."""
        async with self._lock:
            repos = self.find_watched_repos()
            facts_before = self._facts_written
            now = self._clock.now()

            for repo_path in repos:
                info = await self._inspect_repo(repo_path)
                if info is None:
                    continue

                project_entity = info["project_entity"]
                state_key = (
                    info["branch"],
                    info["dirty_count"],
                    info["last_commit_timestamp"],
                    tuple(info["recent_files"]),
                    tuple(info["last_failing_tests"]),
                    info["next_note"],
                )

                if self._last_seen_state.get(project_entity) != state_key:
                    self._last_seen_state[project_entity] = state_key
                    if self._store is not None:
                        self._store.assert_fact(
                            EpisodeKind.PROJECT_STATE_FACT,
                            project_entity,
                            info["branch"],
                            valid_from=now,
                            source="project",
                            attrs={
                                "repo_path": info["path"],
                                "repo_name": info["name"],
                                "branch": info["branch"],
                                "dirty_count": info["dirty_count"],
                                "last_commit_timestamp": info["last_commit_timestamp"],
                                "recent_files": info["recent_files"],
                                "last_failing_tests": info["last_failing_tests"],
                                "next_note": info["next_note"],
                            },
                        )
                        self._facts_written += 1

                await self._gm.upsert_node(
                    project_entity,
                    NodeType.PROJECT,
                    attributes={"label": info["name"], "path": info["path"]},
                )
                if self._gm.has_node("domain:engineering"):
                    await self._gm.add_edge(
                        project_entity, "domain:engineering", relation=RelationType.PART_OF
                    )

                self._repo_states[project_entity] = info

            if self._facts_written > facts_before and self._store is not None:
                await self._store.flush()

            return len(repos)

    # ------------------------------------------------------ git repo inspection
    async def _run_git(
        self, repo_path: Path, *args: str, timeout_seconds: float = 5.0
    ) -> tuple[int, str, str]:
        """Execute a read-only git command safely bounded by a timeout."""
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
                return (
                    proc.returncode or 0,
                    stdout.decode("utf-8", errors="replace").strip(),
                    stderr.decode("utf-8", errors="replace").strip(),
                )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, "", "timeout"

    async def _inspect_repo(self, repo_path: Path) -> dict[str, Any] | None:
        """Inspect repo state using read-only plumbing and filesystem queries."""
        rc, out, _ = await self._run_git(repo_path, "rev-parse", "--is-inside-work-tree")
        if rc != 0 or out != "true":
            return None

        # 1. Branch
        rc, branch_out, _ = await self._run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_out if rc == 0 and branch_out else "unknown"
        if branch == "HEAD":
            rc_c, commit_out, _ = await self._run_git(repo_path, "rev-parse", "--short", "HEAD")
            branch = f"detached:{commit_out}" if rc_c == 0 and commit_out else "detached"

        # 2. Dirty count
        rc, status_out, _ = await self._run_git(repo_path, "status", "--porcelain")
        dirty_lines = [line for line in status_out.splitlines() if line.strip()]
        dirty_count = len(dirty_lines)

        # 3. Last commit timestamp
        rc, log_out, _ = await self._run_git(repo_path, "log", "-1", "--format=%ct")
        last_commit_ts = int(log_out) if rc == 0 and log_out.isdigit() else None

        # 4. Top recent tracked files
        rc, ls_out, _ = await self._run_git(repo_path, "ls-files")
        tracked_files = [line.strip() for line in ls_out.splitlines() if line.strip()]
        recent_files: list[tuple[str, float]] = []
        for rel_p in tracked_files[:500]:
            try:
                full_p = repo_path / rel_p
                mtime = full_p.stat().st_mtime
                recent_files.append((rel_p, mtime))
            except OSError:
                continue
        recent_files.sort(key=lambda x: x[1], reverse=True)
        top_recent = [f[0] for f in recent_files[:5]]

        # 5. Last failing tests from .pytest_cache
        lastfailed_p = repo_path / ".pytest_cache" / "v" / "cache" / "lastfailed"
        last_failing_tests: list[str] = []
        if lastfailed_p.is_file():
            try:
                data = json.loads(lastfailed_p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    last_failing_tests = list(data.keys())
            except Exception:
                pass

        # 6. Explicit next step note from .neuropaca/next
        next_p = repo_path / ".neuropaca" / "next"
        next_note: str | None = None
        if next_p.is_file():
            try:
                raw_note = next_p.read_text(encoding="utf-8").strip()
                first_line = raw_note.splitlines()[0].strip() if raw_note else ""
                if first_line:
                    next_note = first_line[: self.config.project_next_max_chars]
            except Exception:
                pass

        slug = project_slug(repo_path)
        return {
            "path": str(repo_path),
            "name": repo_path.name,
            "slug": slug,
            "project_entity": f"project:{slug}",
            "branch": branch,
            "dirty_count": dirty_count,
            "last_commit_timestamp": last_commit_ts,
            "recent_files": top_recent,
            "last_failing_tests": last_failing_tests,
            "next_note": next_note,
        }

    # ------------------------------------------------------------- focus & thread
    def _match_repo(self, payload: dict[str, Any]) -> str | None:
        """Resolve a project entity from an event payload."""
        candidates = [
            payload.get("project"),
            payload.get("project_id"),
            payload.get("repo_path"),
            payload.get("path"),
            payload.get("cwd"),
            payload.get("subject"),
            payload.get("app_id"),
        ]
        for c in candidates:
            if not isinstance(c, str) or not c:
                continue
            c_slug = project_slug(c)
            entity = f"project:{c_slug}"
            if entity in self._repo_states:
                return entity

            # Match by path prefix
            for pe, state in self._repo_states.items():
                rpath = state.get("path", "")
                if rpath and (c == rpath or c.startswith(rpath + "/")):
                    return pe
                if state.get("name", "").lower() == c.lower():
                    return pe
        return None

    async def _find_last_project_activity(
        self, project_entity: str, state: dict[str, Any] | None
    ) -> datetime | None:
        """Find the timestamp of the last known activity on this repository."""
        if self._store is not None:
            records = await self._store.for_entity(project_entity)
            if records:
                timestamps = [
                    r.t_end or r.t_start or r.t_valid or r.t_seen
                    for r in records
                    if (r.t_end or r.t_start or r.t_valid or r.t_seen) is not None
                ]
                if timestamps:
                    return max(timestamps)

        # Fall back to git last commit timestamp
        if state is not None:
            ts = state.get("last_commit_timestamp")
            if ts is not None:
                return datetime.fromtimestamp(ts, tz=UTC)
        return None

    async def on_app_switch(self, event: Event) -> None:
        """Handle app switches to detect return to a project after project_stale_days."""
        try:
            if not self.is_running:
                return
            project_entity = self._match_repo(event.payload)
            if project_entity is None:
                return

            now = self._clock.now()
            state = self._repo_states.get(project_entity)
            last_focus = self._last_focus_time.get(project_entity)
            if last_focus is None:
                last_focus = await self._find_last_project_activity(project_entity, state)

            stale_threshold = timedelta(days=self.config.project_stale_days)
            was_stale = last_focus is not None and (now - last_focus) >= stale_threshold

            self._last_focus_time[project_entity] = now

            if was_stale and state is not None:
                text = format_project_left_off(
                    name=state["name"],
                    branch=state["branch"],
                    dirty_count=state["dirty_count"],
                    last_failing_tests=state["last_failing_tests"],
                    next_note=state["next_note"],
                )
                moment = Moment(
                    kind="thread",
                    text=text,
                    evidence=(project_entity,),
                    value=0.8,
                    context={"project": project_entity, "hour": now.hour},
                    expires_at=now + timedelta(minutes=30),
                )
                self._moments_proposed += 1
                self.event_bus.publish(
                    Event(
                        event_type=EventType.MOMENT_PROPOSED,
                        source="project_ingest",
                        payload={"moment": moment},
                    )
                )
        except Exception:
            _log.exception("ProjectIngest error on app_switch")

    # ---------------------------------------------------------------- forget
    async def forget(self, project: str | Path) -> int:
        """Scrub project facts, episodes, graph node, and in-memory caches."""
        async with self._lock:
            slug = project_slug(project)
            project_entity = f"project:{slug}"
            removed = 0
            if self._store is not None:
                removed += await self._store.forget(project_entity)
                removed += await self._store.forget(str(project))
            if self._gm.has_node(project_entity):
                await self._gm.delete_node(project_entity)

            self._last_seen_state.pop(project_entity, None)
            self._repo_states.pop(project_entity, None)
            self._last_focus_time.pop(project_entity, None)
            return removed
