# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L10 · `NeuroPACAOrchestrator` — constructs the daemon, owns its lifecycle
(Architecture.md §10).

Startup:  configure logging -> get L1 singletons -> `GraphMemory.load()` ->
          build the scheduler -> `EventBus.start()` -> start modules -> timers.
Shutdown: stop modules -> stop timers -> drain the queue -> stop the bus ->
          `GraphMemory.save()` (exactly once) -> unload the model.

`request_shutdown_nowait()` is the sync entry a real `SIGTERM` handler calls; it
just sets the shutdown event that `run()` awaits. `request_shutdown()` is the
async form that also performs the shutdown, for callers not going through
`run()`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from neuropaca.core import logging as np_logging
from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.episodic_writer import EpisodicWriter
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.core.health import SystemHealth, current_rss_mb
from neuropaca.core.inference import create_backend, create_interactive_backend
from neuropaca.core.models import Event
from neuropaca.interface.briefing import BriefingComposer
from neuropaca.orchestration.scheduler import Scheduler

if TYPE_CHECKING:
    from neuropaca.diagnosis.app_identity import AppIdentity

_log = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

ModuleBuilder = Callable[[Config, EventBus, GraphMemory, BitNetRuntime], list[BaseModule]]


def non_activity_app(config: Config, identity: AppIdentity) -> Callable[[str], bool]:
    """V-3c · the boot tidy's "is this `app:` node not really an app?" test:
    B17's thread-label / bare-shell check, plus every name the config says is
    not an activity — `process_exclude_names` (the daemon's own footprint,
    D-20) and `focus_exclude_app_ids` (dialogs, portals). Those filters only
    stop *new* nodes; nodes minted before a filter existed — or while a config
    file overrode it with `[]` — were never removed. Matched on the raw name
    and on its canonical id, case-insensitively."""
    excluded = {
        name.strip().lower()
        for name in (*config.process_exclude_names, *config.focus_exclude_app_ids)
        if name.strip()
    }

    def is_non_activity(bare: str) -> bool:
        canon = identity.resolve(bare) or bare
        return identity.is_non_app(bare) or bare.lower() in excluded or canon.lower() in excluded

    return is_non_activity


