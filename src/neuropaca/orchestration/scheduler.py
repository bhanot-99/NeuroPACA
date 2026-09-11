# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L10 · `Scheduler` — the daemon's background timers (Architecture.md §10, §14).

B1 runs two: persist the graph when it is dirty, and recompute
`relevance_score`s. Pressure decay joins in B5. V-6 adds a third: link the
nodes minted since the last tick that still have no edge, so a new node is
reachable within one tick instead of waiting for the next idle spell. Each tick
is guarded so a slow run never overlaps the next, and an error in one tick is
logged, not fatal.
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
        # No try/except here: CancelledError propagates on its own, and catching
        # it only to re-raise is scaffolding that reads like a decision.
        while True:
            await asyncio.sleep(self._interval)
            await self._tick()

    async def _tick(self) -> None:
        try:
            # V-6 · before the save, so a node linked this tick is persisted
            # with its edge rather than waiting a whole interval for the next
            # one. Reads only the never-linked ledger, so this is O(pending) —
            # it does not add a graph scan to the tick.
            linked = await self._graph_memory.link_new_orphans()
            if linked:
                _log.debug("V-6: linked %d new orphan node(s) to YOU", linked)
            if self._graph_memory.dirty:
                await self._graph_memory.save()
            await self._graph_memory.recalculate_importance()
        except Exception:
            _log.exception("scheduler tick failed")


# gen-ref: f388ac1f
