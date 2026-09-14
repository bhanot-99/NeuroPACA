# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.1 · `VoiceIntentParser` — the voice-intent classifier (VISION_PHASES.md).

Subscribes to `VOICE_UTTERANCE_CAPTURED` (published by `plugins/voice/
voice_plugin.py` via `PluginHost`). For each utterance it runs a cheap gate,
then asks the **interactive** backend (D-12 — the model that fills
`BitNetRuntime`'s `interactive_model_path` slot, dormant since the terminal/
CLI's removal, not the always-on loop model L4/L6 share) for one extractive
classification (`{cited_node_id, voice_intent}` — never a sentence, never an
open-ended "target"). The result enriches the utterance's own `PLUGIN_FACT`
in place — same `(kind, subject)` key the plugin already wrote, so
`EpisodeStore.assert_fact`'s ordinary supersession closes the raw-only fact
and opens an enriched one; nothing is lost, `at(t)` before this still returns
the original (VISION_PHASES.md's bi-temporal guarantee, unchanged).

Gate (drop, in order), mirroring L4's `BitNetPlasticity`:
  1. no non-hub graph nodes exist yet to offer as candidates — nothing to cite
  2. `BitNetRuntime.is_busy` — yields to whatever holds the model
  3. the interactive backend isn't configured, or fails to lazy-load
  4. the model's output fails the validation gate (`rules.md §4.1`)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.learning.prompts import (
    VOICE_INTENT_MAX_TOKENS,
    alias_nodes,
    build_voice_intent_grammar,
    build_voice_intent_prompt,
    parse_voice_intent,
)

_log = logging.getLogger(__name__)

_CONTEXT_K = 5  # top-K candidate nodes offered to the model, same as L4


class VoiceIntentParser(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
        *,
        episode_store: EpisodeStore | None = None,
        name: str = "voice_intent",
    ) -> None:
        super().__init__(name, event_bus, config)
        self._graph = graph_memory
        self._runtime = bitnet_runtime
        self._store = episode_store
        self._classified = 0
        self._dropped = 0
        self._drops: dict[str, int] = {
            "no_nodes": 0,
            "busy": 0,
            "model": 0,
            "abstain": 0,
        }
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.VOICE_UTTERANCE_CAPTURED, self.on_utterance_event)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.VOICE_UTTERANCE_CAPTURED, self.on_utterance_event)

    def health(self) -> ModuleHealth:
        if self._runtime.interactive_loaded:
            model = "loaded"
        elif not self._runtime.interactive_configured:
            model = "unconfigured"
        else:
            model = "lazy"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"interactive model {model} · {self._classified} classified · "
                f"{self._dropped} dropped · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handler
    async def on_utterance_event(self, event: Event) -> None:
        try:
            entity_id = event.payload.get("entity_id")
            text = event.payload.get("text")
            if isinstance(entity_id, str) and isinstance(text, str):
                await self._handle(entity_id, text)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("voice_intent on_utterance_event failed")
            self.event_bus.publish(
                system_error_event(module=self.name, exception=str(exc), severity="handler")
            )

    def _drop(self, reason: str) -> None:
        self._dropped += 1
        self._drops[reason] += 1

    async def _handle(self, entity_id: str, text: str) -> None:
        if self._runtime.is_busy:
            self._drop("busy")
            return

        nodes = self._graph.top_nodes_by_score(_CONTEXT_K)
        if not nodes:
            self._drop("no_nodes")
            return

        if not self._runtime.interactive_configured:
            self._drop("model")
            return
        if not self._runtime.interactive_loaded:
            if not await self._runtime.load_interactive_model_async():
                self._drop("model")
                return

        aliased = alias_nodes(nodes)
        aliases = [alias for alias, _ in aliased]
        alias_to_id = {alias: node.id for alias, node in aliased}
        prompt = build_voice_intent_prompt(text, aliased)
        grammar = build_voice_intent_grammar(aliases)  # pure string work, before the lock

        raw = await self._runtime.infer_async(
            prompt, VOICE_INTENT_MAX_TOKENS, 0.0, grammar, interactive=True
        )
        intent = parse_voice_intent(raw, alias_to_id, raw_text=text)
        if intent is None:
            self._drop("abstain")
            return

        self._store_intent(entity_id, intent.category, intent.cited_node_id, text)
        self.event_bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source=self.name,
                payload={
                    "entity_id": entity_id,
                    "text": text,
                    "category": intent.category,
                },
            )
        )
        self._classified += 1
        self._last_at = datetime.now(UTC)

    def _store_intent(
        self, entity_id: str, category: str, cited_node_id: str | None, text: str
    ) -> None:
        """Enrich the utterance's own fact in place — same subject the plugin
        already wrote under, so this supersedes it (module docstring)."""
        if self._store is None:
            return
        attrs: dict[str, object] = {"source": "typed", "voice_intent": category}
        if cited_node_id is not None:
            # Persisted for scripts/eval_voice_a6_1_review.py's offline citation-
            # accuracy report — the live pipeline (VOICE_INTENT_CLASSIFIED, the
            # briefing) never reads it back. Known limitation, not dead code.
            attrs["cited_node_id"] = cited_node_id
        self._store.assert_fact(
            EpisodeKind.PLUGIN_FACT,
            entity_id,
            text,
            valid_from=datetime.now(UTC),
            source=self.name,
            attrs=attrs,
        )


# gen-ref: 315dfe73
