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
- `Moment(kind="thread")` proposed when a repo whose last commit is older than
  `project_stale_days` picks up uncommitted changes again (see `_maybe_fire_thread_moment`).

Zero repo mutation: all operations are strictly read-only.

**Why the thread moment is triggered from git state, not `APP_SWITCH`.**
VISION.md §1.4 ("the thread") wants this to fire when you *return* to a stale
project. The obvious trigger would be the focused window changing to that
project — but nothing in this codebase can make that correlation: `WindowInfo`
(`sensing/activity/window.py`) carries only `app_id`/`title`, no PID and no cwd
(the Wayland toplevel protocols this daemon binds don't expose either), and the
raw title never leaves the sensing collector onto the bus at all (B14's
membrane — `APP_SWITCH`'s payload is `app_id`/`webapp`/`webapp_domain` only, see
`sensing/activity/collector.py::_on_window_switch`). Matching an `app_id` like
`"code"` against a repo path is therefore never more than a name coincidence.
Instead, this module fires straight from what it already measures accurately:
a repo's own last-commit timestamp is real, immutable ground truth for
staleness (unlike this module's own poll-cadence bookkeeping, which would
just measure "since the daemon last restarted"), and uncommitted changes
appearing after a stale gap is a fact, not a guess, that you are back working
in it.
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
from neuropaca.core.project_format import format_project_left_off

_log = logging.getLogger(__name__)

__all__ = ["ProjectIngest", "format_project_left_off", "project_slug"]


def project_slug(path: Path | str) -> str:
    """Derive a canonical node slug from a repository path or name."""
    raw = str(path).removeprefix("project:").strip()
    name = Path(raw).name or raw
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", name).strip("-").lower()
    return slug or "project"


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
        self._thread_fired: set[str] = set()
        self._facts_written = 0
        self._moments_proposed = 0

    # ---------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        pass

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

                # V-10: a poll is a *sighting*, not an access — `mark_seen` (or,
                # for a brand-new node, a plain create) must not bump
                # `activity`/`access_count`/`relevance_score` the way
                # `upsert_node` does on every call to an existing node
                # (GraphMemory._touch_unsafe). Polling every repo under
                # `watch_paths` every `project_poll_interval_seconds` must not
                # by itself make a repo look more "important" than one the
                # user actually opens.
                if not self._gm.has_node(project_entity):
                    await self._gm.upsert_node(
                        project_entity,
                        NodeType.PROJECT,
                        attributes={"label": info["name"], "path": info["path"]},
                    )
                else:
                    await self._gm.mark_seen(project_entity, now)
                    await self._gm.update_node(
                        project_entity, {"label": info["name"], "path": info["path"]}
                    )
                if self._gm.has_node("domain:engineering"):
                    await self._gm.add_edge(
                        project_entity, "domain:engineering", relation=RelationType.PART_OF
                    )

                prev_info = self._repo_states.get(project_entity)
                self._repo_states[project_entity] = info
                await self._maybe_fire_thread_moment(project_entity, prev_info, info, now)

            if self._facts_written > facts_before and self._store is not None:
                await self._store.flush()

            return len(repos)

    async def _maybe_fire_thread_moment(
        self,
        project_entity: str,
        prev_info: dict[str, Any] | None,
        info: dict[str, Any],
        now: datetime,
    ) -> None:
        """The thread moment (VISION.md §1.4): fire once when a repo whose last
        commit is older than `project_stale_days` picks up its first
        uncommitted change since we last looked — real git ground truth, no
        window/focus correlation (see the module docstring for why)."""
        last_commit_ts = info["last_commit_timestamp"]
        was_stale = last_commit_ts is not None and (
            now - datetime.fromtimestamp(last_commit_ts, tz=UTC)
        ) >= timedelta(days=self.config.project_stale_days)

        prev_dirty = prev_info["dirty_count"] if prev_info is not None else 0
        became_active = info["dirty_count"] > 0 and prev_dirty == 0

        if was_stale and became_active and project_entity not in self._thread_fired:
            text = format_project_left_off(
                name=info["name"],
                branch=info["branch"],
                dirty_count=info["dirty_count"],
                last_failing_tests=info["last_failing_tests"],
                next_note=info["next_note"],
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
            self._thread_fired.add(project_entity)

        if info["dirty_count"] == 0:
            # A commit landed (or changes were discarded) — the next stale ->
            # dirty transition is a new "return" and may fire again.
            self._thread_fired.discard(project_entity)

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
            self._thread_fired.discard(project_entity)
            return removed


# gen-ref: 86c879b5
