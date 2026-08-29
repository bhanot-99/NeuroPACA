"""L10 · `Scheduler` — the daemon's background timers (Architecture.md §10, §14).

B1 runs two: persist the graph when it is dirty, and recompute
`relevance_score`s. Pressure decay joins in B5. Each tick is guarded so a slow
run never overlaps the next, and an error in one tick is logged, not fatal.
"""

from __future__ import annotations

import asyncio
import logging

from neuropaca.core.config import Config
from neuropaca.core.graph_memory import GraphMemory

_log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, graph_memory: GraphMemory, config: Config) -> None:
        self._graph_memory = graph_memory
        self._interval = float(config.graph_save_interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._tick()
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        try:
            if self._graph_memory.dirty:
                await self._graph_memory.save()
            await self._graph_memory.recalculate_importance()
        except Exception:
            _log.exception("scheduler tick failed")
