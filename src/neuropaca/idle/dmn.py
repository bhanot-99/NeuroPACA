"""L6 · `DefaultModeNetwork` — idle cognition (Architecture.md §8, B6, D-13).

When CPU drops (you walked away) L2 publishes `IDLE_DETECTED`; the DMN starts one
cancellable `idle_task`. The cycle has two halves:

- **Reminiscence** — graph housekeeping: merge exact-duplicate nodes, link
  orphans to `YOU`, prune stale / expired nodes. All of it runs through
  `GraphMemory`'s bounded-transaction workers (one lock per mutation, yield
  between), so `ACTIVITY_DETECTED` cancelling the task mid-cycle never leaves the
  graph half-mutated.
- **Imagination** — pull the top-K nodes by `relevance_score` and, up to
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
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Node, system_error_event
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    PROACTIVE_MAX_TOKENS,
    alias_nodes,
    build_proactive_grammar,
    build_proactive_prompt,
    parse_proactive,
)

_log = logging.getLogger(__name__)

_EXCLUDED_SEED_TYPES = frozenset({NodeType.INSIGHT, NodeType.IDLE_THOUGHT})


class DefaultModeNetwork(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
        *,
        clock: Clock | None = None,
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
        linked = await self._graph.link_orphan_nodes()
        ttl = timedelta(hours=self.config.dmn_idle_thought_ttl_hours)
        pruned = await self._graph.prune_stale_nodes(ttl)
        return f"merged {merged} · linked {linked} · pruned {pruned}"

    async def _imagination(self) -> int:
        """Returns the count made *this cycle* (for the summary line). The running
        `self._thoughts` tally is bumped in `_one_thought` as each thought lands,
        so a cycle abandoned mid-imagination (timeout / activity) still reports
        the thoughts it did produce."""
        budget = self.config.dmn_max_inferences_per_cycle
        if budget <= 0 or self._runtime.backend_unavailable:
            return 0
        seeds = self._top_nodes(self.config.dmn_top_k)
        if len(seeds) < 2:
            return 0  # nothing to relate — a follow-up question needs two nodes
        if not self._runtime.is_loaded and not await self._runtime.load_model_async():
            return 0

        seen: set[str] = set()
        made = 0
        for rotation in range(min(budget, len(seeds))):
            if self._runtime.is_busy:
                break
            if await self._one_thought(seeds, rotation, seen) is not None:
                made += 1
        return made

    def _top_nodes(self, k: int) -> list[Node]:
        """Top-K non-hub, non-thought nodes by `relevance_score`. A sync read —
        bounded dict work, inside the cycle's wall-clock budget."""
        return self._graph.top_nodes_by_score(k, exclude_types=_EXCLUDED_SEED_TYPES)

    async def _one_thought(
        self, seeds: list[Node], rotation: int, seen: set[str]
    ) -> Insight | None:
        ordered = seeds[rotation:] + seeds[:rotation]
        aliased = alias_nodes(ordered)
        aliases = [a for a, _ in aliased]
        alias_to_id = {a: n.id for a, n in aliased}
        alias_to_label = {a: n.label for a, n in aliased}
        grammar = build_proactive_grammar(aliases)  # pure string work, before the lock
        prompt = build_proactive_prompt(aliased)

        raw = await self._runtime.infer_async(prompt, PROACTIVE_MAX_TOKENS, 0.0, grammar)
        insight = parse_proactive(raw, alias_to_id, alias_to_label)
        if insight is None or insight.detail in seen:
            return None
        seen.add(insight.detail)

        stored = await self._store_thought(insight)
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

    async def _store_thought(self, insight: Insight) -> Insight:
        node_id = f"idle:{uuid4().hex[:12]}"
        await self._graph.upsert_node(node_id, NodeType.IDLE_THOUGHT, {"label": insight.detail})
        for cited_id in insight.cited_node_ids:
            await self._graph.add_edge(node_id, cited_id, RelationType.RELATED_TO)
        return replace(insight, node_id=node_id)
