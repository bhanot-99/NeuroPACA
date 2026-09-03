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

from neuropaca.core import logging as np_logging
from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.core.health import SystemHealth, current_rss_mb
from neuropaca.core.inference import create_backend, create_interactive_backend
from neuropaca.core.models import Event
from neuropaca.orchestration.scheduler import Scheduler

_log = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

ModuleBuilder = Callable[[Config, EventBus, GraphMemory, BitNetRuntime], list[BaseModule]]


class NeuroPACAOrchestrator:
    def __init__(self, config: Config, *, module_builder: ModuleBuilder | None = None) -> None:
        self._config = config
        self._module_builder = module_builder
        self._event_bus: EventBus | None = None
        self._graph_memory: GraphMemory | None = None
        self._bitnet_runtime: BitNetRuntime | None = None
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
        self._scheduler = Scheduler(self._graph_memory, self._config)
        if self._module_builder is not None:
            self._modules.extend(
                self._module_builder(
                    self._config, self._event_bus, self._graph_memory, self._bitnet_runtime
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
