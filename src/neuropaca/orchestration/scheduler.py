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
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.graph_rebuild import catch_up_focus_span

_log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        graph_memory: GraphMemory,
        config: Config,
        episode_store: EpisodeStore | None = None,
    ) -> None:
        self._graph_memory = graph_memory
        self._episode_store = episode_store
        self._config = config
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
            await self._catch_up_episode_watermark()
            if self._graph_memory.dirty:
                await self._graph_memory.save()
            await self._graph_memory.recalculate_importance()
        except Exception:
            _log.exception("scheduler tick failed")

    async def _catch_up_episode_watermark(self) -> None:
        """S0 · graph-store consistency (VISION_PHASES.md). The bus drops
        events under backpressure by design, and an isolated subscriber
        failure (rules.md §2) can leave `EpisodicWriter` recording an episode
        that `SignalCorrelator` never got to mutate the graph for. `since()`
        past the graph's own watermark is O(1) in the normal case (zero or
        one missed row) — the self-heal, not a detect-and-alert check.

        A missed `focus_span` gets `catch_up_focus_span` — the node, its
        `domain:*`/browser structure, a Hebbian coactivation bump derived from
        the live graph's own `last_seen_at` (not an exact replay, a reasonable
        one — see that function's docstring), and the sighting. An `idle_span`
        gets only `mark_seen`: `SignalCorrelator` never reacts to idle/activity
        events in the first place, so there is nothing else to redo for it.
        The exact-match repair, when that precision is worth its O(all
        history) cost, is the full rebuild (`neuropaca repair-graph`)."""
        if self._episode_store is None:
            return
        rows = await self._episode_store.since(self._graph_memory.last_episode_seq)
        if not rows:
            return
        for row in rows:
            if row.kind == "focus_span":
                await catch_up_focus_span(self._graph_memory, row, self._config)
            elif row.kind == "idle_span" and row.t_end is not None:
                await self._graph_memory.mark_seen(row.subject, row.t_end)
        await self._graph_memory.advance_last_episode_seq(rows[-1].episode_seq)
        _log.debug("S0: replayed %d missed episode(s) past the graph's watermark", len(rows))


# gen-ref: f388ac1f
