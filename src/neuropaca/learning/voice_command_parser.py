# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.2 · `VoiceCommandParser` — turning classified voice intent into actions (VISION_PHASES.md).

Ties Tier 0 (pattern match), Tier 1 (span-pointer model call), and Tier 2
(app registry resolution) together. Subscribes to `VOICE_INTENT_CLASSIFIED`,
ignoring anything whose category is not `action_request`.

When a command is extracted:
- For `open`: resolves target against installed applications (Tier 2).
  - 1 match: publishes `ACTION_PROPOSAL` for `open_app` (dangerous tier — it
    runs a process, so L7 still pauses for a human confirmation).
  - 0 or 2+ matches: publishes `notification` proposal detailing ambiguity.
- For `increase`/`decrease`: publishes `adjust_volume` or `adjust_brightness`
  (dangerous tier, same reason).
- For `close` or other actions: safely dropped and surfaced via notification.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from neuropaca.action.app_registry import list_installed_apps, resolve_app_name
from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.learning.prompts import (
    VOICE_COMMAND_MAX_TOKENS,
    _tokenize_words,
    build_voice_command_grammar,
    build_voice_command_prompt,
    parse_voice_command,
)
from neuropaca.learning.voice_command import VoiceCommand, try_pattern_match

_log = logging.getLogger(__name__)


