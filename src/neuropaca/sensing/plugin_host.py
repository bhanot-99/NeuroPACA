# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S4 · The Plugin Contract and PluginHost (VISION_PHASES.md).

Decouples data sensing from GraphMemory and EpisodeStore mutation.
A Plugin is a pure, side-effect-free reader:
- Takes a `since: datetime` watermark
- Returns a list of `PluginItem` records
- Has no reference to `GraphMemory` or `EpisodeStore`

PluginHost(BaseModule) hosts plugins and owns, once, correctly:
1. V-10 non-inflation: calls `upsert_node` only for brand-new nodes; uses `mark_seen`
   for sightings on repeat poll ticks so `access_count`/`relevance_score` are never inflated.
2. Fact churn suppression: identity-based state hashing suppresses redundant writes.
3. Force-refresh on span close: when active sessions (e.g. playback, interactive work)
   end, the stopping point is force-refreshed from the last reading rather than left frozen.
4. EpisodeKind/NodeType/domain-hub wiring: centralized PART_OF hub connections.
5. Permission manifest validation: startup validation flags plugins exceeding allowed
   paths or network constraints.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event

_log = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

__all__ = [
    "Plugin",
    "PluginDescriptor",
    "PluginHost",
    "PluginItem",
    "PluginManifest",
    "doctor",
]


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Security and resource permissions manifest for a plugin."""

    allowed_read_paths: tuple[Path, ...] = ()
    allowed_write_paths: tuple[Path, ...] = ()
    allow_network: bool = False
    allow_subprocesses: bool = False


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """Static metadata and contract declaration of a plugin."""

    name: str
    node_type: NodeType
    domain_hub: str
    poll_interval_seconds: float = 60.0
    span_kind: EpisodeKind | str | None = None
    manifest: PluginManifest = field(default_factory=PluginManifest)


@dataclass(frozen=True, slots=True)
class PluginItem:
    """Pure observation yielded by a Plugin."""

    entity_id: str
    label: str
    node_type: NodeType | None = None
    span: tuple[datetime, datetime] | None = None
    fact: tuple[EpisodeKind | str, str, dict[str, Any]] | None = None
    state_key: tuple[Any, ...] | None = None
    active: bool = False
    span_kind: EpisodeKind | str | None = None
    span_obj: str | None = None
    span_attrs: dict[str, Any] | None = None
    node_attributes: dict[str, Any] | None = None
    extra_nodes: tuple[tuple[str, str, NodeType], ...] = ()
    edges: tuple[tuple[str, str, RelationType], ...] = ()
    events: tuple[Event, ...] = ()


@runtime_checkable
class Plugin(Protocol):
    """The side-effect-free plugin reader protocol."""

    def describe(self) -> PluginDescriptor:
        """Return static descriptor, capabilities, and permissions manifest."""
        ...

    async def items(self, since: datetime) -> list[PluginItem]:
        """Pure read: extract observations since watermark timestamp."""
        ...

    def entities(self) -> frozenset[str]:
        """Return current set of known entity IDs."""
        ...

    async def forget(self, entity: str) -> int:
        """Perform plugin-internal cleanup (e.g. local cache files). Returns cleaned count."""
        ...


class PluginHost(BaseModule):
    """Centralized host managing plugin lifecycles, V-10 invariance, and episodic writes."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        episode_store: EpisodeStore | None = None,
        *,
        plugins: Sequence[Plugin] | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("plugin_host", event_bus, config)
        self._gm = graph_memory
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()

        self._plugins: dict[str, Plugin] = {}
        if plugins:
            for p in plugins:
                self.register(p)

        # Open active spans: host_key -> (entity_id, start_time, last_item)
        self._open_spans: dict[str, tuple[str, datetime, PluginItem]] = {}
        # Fact churn suppression: host_key -> state_key
        self._last_state: dict[str, tuple[Any, ...]] = {}
        # Span close force-refresh duplicate suppression: host_key -> written_key
        self._last_written: dict[str, tuple[Any, ...]] = {}
        # Watermarks: plugin_name -> last_poll_datetime
        self._last_poll: dict[str, datetime] = {}

        self._facts_written = 0
        self._spans_written = 0
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._manifest_violations: list[str] = []
        self._lock = asyncio.Lock()

    @property
    def plugins(self) -> dict[str, Plugin]:
        return dict(self._plugins)

    @property
    def facts_written(self) -> int:
        return self._facts_written

    @property
    def spans_written(self) -> int:
        return self._spans_written

    @property
    def manifest_violations(self) -> tuple[str, ...]:
        return tuple(self._manifest_violations)

    def register(self, plugin: Plugin) -> None:
        desc = plugin.describe()
        if desc.name in self._plugins:
            raise ValueError(f"plugin '{desc.name}' is already registered in PluginHost")
        self._plugins[desc.name] = plugin

    def validate_manifests(self) -> list[str]:
        """Validate all registered plugins against their declared manifests.

        Flags any plugin exceeding allowed read paths, write paths, or network constraints.
        """
        violations: list[str] = []
        for name, plugin in self._plugins.items():
            desc = plugin.describe()
            manifest = desc.manifest

            # Custom plugin validation hook if available
            custom_validator = getattr(plugin, "validate_manifest", None)
            if callable(custom_validator):
                try:
                    custom_errs = custom_validator()
                    if isinstance(custom_errs, list):
                        violations.extend(f"plugin '{name}': {err}" for err in custom_errs)
                except Exception as exc:
                    violations.append(f"plugin '{name}' manifest validation failed: {exc}")

            # Check paths declared or configured on the plugin
            configured_paths: list[Path] = []
            for attr_name in (
                "watch_paths",
                "spool_dir",
                "calendar_path",
                "reading_list_path",
                "path",
            ):
                val = getattr(plugin, attr_name, None)
                if isinstance(val, (str, Path)):
                    configured_paths.append(Path(val).expanduser().resolve())
                elif isinstance(val, (list, tuple)):
                    for sub in val:
                        if isinstance(sub, (str, Path)):
                            configured_paths.append(Path(sub).expanduser().resolve())

            if manifest.allowed_read_paths and configured_paths:
                allowed_resolved = [p.expanduser().resolve() for p in manifest.allowed_read_paths]
                for cp in configured_paths:
                    if not any(cp == ap or cp.is_relative_to(ap) for ap in allowed_resolved):
                        violations.append(
                            f"plugin '{name}': configured path '{cp}' "
                            f"exceeds manifest allowed_read_paths"
                        )

            # Check network constraints
            if not manifest.allow_network:
                for attr_name in ("network_url", "remote_host", "imap_host", "smtp_host"):
                    net_val = getattr(plugin, attr_name, None)
                    if net_val:
                        violations.append(
                            f"plugin '{name}': network access '{net_val}' prohibited by manifest"
                        )

        self._manifest_violations = violations
        return violations

    async def initialize(self) -> None:
        self.validate_manifests()
        if self._manifest_violations:
            for v in self._manifest_violations:
                _log.warning("PluginHost manifest warning: %s", v)

        for name, plugin in self._plugins.items():
            init_fn = getattr(plugin, "initialize", None)
            if callable(init_fn):
                try:
                    res = init_fn()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    _log.error("Failed to initialize plugin '%s': %s", name, exc)

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        for name, plugin in self._plugins.items():
            desc = plugin.describe()
            task = asyncio.create_task(
                self._run_plugin_loop(name, desc.poll_interval_seconds),
                name=f"plugin_host_loop_{name}",
            )
            self._poll_tasks.append(task)

    async def stop(self) -> None:
        self.is_running = False
        for task in self._poll_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._poll_tasks.clear()

        # Close any remaining open spans on shutdown and force-refresh facts
        now = self._clock.now()
        for host_key, (entity_id, start_time, last_item) in list(self._open_spans.items()):
            plugin_name = host_key.split(":", 1)[0]
            desc = self._plugins[plugin_name].describe() if plugin_name in self._plugins else None
            raw_span_kind = (
                last_item.span_kind
                or (desc.span_kind if desc else None)
            )
            if raw_span_kind and self._store is not None:
                self._store.record_span(
                    EpisodeKind(raw_span_kind),
                    entity_id,
                    start_time,
                    now,
                    obj=last_item.span_obj,
                    source=plugin_name,
                    attrs=last_item.span_attrs,
                )
                self._spans_written += 1
            await self._assert_fact_guarded(
                plugin_name,
                entity_id,
                last_item.fact,
                now,
                force=True,
                state_key=last_item.state_key,
            )
        self._open_spans.clear()

        for name, plugin in self._plugins.items():
            stop_fn = getattr(plugin, "stop", None)
            if callable(stop_fn):
                try:
                    res = stop_fn()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    _log.error("Failed to stop plugin '%s': %s", name, exc)

    def health(self) -> ModuleHealth:
        if self._manifest_violations:
            return ModuleHealth(
                name=self.name,
                ok=False,
                detail=f"manifest violations: {'; '.join(self._manifest_violations)}",
            )
        last_poll_ts = max(self._last_poll.values()) if self._last_poll else None
        return ModuleHealth(
            name=self.name,
            ok=True,
            detail=(
                f"plugins={len(self._plugins)}, "
                f"facts_written={self._facts_written}, "
                f"spans_written={self._spans_written}, "
                f"open_spans={len(self._open_spans)}"
            ),
            last_event_at=last_poll_ts,
        )

    async def _run_plugin_loop(self, plugin_name: str, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self.poll_tick(plugin_name)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.error("Error in plugin '%s' poll loop: %s", plugin_name, exc)

    async def poll_tick(self, plugin_name: str | None = None) -> int:
        """Poll one or all registered plugins. Returns total facts written this tick."""
        async with self._lock:
            now = self._clock.now()
            targets = [plugin_name] if plugin_name else list(self._plugins.keys())
            total_facts_before = self._facts_written

            for name in targets:
                plugin = self._plugins.get(name)
                if plugin is None:
                    continue

                desc = plugin.describe()
                watermark = self._last_poll.get(name, _EPOCH)
                self._last_poll[name] = now

                try:
                    items = await plugin.items(watermark)
                except Exception as exc:
                    _log.error("Plugin '%s' items() raised: %s", name, exc)
                    continue

                active_keys: set[str] = set()
                closed_keys: set[str] = set()

                for item in items:
                    host_key = f"{name}:{item.entity_id}"
                    if item.active:
                        active_keys.add(host_key)

                    # 1. GraphMemory integration (V-10 invariance)
                    await self._integrate_graph_nodes(desc, item, now)

                    # 2. Discrete spans (historical/closed events)
                    if item.span is not None and self._store is not None:
                        start_time, end_time = item.span
                        raw_span_kind = item.span_kind or desc.span_kind
                        if raw_span_kind:
                            self._store.record_span(
                                EpisodeKind(raw_span_kind),
                                item.entity_id,
                                start_time,
                                end_time,
                                obj=item.span_obj,
                                source=name,
                                attrs=item.span_attrs,
                            )
                            self._spans_written += 1

                    # 3. Active ongoing sessions management
                    if item.active:
                        if host_key in self._open_spans:
                            entity_id, start_time, _ = self._open_spans[host_key]
                            self._open_spans[host_key] = (entity_id, start_time, item)
                        else:
                            self._open_spans[host_key] = (item.entity_id, now, item)
                    else:
                        if host_key in self._open_spans:
                            # State transitioned from active to inactive
                            entity_id, start_time, last_item = self._open_spans.pop(host_key)
                            closed_keys.add(host_key)
                            raw_span_kind = item.span_kind or last_item.span_kind or desc.span_kind
                            if raw_span_kind and self._store is not None:
                                self._store.record_span(
                                    EpisodeKind(raw_span_kind),
                                    item.entity_id,
                                    start_time,
                                    now,
                                    obj=item.span_obj or last_item.span_obj,
                                    source=name,
                                    attrs=item.span_attrs or last_item.span_attrs,
                                )
                                self._spans_written += 1
                            # Force-refresh on span close
                            state_key = (
                                item.state_key
                                if item.state_key is not None
                                else last_item.state_key
                            )
                            await self._assert_fact_guarded(
                                name,
                                item.entity_id,
                                item.fact or last_item.fact,
                                now,
                                force=True,
                                state_key=state_key,
                            )

                    # 4. Fact assertion with churn suppression
                    if item.fact is not None and host_key not in closed_keys:
                        await self._assert_fact_guarded(
                            name,
                            item.entity_id,
                            item.fact,
                            now,
                            force=False,
                            state_key=item.state_key,
                        )

                    # 5. Event publication
                    if item.events and self.event_bus:
                        for ev in item.events:
                            self.event_bus.publish(ev)

                # 6. Close open spans for any entities no longer reported active
                for open_key in list(self._open_spans.keys()):
                    if open_key.startswith(f"{name}:") and open_key not in active_keys:
                        entity_id, start_time, last_item = self._open_spans.pop(open_key)
                        raw_span_kind = last_item.span_kind or desc.span_kind
                        if raw_span_kind and self._store is not None:
                            self._store.record_span(
                                EpisodeKind(raw_span_kind),
                                entity_id,
                                start_time,
                                now,
                                obj=last_item.span_obj,
                                source=name,
                                attrs=last_item.span_attrs,
                            )
                            self._spans_written += 1
                        await self._assert_fact_guarded(
                            name,
                            entity_id,
                            last_item.fact,
                            now,
                            force=True,
                            state_key=last_item.state_key,
                        )

            if self._facts_written > total_facts_before and self._store is not None:
                await self._store.flush()

            return self._facts_written - total_facts_before

    async def _integrate_graph_nodes(
        self, desc: PluginDescriptor, item: PluginItem, now: datetime
    ) -> None:
        """V-10 compliant node creation and sighting."""
        if self._gm is None:
            return

        node_type = item.node_type or desc.node_type
        attrs: dict[str, Any] = {"label": item.label}
        if item.node_attributes:
            attrs.update(item.node_attributes)

        if not self._gm.has_node(item.entity_id):
            await self._gm.upsert_node(
                item.entity_id, node_type, attributes=attrs
            )
        else:
            await self._gm.mark_seen(item.entity_id, now)
            await self._gm.update_node(item.entity_id, attrs)

        if desc.domain_hub and self._gm.has_node(desc.domain_hub):
            await self._gm.add_edge(
                item.entity_id, desc.domain_hub, relation=RelationType.PART_OF
            )

        # Auxiliary nodes
        for extra_id, extra_label, extra_type in item.extra_nodes:
            if not self._gm.has_node(extra_id):
                await self._gm.upsert_node(
                    extra_id, extra_type, attributes={"label": extra_label}
                )
                if desc.domain_hub and self._gm.has_node(desc.domain_hub):
                    await self._gm.add_edge(
                        extra_id, desc.domain_hub, relation=RelationType.PART_OF
                    )
            else:
                await self._gm.mark_seen(extra_id, now)

        # Auxiliary edges
        for src, dst, rel in item.edges:
            await self._gm.add_edge(src, dst, relation=rel)

    async def _assert_fact_guarded(
        self,
        plugin_name: str,
        entity_id: str,
        fact: tuple[EpisodeKind | str, str, dict[str, Any]] | None,
        now: datetime,
        *,
        force: bool = False,
        state_key: tuple[Any, ...] | None = None,
    ) -> bool:
        """Write a fact guarded by identity churn suppression and span-close refresh."""
        if fact is None or self._store is None:
            return False

        kind, obj, attrs = fact
        host_key = f"{plugin_name}:{entity_id}"

        # If plugin did not provide a custom state key, derive one from fact identity
        if state_key is None:
            frozen_attrs = tuple(
                sorted(
                    (k, str(v))
                    for k, v in attrs.items()
                    if k not in ("position_seconds", "valid_from")
                )
            )
            state_key = (str(kind), obj, frozen_attrs)

        pos = attrs.get("position_seconds")
        pos_key = round(pos) if isinstance(pos, (int, float)) else None
        written_key = (*state_key, pos_key)

        if force:
            if self._last_written.get(host_key) == written_key:
                return False
        elif self._last_state.get(host_key) == state_key:
            return False

        self._last_state[host_key] = state_key
        self._last_written[host_key] = written_key

        valid_from = attrs.get("valid_from", now)
        if not isinstance(valid_from, datetime):
            valid_from = now

        clean_attrs = {k: v for k, v in attrs.items() if k != "valid_from"}

        self._store.assert_fact(
            EpisodeKind(kind),
            entity_id,
            obj,
            valid_from=valid_from,
            source=plugin_name,
            attrs=clean_attrs,
        )
        self._facts_written += 1
        return True

    async def forget(self, entity: str) -> int:
        """Purge an entity across graph, store, host state, and all plugins."""
        async with self._lock:
            cleaned = 0

            # 1. Clean GraphMemory
            if self._gm is not None and self._gm.has_node(entity):
                await self._gm.delete_node(entity)
                cleaned += 1

            # 2. Clean EpisodeStore
            if self._store is not None:
                cleaned += await self._store.forget(entity)

            # 3. Clean host tracking caches
            for d in (self._last_state, self._last_written, self._open_spans):
                for k in list(d.keys()):
                    if k.endswith(f":{entity}") or k == entity:
                        d.pop(k, None)

            # 4. Delegate plugin-internal cleanup
            for plugin in self._plugins.values():
                cleaned += await plugin.forget(entity)

            return cleaned


def doctor(host: PluginHost) -> list[str]:
    """Inspect all plugins hosted in host and return any manifest violations
    (VISION_PHASES.md §S4).
    """
    return host.validate_manifests()
