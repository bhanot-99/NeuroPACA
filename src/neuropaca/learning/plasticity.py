# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L4 · `BitNetPlasticity` — the learning module (Architecture.md §6, D-11).

Subscribes to `SIGNAL_CORRELATED`. For each signal it runs a cheap gate, and
only for what survives does it lazy-load the model and ask for one extractive
classification (`{cited_node_id, insight_category}` — never a sentence). A parsed
insight is stored as an `INSIGHT` node edged to its cited node, published on
`INSIGHT_GENERATED`, and buffered; co-occurring edges get a Hebbian bump.

Gate (drop, in order):
  1. `signal.confidence < 0.7`
  2. no `related_node_ids`, or none survives in the graph — nothing to attribute
  3. `BitNetRuntime.is_busy` — optional work yields to whatever holds the model
  4. repeat (B18) — every candidate the model could cite already has an insight
     for this signal type reinforced within `insight_refractory_minutes`; any
     answer would be a repeat. Read from the graph, so it survives restarts.
  5. model can't load / backend unavailable
  6. the model abstains or the output fails the validation gate (`rules.md §4.1`)
  7. repeat (B18) — the answer is a fact already stored: it is reinforced, not
     re-created, and not re-published (L5/L9 must not double-count it)

The single `infer_async` is the only heavy call and it runs in `BitNetRuntime`'s
dedicated executor, never on the loop.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.labels import LabelKind, LabelSpec
from neuropaca.core.models import Event, Node, system_error_event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.insight import L4_CATEGORIES, Insight
from neuropaca.learning.prompts import (
    INSIGHT_MAX_TOKENS,
    alias_nodes,
    build_insight_grammar,
    build_insight_prompt,
    parse_insight,
)

_log = logging.getLogger(__name__)

_CONFIDENCE_GATE = 0.7
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
        self._generated = 0
        self._dropped = 0
        # per-reason drop counters (observability — sum == _dropped)
        self._drops: dict[str, int] = {
            "confidence": 0,
            "no_nodes": 0,
            "busy": 0,
            "repeat": 0,
            "model": 0,
            "abstain": 0,
        }
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

    def _drop(self, reason: str) -> None:
        self._dropped += 1
        self._drops[reason] += 1

    async def _handle(self, signal: Signal) -> None:
        # (1-4) cheap gate — pure, no await
        if signal.confidence < _CONFIDENCE_GATE:
            self._drop("confidence")
            return
        if not signal.related_node_ids:
            self._drop("no_nodes")
            return
        if self._runtime.is_busy:
            self._drop("busy")
            return
        # distilled context — top-K cited candidates that still exist
        nodes = self._context_nodes(signal)
        if not nodes:
            self._drop("no_nodes")
            return
        if self._already_known(signal, nodes):
            self._drop("repeat")
            return

        # (5) lazy load — offloaded to the inference executor (rules.md §1)
        if not self._runtime.is_loaded:
            if self._runtime.backend_unavailable:
                self._drop("model")
                return
            if not await self._runtime.load_model_async():
                self._drop("model")
                self.event_bus.publish(
                    system_error_event(
                        module="learning",
                        exception="BitNet model failed to load — L4 inert",
                        severity="degraded",
                    )
                )
                return

        aliased = alias_nodes(nodes)
        aliases = [alias for alias, _ in aliased]
        alias_to_id = {alias: node.id for alias, node in aliased}
        prompt = build_insight_prompt(signal.signal_type, signal.confidence, aliased)
        grammar = build_insight_grammar(aliases)  # pure string work, before the lock

        # one greedy, grammar-constrained call
        raw = await self._runtime.infer_async(prompt, INSIGHT_MAX_TOKENS, 0.0, grammar)
        insight = parse_insight(
            raw,
            alias_to_id,
            source_signal=signal.signal_type,
            confidence=signal.confidence,
            snapshot_count=len(signal.source_snapshots),
        )
        if insight is None or not insight.traces_to_evidence():
            self._drop("abstain")
            return

        stored, created = await self._store_insight(insight, signal)
        if not created:
            self._drop("repeat")
            return
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
    def _already_known(self, signal: Signal, nodes: list[Node]) -> bool:
        """B18 pre-inference repeat gate. True only when *every* candidate the
        model could cite already carries an insight (any L4 category) for this
        signal type, reinforced within the refractory window — then any answer
        would be a stored fact, so the model call is skipped. Pure id lookups."""
        cutoff = datetime.now(UTC) - timedelta(minutes=self.config.insight_refractory_minutes)
        for node in nodes:
            fresh = False
            for category in L4_CATEGORIES:
                spec = LabelSpec(LabelKind.INSIGHT, (node.id,), f"{category}/{signal.signal_type}")
                known = self._graph.find_fact(spec)
                if known is not None and known.last_accessed >= cutoff:
                    fresh = True
                    break
            if not fresh:
                return False
        return True

    def _context_nodes(self, signal: Signal) -> list[Node]:
        found = [n for n in (self._graph.get_node(nid) for nid in signal.related_node_ids) if n]
        found.sort(key=lambda n: n.relevance_score, reverse=True)
        return found[:_CONTEXT_K]

    async def _store_insight(self, insight: Insight, signal: Signal) -> tuple[Insight, bool]:
        """Upsert the insight *fact* (B18 — node + `RELATED_TO` edges on create,
        reinforcement on a repeat), then the Hebbian co-occurrence update for the
        whole episode (cited nodes + the signal's other related nodes) in one
        `_lock` cycle (Architecture.md §6, D-11). A repeat still wires: the
        episode genuinely co-occurred again. Returns `(stored, created)`.

        A model-confirmed episode is stronger evidence of a real association
        than a bare focus co-activation, so it uses `wire_cooccurrence` (which
        also *creates* the pair edge the correlator never builds — T7) with the
        `hebbian_insight_multiplier` applied to the delta."""
        node, created = await self._graph.upsert_fact(insight.spec)
        episode = [*insight.cited_node_ids, *signal.related_node_ids]
        # the insight pipeline already bounds this set (cited <= _CONTEXT_K, the
        # signal's related ids are a pattern's own NodeSpecs) — reinforce all of
        # it, no extra truncation. The 50-citation loop-lag test is the ceiling.
        await self._graph.wire_cooccurrence(
            episode,
            delta=min(1.0, self.config.hebbian_delta * self.config.hebbian_insight_multiplier),
            max_episode=len(episode) or 1,
            max_new_edges=len(episode) or 1,
        )
        return replace(insight, node_id=node.id, label=node.label), created


# gen-ref: 7235dbfe