class VoiceCommandParser(BaseModule):
    """Orchestrates Tiers 0 -> 1 -> 2 voice command extraction and action proposals."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
        *,
        app_registry: dict[str, str] | None = None,
        name: str = "voice_command",
    ) -> None:
        super().__init__(name, event_bus, config)
        self._graph = graph_memory
        self._runtime = bitnet_runtime
        self._app_registry = app_registry
        self._parsed = 0
        self._tier0_hits = 0
        self._tier1_hits = 0
        self._proposed = 0
        self._ambiguous = 0
        self._drops: dict[str, int] = {
            "category": 0,
            "busy": 0,
            "model": 0,
            "abstain": 0,
            "no_words": 0,
        }
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.VOICE_INTENT_CLASSIFIED, self.on_intent_classified)
        if self._app_registry is None:
            # Globs XDG app directories and reads every .desktop file — blocking
            # I/O, offloaded per this codebase's convention (D-7 B3) so it
            # cannot stall the event dispatch loop during daemon startup.
            self._app_registry = await asyncio.to_thread(list_installed_apps)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.VOICE_INTENT_CLASSIFIED, self.on_intent_classified)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._parsed} parsed ({self._tier0_hits} pattern, {self._tier1_hits} model) · "
                f"{self._proposed} proposed · {self._ambiguous} ambiguous · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handler
    async def on_intent_classified(self, event: Event) -> None:
        try:
            category = event.payload.get("category")
            if category != "action_request":
                self._drops["category"] += 1
                return

            text = event.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return

            await self._handle_command(text.strip())
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("voice_command on_intent_classified failed")
            self.event_bus.publish(
                system_error_event(module=self.name, exception=str(exc), severity="handler")
            )

    async def _handle_command(self, text: str) -> None:
        # 1. Tier 0: deterministic pattern match
        cmd = try_pattern_match(text)
        if cmd is not None:
            self._tier0_hits += 1
        else:
            # 2. Tier 1: model-assisted span-pointer extraction
            cmd = await self._run_tier1_model(text)
            if cmd is None:
                return
            self._tier1_hits += 1

        self._parsed += 1
        self._last_at = datetime.now(UTC)
        await self._route_command(cmd, text)

    async def _run_tier1_model(self, text: str) -> VoiceCommand | None:
        if self._runtime.is_busy:
            self._drops["busy"] += 1
            return None
        if not self._runtime.interactive_configured:
            self._drops["model"] += 1
            return None
        if not self._runtime.interactive_loaded:
            if not await self._runtime.load_interactive_model_async():
                self._drops["model"] += 1
                return None

        words = _tokenize_words(text)
        if not words:
            self._drops["no_words"] += 1
            return None

        aliases = [f"w{i + 1}" for i in range(len(words))]
        prompt = build_voice_command_prompt(text, words)
        grammar = build_voice_command_grammar(aliases)

        raw = await self._runtime.infer_async(
            prompt, VOICE_COMMAND_MAX_TOKENS, 0.0, grammar, interactive=True
        )
        cmd = parse_voice_command(raw, words)
        if cmd is None:
            self._drops["abstain"] += 1
            return None
        return cmd

    async def _route_command(self, cmd: VoiceCommand, raw_text: str) -> None:
        # Tier 2 & Action proposals
        if cmd.action == "open":
            await self._handle_open_action(cmd, raw_text)
        elif cmd.action in ("increase", "decrease"):
            await self._handle_adjust_action(cmd, raw_text)
        elif cmd.action == "close":
            self._publish_notification(
                f"Heard '{raw_text}' — close app is reserved for dangerous tier, did nothing",
                reason="voice command: close app reserved",
                trigger=f"voice_command:{raw_text}",
            )
        elif cmd.action == "search":
            self._publish_notification(
                f"Heard '{raw_text}' — browser search is not yet enabled, did nothing",
                reason="voice command: search not configured",
                trigger=f"voice_command:{raw_text}",
            )

    async def _handle_open_action(self, cmd: VoiceCommand, raw_text: str) -> None:
        target = cmd.target.strip() if cmd.target else ""
        if not target:
            self._notify_ambiguity(raw_text, 0)
            return

        apps = self._app_registry if self._app_registry is not None else {}
        matches = resolve_app_name(target, apps)

        if len(matches) == 1:
            app_name, _score = matches[0]
            launch_cmd = apps[app_name]
            self._publish_proposal(
                "open_app",
                {"app_name": app_name, "launch_command": launch_cmd},
                reason=f"voice command: open {app_name}",
                trigger=f"voice_command:{raw_text}",
            )
        else:
            # 0 or 2+ matches -> surface ambiguity as notification (Step 0b)
            candidate_names = [m[0] for m in matches]
            self._notify_ambiguity(raw_text, len(matches), candidate_names)

    async def _handle_adjust_action(self, cmd: VoiceCommand, raw_text: str) -> None:
        target_str = (cmd.target or "").lower()
        if "brightness" in target_str:
            self._publish_proposal(
                "adjust_brightness",
                {"direction": cmd.action},
                reason=f"voice command: {cmd.action} brightness",
                trigger=f"voice_command:{raw_text}",
            )
        else:
            # Default to volume when target is "volume" or not explicitly brightness
            self._publish_proposal(
                "adjust_volume",
                {"direction": cmd.action},
                reason=f"voice command: {cmd.action} volume",
                trigger=f"voice_command:{raw_text}",
            )

    def _notify_ambiguity(
        self, text: str, count: int, candidate_names: list[str] | None = None
    ) -> None:
        self._ambiguous += 1
        if count == 0:
            msg = f"Heard '{text}' — found 0 matches, did nothing"
        else:
            candidates = candidate_names or []
            sample = ", ".join(candidates[:3])
            msg = f"Heard '{text}' — found {count} matches ({sample}), did nothing"
        self._publish_notification(
            msg,
            reason="ambiguous voice command",
            trigger=f"voice_command:{text}",
        )

    def _publish_notification(self, message: str, *, reason: str, trigger: str = "") -> None:
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source=self.name,
                payload={
                    "proposal_id": uuid4().hex[:12],
                    "action_type": "notification",
                    "kwargs": {"text": message},
                    "reason": reason,
                    "trigger": trigger or f"voice_command:{self.name}",
                },
            )
        )

    def _publish_proposal(
        self,
        action_type: str,
        kwargs: dict[str, Any],
        *,
        reason: str,
        trigger: str,
    ) -> None:
        self._proposed += 1
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source=self.name,
                payload={
                    "proposal_id": uuid4().hex[:12],
                    "action_type": action_type,
                    "kwargs": kwargs,
                    "reason": reason,
                    "trigger": trigger,
                },
            )
        )
