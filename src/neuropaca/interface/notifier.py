# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""F2 · `NotificationDispatcher` — the only thing that actually shows a
`Moment` on screen (VISION_PHASES.md).

The terminal/CLI removal deleted `interface/desktop.py`, the only thing that
ever turned a `NotificationAction` into a real desktop popup —
`NotificationAction.execute()` (`action/actions.py`) does nothing but return
a string on purpose ("L9 owns delivery"). This module is L9's replacement
for exactly that one job, and nothing else: it does not decide *whether* to
speak (that's `drive/guardian.py`) or run any inference — it subscribes
`MOMENT_DELIVERED` only, and turns each one into a real notification.

**Delivery + feedback are one subprocess call**, not two channels:
`notify-send --wait --action=keep=Keep --action=dismiss=Dismiss` blocks
until the user clicks a button, closes the bubble, or our own timeout
(`moment.expires_at`) fires — so F2's three outcomes fall out for free:
`"keep"` on stdout -> **accepted**; anything else notify-send returns
(closed without a labelled click) -> **dismissed**; our own timeout with no
process return at all -> **ignored**, matching F2's own definition
("expired untouched"). `-t 0` disables the notification daemon's own
auto-hide so the *only* timeout in play is `moment.expires_at`.

Spike result (this session): `cosmic-notifications` advertises the
`actions` D-Bus capability, but two manual tests both hit the timeout with
no observed click. If it turns out this daemon never actually renders the
buttons, every delivered moment simply reads as `ignored` — a real, already
-handled F2 outcome, not a crash or a hang. A tray-menu fallback (F2's own
documented second path, "if no: the tray menu carries it") is a follow-up
if that turns out to be the case, not a blocker for this module.

`notify-send` missing entirely (e.g. a bare CI container) degrades the same
way: `ignored`, immediately, never raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment, system_error_event

_log = logging.getLogger(__name__)

_APP_NAME = "neuropaca"
_TITLE = "NeuroPACA"


class NotificationDispatcher(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("notifier", event_bus, config)
        self._clock: Clock = clock or SystemClock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._accepted = 0
        self._dismissed = 0
        self._ignored = 0
        self._errors = 0

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.MOMENT_DELIVERED, self.on_moment_delivered)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.MOMENT_DELIVERED, self.on_moment_delivered)
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tasks.clear()

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._accepted} accepted · {self._dismissed} dismissed · "
                f"{self._ignored} ignored · {self._errors} errors"
            ),
        )

    # ------------------------------------------------------------- delivery
    async def on_moment_delivered(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            moment = event.payload.get("moment")
            if not isinstance(moment, Moment):
                return
            task = asyncio.create_task(self._dispatch(moment))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def _dispatch(self, moment: Moment) -> None:
        try:
            outcome = await self._show(moment)
        except Exception as exc:
            self._on_error(exc)
            outcome = "ignored"
        if outcome == "accepted":
            self._accepted += 1
        elif outcome == "dismissed":
            self._dismissed += 1
        else:
            self._ignored += 1
        self.event_bus.publish(
            Event(
                event_type=EventType.MOMENT_FEEDBACK,
                source="notifier",
                payload={"moment": moment, "outcome": outcome},
            )
        )

    async def _show(self, moment: Moment) -> str:
        remaining = (moment.expires_at - self._clock.now()).total_seconds()
        if remaining <= 0.0:
            return "ignored"
        if shutil.which("notify-send") is None:
            return "ignored"
        argv = [
            "notify-send",
            f"--app-name={_APP_NAME}",
            "--expire-time=0",
            "--wait",
            "--action=keep=Keep",
            "--action=dismiss=Dismiss",
            _TITLE,
            moment.text,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        except OSError:
            return "ignored"
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=remaining)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            return "ignored"
        return "accepted" if stdout.decode(errors="replace").strip() == "keep" else "dismissed"

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("notifier handler failed")
        self.event_bus.publish(
            system_error_event(module="notifier", exception=str(exc), severity="handler")
        )
