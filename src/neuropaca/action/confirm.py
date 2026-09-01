"""L7 · the confirmation handshake (rules.md §5.2, D-14).

The rule is absolute — *dangerous actions require terminal confirmation at
execution time; no pressure level and no config flag removes this* — and the
daemon has no terminal. `neuropacad` runs headless under systemd; the human's
terminal belongs to a separate `neuropaca` CLI process that may not even be
running.

So the confirmation is a **handshake over the bus**, and it is built to fail
closed at every step:

1. L7 pauses the action and publishes `ACTION_CONFIRMATION_REQUEST` carrying a
   `request_id` and a plain-language summary of the exact effect.
2. L9 relays it to whichever terminal asks (`neuropaca confirmations`).
3. The human answers (`neuropaca confirm <id>` / `--deny`), L9 publishes
   `ACTION_CONFIRMATION_RESPONSE` with that `request_id`.
4. No matching response inside `action_confirmation_timeout_seconds` — nobody
   watching, CLI never started, daemon restarted — is a **refusal**.

A response whose `request_id` is unknown or already resolved is dropped. There is
no "approve all", no default-yes, and no way to pre-approve: a fresh request is
published for every single dangerous attempt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """One paused action, as shown in the human's terminal."""

    request_id: str
    action: str
    tier: str
    summary: str
    reason: str
    requested_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "tier": self.tier,
            "summary": self.summary,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
        }


class ConfirmationBroker:
    """Publishes requests, matches responses, and refuses on timeout."""

    def __init__(self, event_bus: EventBus, timeout_seconds: float) -> None:
        self._event_bus = event_bus
        self._timeout = float(timeout_seconds)
        self._waiters: dict[str, asyncio.Future[bool]] = {}
        self._pending: dict[str, PendingConfirmation] = {}
        self._approved = 0
        self._denied = 0
        self._expired = 0

    @property
    def pending(self) -> tuple[PendingConfirmation, ...]:
        return tuple(self._pending.values())

    @property
    def counters(self) -> tuple[int, int, int]:
        """(approved, denied, expired)."""
        return (self._approved, self._denied, self._expired)

    async def request(
        self, *, action: str, tier: str, summary: str, reason: str
    ) -> tuple[str, bool]:
        """Ask, and wait. Returns `(request_id, approved)`; expiry is `False`."""
        request_id = uuid4().hex[:12]
        pending = PendingConfirmation(
            request_id=request_id,
            action=action,
            tier=tier,
            summary=summary,
            reason=reason,
            requested_at=datetime.now(UTC),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._waiters[request_id] = future
        self._pending[request_id] = pending

        self._event_bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_REQUEST,
                source="action",
                priority=10,
                payload={"confirmation": pending, **pending.as_dict()},
            )
        )
        _log.warning(
            "L7 awaiting confirmation %s for %s (%ss): %s",
            request_id,
            action,
            self._timeout,
            summary,
        )
        try:
            approved = await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            self._expired += 1
            _log.warning("L7 confirmation %s expired — refusing %s", request_id, action)
            return (request_id, False)
        finally:
            self._waiters.pop(request_id, None)
            self._pending.pop(request_id, None)
        if approved:
            self._approved += 1
        else:
            self._denied += 1
        return (request_id, approved)

    async def on_response(self, event: Event) -> None:
        """Bus handler for `ACTION_CONFIRMATION_RESPONSE`. Never raises."""
        request_id = str(event.payload.get("request_id", ""))
        future = self._waiters.get(request_id)
        if future is None or future.done():
            _log.debug("L7 dropped confirmation response for unknown id %r", request_id)
            return
        future.set_result(bool(event.payload.get("approved", False)))
