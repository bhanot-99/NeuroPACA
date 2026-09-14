# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L9 · `VoiceConfirmationBridge` — voice as the "yes, confirm" answerer
(user decision 2026-09-14, VISION_PHASES.md A6.3/rules.md §5.2).

The gap this closes: `action/confirm.py`'s `ConfirmationBroker` was built to
be answered by `neuropaca confirm <id>` over the (now-removed) L9 CLI/socket.
Nothing replaced it, so every dangerous-tier voice command (`open_app`,
`adjust_volume`, `adjust_brightness`) was proposed, waited
`action_confirmation_timeout_seconds`, and auto-refused — real bug, seen
live 2026-09-14 (`data/actions.jsonl`: "refused: tier 'dangerous' is not
enabled" once the tier was on, "confirmation expired" is the shape it takes
once this module exists but nobody answers).

**Never calls `ConfirmationBroker` directly** (rules.md §0 — no module
imports another module's internals to call it; you want a new event, and
the broker already speaks pure events): this module only ever subscribes
`ACTION_CONFIRMATION_REQUEST` and publishes `ACTION_CONFIRMATION_RESPONSE`,
the exact same two events any future L9 surface would use.

**How the answer is heard.** When a request arrives, this module fires an
informational desktop notification (via `notify-send`, no buttons, nothing
trusted from it — see the note below) naming the action, then treats the
NEXT `VOICE_UTTERANCE_CAPTURED` text as a candidate answer, checked with a
strict, literal, exact-match-only comparison against a small fixed
vocabulary (rules.md's own risk table: "strict/literal final-parse rule,
never a loose one" for dangerous-tier confirmations). No fuzzy matching, no
substring/startswith checks, no "contains yes somewhere in a longer
sentence" — `"yes, confirm the following meeting"` does NOT match. An
utterance that matches NEITHER list is not a confirmation reply at all and
is left alone (VoiceIntentParser/VoiceCommandParser still see the same
event and classify it as a normal command, unaffected by this module).

**Why the notification carries no buttons.** `interface/notifier.py`'s own
docstring records a verified, machine-specific fact: `cosmic-notifications`
on this box fabricates `ActionInvoked` for ANY notification action within
seconds, with zero human interaction. Trusting a button click here — for a
DANGEROUS action's confirmation, the one place rules.md §5.2 is absolute —
would be worse than the CLI gap this module fixes: an automatic false
"yes" on every dangerous action, forever. The notification is display only;
the real answer only ever comes from a real transcribed utterance.

**Fail-closed, unchanged.** No matching utterance within
`action_confirmation_timeout_seconds` of the request still expires exactly
as `ConfirmationBroker.request()` already handles it — this module does
not extend or touch that timeout, it just gives the broker something to
match against sooner. A request this module never got to (module wasn't
running, notification tooling missing) times out exactly the same way it
already did before this module existed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from datetime import UTC, datetime, timedelta

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event

_log = logging.getLogger(__name__)

_APP_NAME = "NeuroPACA"

# Strict, literal, exact-match only (module docstring) — matched after
# stripping surrounding whitespace/trailing punctuation and lowercasing, but
# never as a substring or prefix check.
_APPROVE_PHRASES = frozenset({"yes", "confirm", "confirmed", "approve", "approved"})
_DENY_PHRASES = frozenset({"no", "cancel", "deny", "denied", "stop"})


def _normalize(text: str) -> str:
    return text.strip().rstrip(".!?,").strip().lower()


class VoiceConfirmationBridge(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        name: str = "voice_confirmation",
    ) -> None:
        super().__init__(name, event_bus, config)
        # request_id -> deadline. Purely local bookkeeping so this module
        # knows which utterance-arrival windows are live; the broker's own
        # timeout is the real, only source of truth for "too late" (module
        # docstring's fail-closed note).
        self._pending: dict[str, datetime] = {}
        self._prompted = 0
        self._approved = 0
        self._denied = 0
        self._ignored = 0
        self._errors = 0
        self._last_at: datetime | None = None

    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.ACTION_CONFIRMATION_REQUEST, self.on_request)
        self.event_bus.subscribe(EventType.VOICE_UTTERANCE_CAPTURED, self.on_utterance)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.ACTION_CONFIRMATION_REQUEST, self.on_request)
        self.event_bus.unsubscribe(EventType.VOICE_UTTERANCE_CAPTURED, self.on_utterance)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._prompted} prompted · {self._approved} approved · "
                f"{self._denied} denied · {self._ignored} ignored · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    async def on_request(self, event: Event) -> None:
        try:
            request_id = str(event.payload.get("request_id", ""))
            if not request_id:
                return
            summary = str(event.payload.get("summary", "a dangerous action"))
            self._pending[request_id] = datetime.now(UTC) + timedelta(
                seconds=self.config.action_confirmation_timeout_seconds
            )
            self._prompted += 1
            self._last_at = datetime.now(UTC)
            self._notify(summary)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("voice_confirmation on_request failed")
            self.event_bus.publish(
                system_error_event(module=self.name, exception=str(exc), severity="handler")
            )

    async def on_utterance(self, event: Event) -> None:
        try:
            text = event.payload.get("text")
            if not isinstance(text, str):
                return
            self._purge_expired()
            if not self._pending:
                return
            normalized = _normalize(text)
            if normalized in _APPROVE_PHRASES:
                self._respond(approved=True)
            elif normalized in _DENY_PHRASES:
                self._respond(approved=False)
            # Anything else: not a confirmation reply. Leave it alone —
            # VoiceIntentParser/VoiceCommandParser still process the same
            # event as an ordinary utterance (module docstring).
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("voice_confirmation on_utterance failed")
            self.event_bus.publish(
                system_error_event(module=self.name, exception=str(exc), severity="handler")
            )

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [rid for rid, deadline in self._pending.items() if deadline <= now]
        for rid in expired:
            del self._pending[rid]
            self._ignored += 1

    def _respond(self, *, approved: bool) -> None:
        # Oldest pending request first — first in, first answered, same
        # ordering a human confirming one at a time would produce.
        request_id = min(self._pending, key=lambda rid: self._pending[rid])
        del self._pending[request_id]
        if approved:
            self._approved += 1
        else:
            self._denied += 1
        self._last_at = datetime.now(UTC)
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_RESPONSE,
                source=self.name,
                payload={"request_id": request_id, "approved": approved},
            )
        )

    def _notify(self, summary: str) -> None:
        """Fire-and-forget, display only — never awaited, never trusted for
        a click (module docstring). Missing `notify-send` degrades to
        silence, same as `interface/notifier.py`'s own convention."""
        if shutil.which("notify-send") is None:
            return
        timeout_ms = int(self.config.action_confirmation_timeout_seconds * 1000)
        argv = [
            "notify-send",
            f"--app-name={_APP_NAME}",
            f"--expire-time={timeout_ms}",
            "NeuroPACA wants to confirm",
            (
                f'{summary}\nSay "yes" to confirm or "no" to cancel '
                f"within {int(self.config.action_confirmation_timeout_seconds)}s."
            ),
        ]

        async def _launch() -> None:
            try:
                await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                pass  # never fatal — the voice answer path works either way

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_launch())
