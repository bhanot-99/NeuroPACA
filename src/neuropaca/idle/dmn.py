# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L6 · `DefaultModeNetwork` — idle cognition (Architecture.md §8, B6, D-13).

When CPU drops (you walked away) L2 publishes `IDLE_DETECTED`; the DMN starts one
cancellable `idle_task`. The cycle has two halves:

- **Reminiscence** — graph housekeeping: merge exact-duplicate nodes, link
  orphans to `YOU`, prune stale / expired nodes, and reap `domain:` hubs nothing
  routes to (V-5). All of it runs through
  `GraphMemory`'s bounded-transaction workers (one lock per mutation, yield
  between), so `ACTIVITY_DETECTED` cancelling the task mid-cycle never leaves the
  graph half-mutated.
- **Imagination** — draw `dmn_top_k` seed nodes from the `dmn_candidate_pool_k`
  highest-`relevance_score` nodes (V-4: score-weighted sampling, recent seeds
  penalised — the argmax top-K never moved, so every idle thought circled the
  same five nodes) and, up to
  `dmn_max_inferences_per_cycle` times, ask the **loop** model (BitNet 2B4T, not
  the interactive Qwen — L6 is background) for a *strictly extractive* follow-up
  question: pick a subject node, maybe an object node, and a `query_template`
  enum (`learning/prompts.py`). The rendered question is stored as an
  `IDLE_THOUGHT` node and published on `INSIGHT_GENERATED`; L9 surfaces it once
  on your return (B5 `surfaced_at`).

Budgets are strict (D-13): the whole cycle is wrapped in
`asyncio.timeout(dmn_cycle_wall_clock_seconds)`; an overrun is logged, never
fatal. The DMN also stops the moment `BitNetRuntime.is_busy` — optional work
always yields to whatever else wants the model (rules.md §4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.labels import KIND_PREFIX, LabelKind
from neuropaca.core.models import Event, Node, system_error_event
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    PROACTIVE_MAX_TOKENS,
    alias_nodes,
    build_proactive_grammar,
    build_proactive_prompt,
    parse_proactive,
    template_rotation,
)

_log = logging.getLogger(__name__)

_EXCLUDED_SEED_TYPES = frozenset({NodeType.INSIGHT, NodeType.IDLE_THOUGHT})
EPHEMERAL_PREFIX = KIND_PREFIX[LabelKind.PROBE]

# V-4 · a node that has just been a seed keeps this fraction of its sampling
# weight for `dmn_seed_refractory_cycles` cycles. Not zero: the apps you really
# live in should still come up often — just not to the exclusion of everything
# else.
_SEED_REFRACTORY_PENALTY = 0.2
# Floor under the sampling weight so a score-0.0 node (fresh, or never yet
# scored) is reachable rather than impossible.
_SEED_WEIGHT_FLOOR = 0.05