class NeuroPACAOrchestrator:
    def __init__(self, config: Config, *, module_builder: ModuleBuilder | None = None) -> None:
        self._config = config
        self._module_builder = module_builder
        self._event_bus: EventBus | None = None
        self._graph_memory: GraphMemory | None = None
        self._bitnet_runtime: BitNetRuntime | None = None
        self._episode_store: EpisodeStore | None = None
        self._scheduler: Scheduler | None = None
        self._modules: list[BaseModule] = []
        self._initialized = False
        self._running = False
        self._shutdown_done = False
        self._started_at: float | None = None
        # Non-fatal degradations survived at boot (B9/BL-2). Surfaced through
        # health_check().notes so `neuropaca health` shows a daemon that came
        # up on a reseeded graph as degraded rather than silently ok.
        self._degraded_notes: list[str] = []
        self._shutdown_event = asyncio.Event()

    # ---------------------------------------------------------------- properties
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def event_bus(self) -> EventBus:
        if self._event_bus is None:
            raise RuntimeError("orchestrator not initialised — call initialize() first")
        return self._event_bus

    @property
    def graph_memory(self) -> GraphMemory:
        if self._graph_memory is None:
            raise RuntimeError("orchestrator not initialised — call initialize() first")
        return self._graph_memory

    @property
    def bitnet_runtime(self) -> BitNetRuntime:
        if self._bitnet_runtime is None:
            raise RuntimeError("orchestrator not initialised — call initialize() first")
        return self._bitnet_runtime

    @property
    def episode_store(self) -> EpisodeStore | None:
        """S0's bi-temporal log — `None` when `episodes_enabled = false`."""
        return self._episode_store

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        if self._initialized:
            return
        np_logging.configure(
            self._config.log_level,
            file_path=self._config.log_file_path if self._config.log_to_file else None,
        )
        self._event_bus = EventBus.get_instance()
        self._graph_memory = GraphMemory.get_instance(persistence_path=self._config.graph_db_path)
        self._bitnet_runtime = BitNetRuntime.get_instance(
            create_backend(self._config),
            create_interactive_backend(self._config),  # B5 · L9 $ / $? model (D-12)
        )
        await self._load_graph_with_recovery()
        await self._canonicalise_app_nodes()
        if self._config.episodes_enabled:
            # S0 · the durable bi-temporal log beside the graph (VISION_PHASES.md).
            # Started before the modules so `EpisodicWriter` never misses an
            # early event; stopped after the graph is saved (mirrors GraphMemory).
            self._episode_store = EpisodeStore(self._config.episodes_db_path)
            await self._episode_store.start()
        self._scheduler = Scheduler(self._graph_memory, self._config, self._episode_store)
        if self._module_builder is not None:
            self._modules.extend(
                self._module_builder(
                    self._config, self._event_bus, self._graph_memory, self._bitnet_runtime
                )
            )
        if self._episode_store is not None:
            # Not through `module_builder` (rules.md §0's "no module imports
            # another" is unaffected — this only avoids widening every
            # `ModuleBuilder` call site for one conditional module).
            self._modules.append(
                EpisodicWriter(
                    self._event_bus, self._config, self._episode_store, clock=SystemClock()
                )
            )
            self._modules.append(
                BriefingComposer(
                    self._event_bus,
                    self._config,
                    self._graph_memory,
                    self._episode_store,
                    clock=SystemClock(),
                )
            )
        # A6 · the L9 health bridge — L9 cannot import L10, so it asks over the bus.
        self._event_bus.subscribe(EventType.SYSTEM_HEALTH_REQUEST, self._on_health_request)
        for module in self._modules:  # manually-registered first, then built
            await module.initialize()
        self._initialized = True
        _log.info(
            "orchestrator initialised (backend=%s, %d module(s))",
            self._config.inference_backend,
            len(self._modules),
        )

    async def _canonicalise_app_nodes(self) -> None:
        """B17 · one real app = one `app:` node. A pre-B17 graph has the same app
        under both its Wayland `app_id` and its process name; this folds them once
        at boot. Idempotent — a no-op on every start after the first. Never raises:
        a graph that fails to tidy still boots (like `link_orphan_nodes`)."""
        assert self._graph_memory is not None
        try:
            from neuropaca.diagnosis.app_identity import AppIdentity

            identity = AppIdentity.from_file(self._config.app_identity_path)
            merged, dropped = await self._graph_memory.canonicalise_app_nodes(
                identity.resolve, non_activity_app(self._config, identity)
            )
            if merged or dropped:
                _log.info(
                    "B17 app-identity pass: merged %d duplicate app node(s), "
                    "dropped %d non-app node(s)",
                    merged,
                    dropped,
                )
            # V-3a · idle cycles can be rare, so the boot tidy also hands back
            # every `-> YOU` placeholder a node no longer needs
            released = await self._graph_memory.release_you_links()
            if released:
                _log.info("V-3a: released %d stale -> YOU placeholder link(s)", released)
            # V-6 · the ledger `link_new_orphans` drains only knows about nodes
            # minted in *this* process, so a node orphaned on disk before a
            # restart would still wait for an idle spell. One whole-graph sweep
            # at boot closes that, next to the scan `canonicalise_app_nodes`
            # already does here.
            linked = await self._graph_memory.link_orphan_nodes()
            if linked:
                _log.info("V-6: linked %d orphan node(s) carried in from disk", linked)
        except Exception:
            _log.exception("B17 app-identity pass failed — booting with the graph as loaded")

    async def _load_graph_with_recovery(self) -> None:
        """Load the graph; if it is unreadable, quarantine it and boot on a fresh
        one rather than refusing to start (B9/BL-2).

        Before B9 this was a bare `await load()`. `load()` raises
        `GraphMemoryError` on any unreadable file, so a single corrupt
        `graph.json` took the daemon down, `Restart=on-failure` restarted it into
        the same corrupt file, and `StartLimitBurst` then gave up permanently —
        with no `neuropaca` verb able to explain why, because every one of them
        needs the daemon that will not start. A graph is a *derived* artefact
        rebuilt from observation, so refusing to run without it trades a
        recoverable problem for an unrecoverable one.

        The bad file is moved, never deleted: it is the only evidence of what
        went wrong, and `neuropaca doctor` reports it.
        """
        assert self._graph_memory is not None
        try:
            await self._graph_memory.load()
            return
        except GraphMemoryError as exc:
            quarantined = self._quarantine_unreadable_graph(exc)

        # Reseed in place — the singleton is already wired into the modules built
        # below, so it must be *this* instance that comes back with the 11 hubs.
        await self._graph_memory.reset_to_seed()
        _log.error(
            "graph was unreadable and has been quarantined at %s — "
            "booted on a fresh 11-hub graph; run `neuropaca doctor` for detail",
            quarantined,
        )
        self._degraded_notes.append(
            f"graph unreadable at boot; quarantined to {quarantined} and reseeded"
        )

    def _quarantine_unreadable_graph(self, exc: Exception) -> str:
        """Move the unreadable graph aside. Never raises — a failure here must not
        turn a recoverable boot into the crash loop this whole path exists to
        avoid."""
        source = Path(self._config.graph_db_path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = source.with_name(f"{source.name}.corrupt.{stamp}")
        try:
            if source.exists():
                source.replace(target)
        except OSError as move_exc:
            _log.error(
                "could not quarantine the unreadable graph %s (%r) — "
                "starting fresh anyway; the original may be overwritten on the "
                "next save",
                source,
                move_exc,
            )
            return f"{source} (quarantine failed: {move_exc!r})"
        _log.error("graph %s was unreadable (%r)", source, exc)
        return str(target)

    def register_module(self, module: BaseModule) -> None:
        """Attach a module before `initialize()`. The orchestrator then drives its
        `initialize()` / `start()` / `stop()` in lifecycle order (Architecture.md §10)."""
        if self._initialized:
            raise RuntimeError("register_module() must be called before initialize()")
        self._modules.append(module)

    async def start(self) -> None:
        if self._running:
            return
        if not self._initialized:
            raise RuntimeError("call initialize() before start()")
        await self.event_bus.start()
        for module in self._modules:
            await module.start()
        assert self._scheduler is not None
        self._scheduler.start()
        self._started_at = time.perf_counter()
        self._running = True
        _log.info("orchestrator started")

    async def stop(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._running = False

        if self._event_bus is not None:
            self._event_bus.unsubscribe(EventType.SYSTEM_HEALTH_REQUEST, self._on_health_request)

        for module in reversed(self._modules):
            try:
                await module.stop()
            except Exception:
                _log.exception("module %s failed to stop", module.name)

        if self._scheduler is not None:
            await self._scheduler.stop()

        if self._event_bus is not None:
            await self._event_bus.join()  # drain what is queued before we stop
            await self._event_bus.stop()

        if self._graph_memory is not None:
            await self._graph_memory.save()

        if self._episode_store is not None:
            await self._episode_store.stop()

        if self._bitnet_runtime is not None:
            self._bitnet_runtime.unload_model()

        _log.info("orchestrator stopped")

    async def run(self) -> None:
        """Initialise (if needed), start, and block until shutdown is requested."""
        if not self._initialized:
            await self.initialize()
        self._install_signal_handlers()
        await self.start()
        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    # ------------------------------------------------------------------ shutdown
    def request_shutdown_nowait(self) -> None:
        """Sync entry point for an OS signal handler — never blocks."""
        self._shutdown_event.set()

    async def request_shutdown(self) -> None:
        self._shutdown_event.set()
        await self.stop()

    # -------------------------------------------------------------------- health
    def health_check(self) -> SystemHealth:
        try:
            uptime = (
                0.0
                if self._started_at is None
                else max(0.0, time.perf_counter() - self._started_at)
            )
            reports = tuple(module.health() for module in self._modules)
            return SystemHealth(
                ok=(
                    self._running
                    and not self._shutdown_done
                    and all(r.ok for r in reports)
                    and not self._degraded_notes
                ),
                uptime_seconds=uptime,
                modules=reports,
                graph_nodes=self._graph_memory.node_count if self._graph_memory else 0,
                graph_edges=self._graph_memory.edge_count if self._graph_memory else 0,
                graph_dirty=self._graph_memory.dirty if self._graph_memory else False,
                queue_depth=self._event_bus.queue_depth if self._event_bus else 0,
                events_dropped=self._event_bus.dropped_count if self._event_bus else 0,
                inference_loaded=self._bitnet_runtime.is_loaded if self._bitnet_runtime else False,
                rss_mb=current_rss_mb(),
                graph_schema_version=graph_schema_version(),
                notes=tuple(self._degraded_notes),
            )
        except Exception as exc:  # health_check never raises (Architecture.md §3.7)
            return SystemHealth(
                ok=False, uptime_seconds=0.0, notes=(f"health_check failed: {exc!r}",)
            )

    # ------------------------------------------------------------------ health bridge
    async def _on_health_request(self, _event: Event) -> None:
        """Answer L9's `SYSTEM_HEALTH_REQUEST` with a serialisable snapshot (A6).
        Never raises — `health_check()` already swallows its own failures."""
        try:
            report = asdict(self.health_check())
        except Exception as exc:  # a handler never raises (rules.md §2)
            _log.exception("health bridge failed")
            report = {"ok": False, "notes": [f"health bridge failed: {exc!r}"]}
        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_HEALTH_REPORT,
                source="orchestrator",
                payload={"health": report},
            )
        )

    # ------------------------------------------------------------------ internal
    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, self.request_shutdown_nowait)
            except (NotImplementedError, RuntimeError, ValueError):
                _log.debug("no signal handler for %s in this context", sig)


# gen-ref: a0519b6e
