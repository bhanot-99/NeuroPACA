"""L4 · `BitNetPlasticity` — the learning module (Architecture.md §6, D-11).

Subscribes to `SIGNAL_CORRELATED`. For each signal it runs a cheap gate, and
only for what survives does it lazy-load the model and ask for one extractive
classification (`{cited_node_id, insight_category}` — never a sentence). A parsed
insight is stored as an `INSIGHT` node edged to its cited node, published on
`INSIGHT_GENERATED`, and buffered; co-occurring edges get a Hebbian bump.

Gate (drop, in order):
  1. `signal.confidence < 0.7`
  2. no `related_node_ids` — nothing to attribute
  3. `BitNetRuntime.is_busy` — optional work yields to whatever holds the model
  4. Jaccard(this signal's node set, any buffered signal's) > 0.8 — not novel
  5. model can't load / backend unavailable
  6. no cited candidate node survives in the graph
  7. the model abstains or the output fails the validation gate (`rules.md §4.1`)

The single `infer_async` is the only heavy call and it runs in `BitNetRuntime`'s
dedicated executor, never on the loop.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Node, system_error_event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    INSIGHT_MAX_TOKENS,
    alias_nodes,
    build_insight_grammar,
    build_insight_prompt,
    parse_insight,
)

_log = logging.getLogger(__name__)

_CONFIDENCE_GATE = 0.7
_NOVELTY_GATE = 0.8  # Jaccard above this -> too similar to a recent signal
_HEBBIAN_DELTA = 0.01
_CONTEXT_K = 5  # top-K cited candidates offered to the model


class BitNetPlasticity(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
    ) -> None:
        super().__init__("learning", event_bus, config)
        self._graph = graph_memory
        self._runtime = bitnet_runtime
        self._buffer: deque[tuple[Signal, Insight]] = deque(maxlen=config.adaptation_buffer_size)
        self._generated = 0
        self._dropped = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.SIGNAL_CORRELATED, self.on_signal_event)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.SIGNAL_CORRELATED, self.on_signal_event)

    def health(self) -> ModuleHealth:
        if self._runtime.is_loaded:
            model = "loaded"
        elif self._runtime.backend_unavailable:
            model = "unavailable"
        else:
            model = "lazy"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"model {model} · {self._generated} insights · "
                f"{self._dropped} dropped · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handler
    async def on_signal_event(self, event: Event) -> None:
        try:
            signal = event.payload.get("signal")
            if isinstance(signal, Signal):
                await self._handle(signal)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("learning on_signal_event failed")
            self.event_bus.publish(
                system_error_event(module="learning", exception=str(exc), severity="handler")
            )

    async def _handle(self, signal: Signal) -> None:
        # (1-4) cheap gate — pure, no await
        if signal.confidence < _CONFIDENCE_GATE:
            self._dropped += 1
            return
        if not signal.related_node_ids:
            self._dropped += 1
            return
        if self._runtime.is_busy:
            self._dropped += 1
            return
        if self._too_similar(signal):
            self._dropped += 1
            return

        # (5) lazy load — offloaded to the inference executor (rules.md §1)
        if not self._runtime.is_loaded:
            if self._runtime.backend_unavailable:
                self._dropped += 1
                return
            if not await self._runtime.load_model_async():
                self._dropped += 1
                self.event_bus.publish(
                    system_error_event(
                        module="learning",
                        exception="BitNet model failed to load — L4 inert",
                        severity="degraded",
                    )
                )
                return

        # (6) distilled context — top-K cited candidates that still exist
        nodes = self._context_nodes(signal)
        if not nodes:
            self._dropped += 1
            return
        aliased = alias_nodes(nodes)
        aliases = [alias for alias, _ in aliased]
        alias_to_id = {alias: node.id for alias, node in aliased}
        prompt = build_insight_prompt(signal.signal_type, signal.confidence, aliased)
        grammar = build_insight_grammar(aliases)  # pure string work, before the lock

        # (7) one greedy, grammar-constrained call
        raw = await self._runtime.infer_async(prompt, INSIGHT_MAX_TOKENS, 0.0, grammar)
        insight = parse_insight(
            raw,
            alias_to_id,
            source_signal=signal.signal_type,
            confidence=signal.confidence,
            snapshot_count=len(signal.source_snapshots),
        )
        if insight is None or not insight.traces_to_evidence():
            self._dropped += 1
            return

        stored = await self._store(insight)
        await self._reinforce(stored, signal)
        self._buffer.append((signal, stored))
        self.event_bus.publish(
            Event(
                event_type=EventType.INSIGHT_GENERATED,
                source="learning",
                payload={"insight": stored},
            )
        )
        self._generated += 1
        self._last_at = stored.created_at

    # --------------------------------------------------------------- helpers
    def _too_similar(self, signal: Signal) -> bool:
        current = set(signal.related_node_ids)
        for past_signal, _ in self._buffer:
            past = set(past_signal.related_node_ids)
            union = current | past
            if union and len(current & past) / len(union) > _NOVELTY_GATE:
                return True
        return False

    def _context_nodes(self, signal: Signal) -> list[Node]:
        found = [n for n in (self._graph.get_node(nid) for nid in signal.related_node_ids) if n]
        found.sort(key=lambda n: n.relevance_score, reverse=True)
        return found[:_CONTEXT_K]

    async def _store(self, insight: Insight) -> Insight:
        node_id = f"insight:{uuid4().hex[:12]}"
        await self._graph.upsert_node(node_id, NodeType.INSIGHT, {"label": insight.summary})
        for cited_id in insight.cited_node_ids:
            await self._graph.add_edge(node_id, cited_id, RelationType.RELATED_TO)
        return replace(insight, node_id=node_id)

    async def _reinforce(self, insight: Insight, signal: Signal) -> None:
        cited = set(insight.cited_node_ids)
        others = [nid for nid in signal.related_node_ids if nid not in cited]
        for cited_id in cited:
            for other_id in others:
                await self._graph.reinforce_edge(cited_id, other_id, _HEBBIAN_DELTA)
