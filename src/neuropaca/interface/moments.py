# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A0 · `MomentComposer` — the welcome-back moment (VISION_PHASES.md, F1).

You come back to your laptop; one line greets you — what it wondered about
while you were away, and what you were in the middle of. Both halves are
composed from what already exists: L6's idle thoughts (`INSIGHT_GENERATED`,
`source="idle"`) and the last `APP_SWITCH` before the spell opened.

One idle spell, one moment, at most: `IDLE_DETECTED` opens a spell and clears
the thought list; every idle-sourced `INSIGHT_GENERATED` while it is open is
collected; `ACTIVITY_DETECTED` closes it and — if the spell was long enough and
the daily cap allows — composes a `Moment` from fixed templates only, picking
the highest-relevance thought (ties -> newest). Each half is dropped
independently when it has nothing grounded to say; a spell with no thought
still yields the "you were in..." line, and a spell with neither produces
silence, never a hollow greeting.

Until A3 (the guardian) exists, a proposed moment is delivered straight away as
an `ACTION_PROPOSAL` `notification` — the same publisher-agnostic path L8 uses
(Architecture.md §11b, D-16): a description only, never a live action; L7
instantiates and gates it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from uuid import uuid4

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment, system_error_event
from neuropaca.diagnosis.app_identity import AppIdentity
from neuropaca.learning.insight import Insight

_log = logging.getLogger(__name__)

# A held moment is dropped after this (F1) — moot in A0 (delivered straight
# through), kept short so a future holder (A3) never sits on a stale greeting.
_MOMENT_EXPIRES_MINUTES = 10


class MomentComposer(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        *,
        clock: Clock | None = None,
        identity: AppIdentity | None = None,
    ) -> None:
        super().__init__("moments", event_bus, config)
        self._graph = graph_memory
        self._clock: Clock = clock or SystemClock()
        self._identity_path = config.app_identity_path
        self._identity = identity if identity is not None else AppIdentity.empty()
        self._spell_open = False
        self._spell_thoughts: list[Insight] = []
        self._last_focus_id: str | None = None
        self._cap_day: date | None = None
        self._proposed_today = 0
        self._proposed = 0
        self._dropped_below_min = 0
        self._dropped_ungrounded = 0
        self._dropped_capped = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        if self._identity.alias_count == 0:
            self._identity = AppIdentity.from_file(self._identity_path)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.unsubscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._proposed} proposed · {self._dropped_below_min} below-min · "
                f"{self._dropped_ungrounded} ungrounded · {self._dropped_capped} capped · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_idle_detected(self, _event: Event) -> None:
        """Open a spell. A still-open spell (should not happen — L2 emits one
        IDLE_DETECTED per transition) restarts the collection rather than
        compound it."""
        try:
            if not self.is_running:
                return
            self._spell_open = True
            self._spell_thoughts = []
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_app_switch(self, event: Event) -> None:
        """Remember the focused node so a spell that opens right after has
        something to greet you back into. Not spell-gated — the switch that
        matters is the one right *before* you went idle."""
        try:
            if not self.is_running:
                return
            payload = event.payload
            webapp = payload.get("webapp")
            if isinstance(webapp, str) and webapp:
                self._last_focus_id = f"webapp:{webapp}"
                return
            app_id = payload.get("app_id")
            if isinstance(app_id, str) and app_id:
                key = self._identity.resolve(app_id) or app_id
                self._last_focus_id = f"app:{key}"
        except Exception as exc:
            self._on_error(exc)

    async def on_insight_generated(self, event: Event) -> None:
        """Collect this spell's idle thoughts. Only `source="idle"` — L4's
        signal-correlation insights are a different kind of thing and were
        never part of what the DMN produced while you were away."""
        try:
            if not self.is_running or not self._spell_open:
                return
            if event.source != "idle":
                return
            insight = event.payload.get("insight")
            if isinstance(insight, Insight):
                self._spell_thoughts.append(insight)
        except Exception as exc:
            self._on_error(exc)

    async def on_activity_detected(self, event: Event) -> None:
        """Close the spell and compose, if it's worth it. `idle_seconds` comes
        straight off the payload (Architecture.md L2) — no separate timer kept."""
        try:
            if not self.is_running or not self._spell_open:
                return
            self._spell_open = False
            thoughts, self._spell_thoughts = self._spell_thoughts, []
            idle_seconds = float(event.payload.get("idle_seconds", 0.0))
            if idle_seconds < self.config.welcome_min_idle_minutes * 60.0:
                self._dropped_below_min += 1
                return
            await self._compose(thoughts, idle_seconds)
        except Exception as exc:
            self._on_error(exc)

    # --------------------------------------------------------------- compose
    async def _compose(self, thoughts: list[Insight], idle_seconds: float) -> None:
        if not self.config.welcome_enabled:
            return
        today = self._clock.now().date()
        if today != self._cap_day:
            self._cap_day = today
            self._proposed_today = 0
        if self._proposed_today >= self.config.welcome_daily_cap:
            self._dropped_capped += 1
            return

        parts: list[str] = []
        evidence: list[str] = []

        thought = self._best_thought(thoughts)
        if thought is not None and self._graph.has_node(thought.node_id):
            parts.append(f'While you were away I wondered: "{thought.label}"')
            evidence.append(thought.node_id)

        focus_line = self._focus_line()
        if focus_line is not None:
            text, node_id = focus_line
            parts.append(text)
            evidence.append(node_id)

        if not parts:
            # Grounding check (F1): nothing in this spell resolves to a live
            # node — silence, never a hollow "Welcome back." on its own.
            self._dropped_ungrounded += 1
            return

        text = "Welcome back. " + " ".join(parts)
        now = self._clock.now()
        moment = Moment(
            kind="welcome_back",
            text=text,
            evidence=tuple(evidence),
            value=1.0,
            context={"idle_minutes": round(idle_seconds / 60.0, 1), "hour": now.hour},
            expires_at=now + timedelta(minutes=_MOMENT_EXPIRES_MINUTES),
        )
        self._proposed_today += 1
        self._proposed += 1
        self._last_at = now
        self.event_bus.publish(
            Event(
                event_type=EventType.MOMENT_PROPOSED,
                source="moments",
                payload={"moment": moment},
            )
        )
        # A3 does not exist yet — deliver straight through as a `notification`
        # ACTION_PROPOSAL, the same description-only path L8 uses (D-16). L7
        # owns the class registry, the gate, and the audit log; nothing here
        # can bypass any of them.
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="moments",
                payload={
                    "proposal_id": uuid4().hex[:12],
                    "action_type": "notification",
                    "kwargs": {"text": text, "node_ids": list(evidence)},
                    "reason": "welcome back",
                    "trigger": "welcome_back",
                },
            )
        )

    def _best_thought(self, thoughts: list[Insight]) -> Insight | None:
        """Highest relevance-scored node behind a thought; ties -> newest."""
        best: Insight | None = None
        best_key: tuple[float, object] | None = None
        for thought in thoughts:
            node = self._graph.get_node(thought.node_id)
            score = node.relevance_score if node is not None else 0.0
            key = (score, thought.created_at)
            if best_key is None or key > best_key:
                best, best_key = thought, key
        return best

    def _focus_line(self) -> tuple[str, str] | None:
        if self._last_focus_id is None:
            return None
        node = self._graph.get_node(self._last_focus_id)
        if node is None:
            return None
        return f"You were in {node.label}.", self._last_focus_id

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("moments handler failed")
        self.event_bus.publish(
            system_error_event(module="moments", exception=str(exc), severity="handler")
        )


# gen-ref: 9b172a86