class DefaultModeNetwork(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__("idle", event_bus, config)
        self._graph = graph_memory
        self._runtime = bitnet_runtime
        self._clock: Clock = clock or SystemClock()
        self._idle_task: asyncio.Task[None] | None = None
        self._cycles = 0
        self._thoughts = 0
        self._cancels = 0
        self._timeouts = 0
        self._errors = 0
        self._last_at: datetime | None = None
        self._last_summary = ""
        # V-1 · Hebbian decay is paced by uptime; downtime is not "disuse"
        self._last_decay_mono = self._clock.monotonic()
        # V-4 · imagination is sampled, not argmax. Injectable so a test can
        # pin the draw; production gets an unseeded `Random` (no global state —
        # the module-level `random` is shared and something else reseeding it
        # would silently couple to this).
        self._rng = rng if rng is not None else random.Random()
        self._recent_seeds: deque[str] = deque(
            maxlen=max(0, config.dmn_top_k * config.dmn_seed_refractory_cycles)
        )
        self._template_step = 0

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        task, self._idle_task = self._idle_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._cycles} cycles · {self._thoughts} thoughts · "
                f"{self._cancels} cancelled · {self._timeouts} over-budget · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_idle_detected(self, _event: Event) -> None:
        """Start a cycle immediately (D-13). One at a time — a still-running
        cycle is left to finish."""
        try:
            if not self.is_running:
                return
            if self._idle_task is not None and not self._idle_task.done():
                return
            self._idle_task = asyncio.create_task(self._run_idle_cycle())
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("DMN on_idle_detected failed")
            self.event_bus.publish(
                system_error_event(module="idle", exception=str(exc), severity="handler")
            )

    async def on_activity_detected(self, _event: Event) -> None:
        """Cancel the running cycle within this tick — do NOT await it here
        (a handler must not block the dispatch loop, rules.md §2). Cancellation
        lands at the task's next await, between two atomic graph mutations;
        `stop()` awaits any straggler."""
        task = self._idle_task
        if task is not None and not task.done():
            task.cancel()
            self._cancels += 1

    # --------------------------------------------------------------- the cycle
    async def _run_idle_cycle(self) -> None:
        self._cycles += 1
        try:
            async with asyncio.timeout(self.config.dmn_cycle_wall_clock_seconds):
                reminiscence = await self._reminiscence()
                made = await self._imagination()
                # Inside the budget, not after it: the docstring promises the
                # whole cycle is bounded, and a save on a large graph is the
                # longest thing in it. A cancelled save now re-flags `_dirty`,
                # so the scheduler still persists this work.
                if self._graph.dirty:
                    await self._graph.save()
            self._last_at = self._clock.now()
            self._last_summary = f"{reminiscence} · {made} thoughts"
        except asyncio.CancelledError:
            self._last_summary = "cancelled on activity"
            raise
        except TimeoutError:
            self._timeouts += 1
            self._last_summary = "exceeded wall-clock budget"
            _log.warning(
                "DMN cycle exceeded its %ss budget — abandoned",
                self.config.dmn_cycle_wall_clock_seconds,
            )
        except Exception as exc:
            self._errors += 1
            _log.exception("DMN idle cycle failed")
            self.event_bus.publish(
                system_error_event(module="idle", exception=str(exc), severity="handler")
            )

    async def _reminiscence(self) -> str:
        merged = await self._graph.consolidate()
        # T7 · "use it or lose it" — decay the Hebbian co-occurrence mesh and
        # drop edges that faded below the floor, *before* linking orphans (a node
        # left edgeless by a prune is re-attached to YOU on the next line).
        # V-1: the factor follows daemon uptime since the last sweep. A fixed
        # factor per cycle made decay depend on how often the CPU went idle, so
        # a day of many short idle spells erased every pair used only once.
        now = self._clock.monotonic()
        elapsed = max(0.0, now - self._last_decay_mono)
        self._last_decay_mono = now
        half_life = self.config.hebbian_half_life_hours * 3600.0
        faded = await self._graph.decay_cooccurrence_edges(
            0.5 ** (elapsed / half_life), self.config.hebbian_floor
        )
        # V-3a · a node that gained a real edge gives its `-> YOU` placeholder
        # back before orphans are (re)linked — YOU stays a hub of true orphans
        released = await self._graph.release_you_links()
        linked = await self._graph.link_orphan_nodes()
        ttl = timedelta(hours=self.config.dmn_idle_thought_ttl_hours)
        pruned = await self._graph.prune_stale_nodes(ttl)
        # V-5 · last, after every other sweep has settled the edges: a hub only
        # counts as dead once this cycle's prunes and links are done, and a hub
        # reaped here comes back the moment something routes to it again.
        hubs = await self._graph.prune_dead_hubs()
        return (
            f"merged {merged} · faded {faded} · released {released} · "
            f"linked {linked} · pruned {pruned} · dead hubs {hubs}"
        )

    async def _imagination(self) -> int:
        """Returns the count made *this cycle* (for the summary line). The running
        `self._thoughts` tally is bumped in `_one_thought` as each thought lands,
        so a cycle abandoned mid-imagination (timeout / activity) still reports
        the thoughts it did produce."""
        budget = self.config.dmn_max_inferences_per_cycle
        if budget <= 0 or self._runtime.backend_unavailable:
            return 0
        seeds = self._seed_nodes()
        if len(seeds) < 2:
            return 0  # nothing to relate — a follow-up question needs two nodes
        if not self._runtime.is_loaded and not await self._runtime.load_model_async():
            return 0
        # V-4 · remember this cycle's draw *before* the inferences, not after:
        # a cycle cancelled by ACTIVITY_DETECTED mid-imagination still spent the
        # seeds, and the next cycle should move on rather than redraw the same
        # five nodes it was interrupted on.
        self._recent_seeds.extend(n.id for n in seeds)

        made = 0
        for rotation in range(min(budget, len(seeds))):
            if self._runtime.is_busy:
                break
            if await self._one_thought(seeds, rotation) is not None:
                made += 1
        return made

    def _seed_nodes(self) -> list[Node]:
        """The nodes offered to the model this cycle (V-4).

        Was: the `dmn_top_k` argmax nodes by `relevance_score` — a set that does
        not move, so imagination re-asked the same clique until every
        subject/object/template combination existed and every later cycle
        produced nothing but duplicates. Now the same one-pass scan collects a
        wider `dmn_candidate_pool_k` pool and `dmn_top_k` seeds are *sampled*
        from it, weighted by score and penalised for having just been used. The
        added cost is a larger heap on a scan that already ran and one pass over
        the pool — no extra traversal, no extra inference call.
        """
        k = self.config.dmn_top_k
        pool = self._top_nodes(max(k, self.config.dmn_candidate_pool_k))
        if len(pool) <= k:
            return pool
        recent = set(self._recent_seeds)
        # Efraimidis-Spirakis: one key per item, take the k largest — a weighted
        # sample without replacement in a single O(pool) pass.
        keyed = sorted(
            (
                (self._rng.random() ** (1.0 / self._seed_weight(node, recent)), node.id, node)
                for node in pool
            ),
            key=lambda t: (-t[0], t[1]),
        )
        return [node for _, _, node in keyed[:k]]

    @staticmethod
    def _seed_weight(node: Node, recent: set[str]) -> float:
        weight = max(_SEED_WEIGHT_FLOOR, node.relevance_score)
        return weight * _SEED_REFRACTORY_PENALTY if node.id in recent else weight

    def _top_nodes(self, k: int) -> list[Node]:
        """Top-K non-hub, non-thought nodes by `relevance_score`. A sync read —
        bounded dict work, inside the cycle's wall-clock budget. B18: L8's
        `ephemeral:` probes are the system's own bookkeeping, not things the user
        touched — seeding on them produced questions about probe text."""
        return self._graph.top_nodes_by_score(
            k, exclude_types=_EXCLUDED_SEED_TYPES, exclude_prefixes=(EPHEMERAL_PREFIX,)
        )

    async def _one_thought(self, seeds: list[Node], rotation: int) -> Insight | None:
        ordered = seeds[rotation:] + seeds[:rotation]
        aliased = alias_nodes(ordered)
        aliases = [a for a, _ in aliased]
        alias_to_id = {a: n.id for a, n in aliased}
        # V-4 · the question menu rotates per inference and carries across
        # cycles, so the facet vocabulary is actually used instead of every
        # thought landing on `how_does_x_affect_y`.
        templates = template_rotation(self._template_step)
        self._template_step += 1
        grammar = build_proactive_grammar(aliases, templates)  # string work, before the lock
        prompt = build_proactive_prompt(aliased)

        raw = await self._runtime.infer_async(prompt, PROACTIVE_MAX_TOKENS, 0.0, grammar)
        insight = parse_proactive(raw, alias_to_id)
        if insight is None:
            return None
        # B18: the thought is a fact; asking the same open question again —
        # this cycle or a week of restarts later — reinforces it, never repeats it.
        node, created = await self._graph.upsert_fact(insight.spec)
        if not created:
            return None
        stored = replace(insight, node_id=node.id, label=node.label)
        self._thoughts += 1
        self.event_bus.publish(
            Event(
                event_type=EventType.INSIGHT_GENERATED,
                source="idle",
                payload={"insight": stored},
            )
        )
        self._last_at = stored.created_at
        return stored


# gen-ref: 52020876
