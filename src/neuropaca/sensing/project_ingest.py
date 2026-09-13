# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S2/S4 · `ProjectIngest` & `ProjectPlugin` — software project collector and thread context.

Migrated under S4 Plugin Contract:
- `ProjectPlugin` implements the pure `Plugin` reader protocol.
- `ProjectIngest` hosts `ProjectPlugin` via `PluginHost`.

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
from neuropaca.core.enums import EpisodeKind, EventType, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment
from neuropaca.core.project_format import format_project_left_off
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginHost,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)

__all__ = ["ProjectIngest", "ProjectPlugin", "format_project_left_off", "project_slug"]


def project_slug(path: Path | str) -> str:
    """Derive a canonical node slug from a repository path or name."""
    raw = str(path).removeprefix("project:").strip()
    name = Path(raw).name or raw
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", name).strip("-").lower()
    return slug or "project"


class ProjectPlugin:
    """Read-only plugin reader inspecting git repositories under watch_paths."""

    def __init__(
        self,
        config: Config,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self._clock: Clock = clock or SystemClock()
        self.watch_paths: list[str] = list(config.watch_paths)
        self._last_seen_state: dict[str, tuple[Any, ...]] = {}
        self._repo_states: dict[str, dict[str, Any]] = {}
        self._thread_fired: set[str] = set()
        self._entities: set[str] = set()
        self._moments_proposed = 0

    def describe(self) -> PluginDescriptor:
        allowed = tuple(Path(p).expanduser().resolve() for p in self.config.watch_paths)
        return PluginDescriptor(
            name="project",
            node_type=NodeType.PROJECT,
            domain_hub="domain:engineering",
            poll_interval_seconds=self.config.project_poll_interval_seconds,
            span_kind=None,
            manifest=PluginManifest(
                allowed_read_paths=allowed,
                allow_network=False,
                allow_subprocesses=True,
            ),
            entity_prefixes=("project:",),
        )

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

    def _maybe_create_thread_moment(
        self,
        project_entity: str,
        prev_info: dict[str, Any] | None,
        info: dict[str, Any],
        now: datetime,
    ) -> Event | None:
        last_commit_ts = info["last_commit_timestamp"]
        was_stale = last_commit_ts is not None and (
            now - datetime.fromtimestamp(last_commit_ts, tz=UTC)
        ) >= timedelta(days=self.config.project_stale_days)

        prev_dirty = prev_info["dirty_count"] if prev_info is not None else 0
        became_active = info["dirty_count"] > 0 and prev_dirty == 0

        event: Event | None = None
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
            event = Event(
                event_type=EventType.MOMENT_PROPOSED,
                source="project_ingest",
                payload={"moment": moment},
            )
            self._thread_fired.add(project_entity)

        if info["dirty_count"] == 0:
            self._thread_fired.discard(project_entity)

        return event

    async def items(self, since: datetime) -> list[PluginItem]:
        now = self._clock.now()
        repos = self.find_watched_repos()
        result: list[PluginItem] = []

        for repo_path in repos:
            info = await self._inspect_repo(repo_path)
            if info is None:
                continue

            project_entity = info["project_entity"]
            self._entities.add(project_entity)
            state_key = (
                info["branch"],
                info["dirty_count"],
                info["last_commit_timestamp"],
                tuple(info["recent_files"]),
                tuple(info["last_failing_tests"]),
                info["next_note"],
            )
            self._last_seen_state[project_entity] = state_key

            prev_info = self._repo_states.get(project_entity)
            self._repo_states[project_entity] = info

            events: list[Event] = []
            ev = self._maybe_create_thread_moment(project_entity, prev_info, info, now)
            if ev is not None:
                events.append(ev)

            result.append(
                PluginItem(
                    entity_id=project_entity,
                    label=info["name"],
                    node_type=NodeType.PROJECT,
                    node_attributes={"label": info["name"], "path": info["path"]},
                    fact=(
                        EpisodeKind.PROJECT_STATE_FACT,
                        info["branch"],
                        {
                            "repo_path": info["path"],
                            "repo_name": info["name"],
                            "branch": info["branch"],
                            "dirty_count": info["dirty_count"],
                            "last_commit_timestamp": info["last_commit_timestamp"],
                            "recent_files": info["recent_files"],
                            "last_failing_tests": info["last_failing_tests"],
                            "next_note": info["next_note"],
                        },
                    ),
                    state_key=state_key,
                    events=tuple(events),
                )
            )

        return result

    @property
    def repo_states(self) -> dict[str, dict[str, Any]]:
        return dict(self._repo_states)

    @property
    def thread_fired(self) -> set[str]:
        return set(self._thread_fired)

    @property
    def last_seen_state(self) -> dict[str, tuple[Any, ...]]:
        return dict(self._last_seen_state)

    @property
    def moments_proposed(self) -> int:
        return self._moments_proposed

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    def owns_entity(self, entity: str) -> bool:
        slug = project_slug(entity)
        return (
            entity in self._entities
            or f"project:{slug}" in self._entities
            or str(entity) in self._repo_states
            or f"project:{slug}" in self._repo_states
            or entity.startswith("project:")
        )

    async def forget(self, entity: str) -> int:
        if not self.owns_entity(entity):
            return 0
        slug = project_slug(entity)
        project_entity = f"project:{slug}"
        was_tracked = (
            project_entity in self._entities
            or str(entity) in self._entities
            or project_entity in self._repo_states
            or str(entity) in self._repo_states
        )
        self._entities.discard(project_entity)
        self._entities.discard(str(entity))
        self._last_seen_state.pop(project_entity, None)
        self._last_seen_state.pop(str(entity), None)
        self._repo_states.pop(project_entity, None)
        self._repo_states.pop(str(entity), None)
        self._thread_fired.discard(project_entity)
        self._thread_fired.discard(str(entity))
        return 1 if was_tracked else 0


class ProjectIngest(BaseModule):
    """Daemon-side project sensor inspecting git repositories under watch_paths.

    Implemented using PluginHost and ProjectPlugin under the S4 plugin contract.
    """

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
        self._plugin = ProjectPlugin(config=config, clock=self._clock)
        self._host = PluginHost(
            event_bus,
            config,
            graph_memory,
            episode_store,
            plugins=[self._plugin],
            clock=self._clock,
        )
        self._lock = asyncio.Lock()
        self._last_seen_state: dict[str, tuple[Any, ...]] = {}

    @property
    def _repo_states(self) -> dict[str, dict[str, Any]]:
        return self._plugin.repo_states

    @property
    def _thread_fired(self) -> set[str]:
        return self._plugin.thread_fired

    @property
    def _facts_written(self) -> int:
        return self._host.facts_written

    @property
    def _moments_proposed(self) -> int:
        return self._plugin.moments_proposed

    async def initialize(self) -> None:
        await self._host.initialize()

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        await self.poll_tick()
        await self._host.start()

    async def stop(self) -> None:
        self.is_running = False
        await self._host.stop()

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

    def find_watched_repos(self) -> list[Path]:
        return self._plugin.find_watched_repos()

    async def poll_tick(self) -> int:
        async with self._lock:
            await self._host.poll_tick("project")
            for entity, state in self._plugin.last_seen_state.items():
                self._last_seen_state[entity] = state
            return len(self.find_watched_repos())

    async def forget(self, project: str | Path) -> int:
        async with self._lock:
            slug = project_slug(project)
            project_entity = f"project:{slug}"
            removed = await self._host.forget(project_entity)
            if self._store is not None:
                removed += await self._store.forget(str(project))
            await self._plugin.forget(str(project))
            self._last_seen_state.pop(project_entity, None)
            self._last_seen_state.pop(str(project), None)
            return removed


# gen-ref: 86c879b5
