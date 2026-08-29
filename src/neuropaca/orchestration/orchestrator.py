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

from neuropaca.core import logging as np_logging
from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import SystemHealth, current_rss_mb
from neuropaca.core.inference import create_backend
from neuropaca.orchestration.scheduler import Scheduler

_log = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class NeuroPACAOrchestrator:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._event_bus: EventBus | None = None
        self._graph_memory: GraphMemory | None = None
        self._bitnet_runtime: BitNetRuntime | None = None
        self._scheduler: Scheduler | None = None
        self._modules: list[BaseModule] = []
        self._initialized = False
        self._running = False
        self._shutdown_done = False
        self._started_at: float | None = None
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
        np_logging.configure(self._config.log_level)
        self._event_bus = EventBus.get_instance()
        self._graph_memory = GraphMemory.get_instance(persistence_path=self._config.graph_db_path)
        self._bitnet_runtime = BitNetRuntime.get_instance(create_backend(self._config))
        await self._graph_memory.load()
        self._scheduler = Scheduler(self._graph_memory, self._config)
        for module in self._modules:  # L2-L9 modules arrive from B2 on
            await module.initialize()
        self._initialized = True
        _log.info(
            "orchestrator initialised (backend=%s, %d module(s))",
            self._config.inference_backend,
            len(self._modules),
        )

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
                ok=self._running and not self._shutdown_done and all(r.ok for r in reports),
                uptime_seconds=uptime,
                modules=reports,
                graph_nodes=self._graph_memory.node_count if self._graph_memory else 0,
                graph_edges=self._graph_memory.edge_count if self._graph_memory else 0,
                graph_dirty=self._graph_memory.dirty if self._graph_memory else False,
                queue_depth=self._event_bus.queue_depth if self._event_bus else 0,
                events_dropped=self._event_bus.dropped_count if self._event_bus else 0,
                inference_loaded=self._bitnet_runtime.is_loaded if self._bitnet_runtime else False,
                rss_mb=current_rss_mb(),
            )
        except Exception as exc:  # health_check never raises (Architecture.md §3.7)
            return SystemHealth(
                ok=False, uptime_seconds=0.0, notes=(f"health_check failed: {exc!r}",)
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
