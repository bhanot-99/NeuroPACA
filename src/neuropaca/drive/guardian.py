# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A3 · `Guardian` — should it speak, and now? (VISION.md §3.6, VISION_PHASES.md).

Every other proposer (A0's `MomentComposer`, A2's `MirrorComposer`, S0's
`BriefingComposer`) publishes `MOMENT_PROPOSED` and stops — the "straight
through `ACTION_PROPOSAL` until A3 exists" comment in all three is gone,
replaced by this module. `Guardian` is now the only thing that turns a
proposed `Moment` into an actual notification `ACTION_PROPOSAL`, and the
only publisher of `MOMENT_DELIVERED`.

**The decision** is Beta-Bernoulli Thompson sampling per `(moment.kind,
bucket)` arm, `bucket = focus x hour x recent_dismissals` (36 per kind):

- `focus`: `focused` (mid-session — always **held**, never sampled: "a
  context override, not a cold-start fix"), `just_ended` (within
  `guardian_just_ended_minutes` of the last `ACTIVITY_DETECTED` — this is
  exactly when A0's welcome-back fires), or `normal` (currently idle and
  not `just_ended`). Derived from `IDLE_DETECTED`/`ACTIVITY_DETECTED` only —
  the same two events `core/presence_tracker.py` already uses for the same
  purpose, no new sensing.
- `hour`: night/morning/afternoon/evening from the wall-clock hour.
- `recent_dismissals`: 0/1/2+ dismissals across *any* kind in the trailing
  24 h — context, not per-arm state.

Below `guardian_burn_in_n` observations an arm uses its deterministic
posterior mean instead of a sample (the cold-start burn-in, VISION.md §3.6);
at or above it, a real Thompson draw. `interrupt_cost_focus` /
`interrupt_cost_normal` are the only two cost tiers the design names —
`just_ended` shares `interrupt_cost_normal`; the 3-way focus split is for
which arm learns, not a third cost. A daily budget (`nudge_daily_budget`)
caps deliveries; exhausting it drops the rest of the day's moments the same
as a losing sample — no re-queue, since a coin flip that came up "no" needs
no memory. `focused`-held moments are the only ones queued (bounded, each
carrying the proposer's own `expires_at`) and re-evaluated in full, in
order, the instant `IDLE_DETECTED` ends the session.

**Posteriors persist** across restarts via `EpisodeStore.assert_fact`
(optional — `episode_store=None` still works, just without cross-restart
memory, same optionality `DefaultModeNetwork` already has): one fact per
arm, `subject="guardian:<kind>:<bucket>"`, replaced whole on every touch.
Rehydrated once at `initialize()` from whatever facts are open right now
(`EpisodeStore.at(now)` — every `GUARDIAN_POSTERIOR` row is open-ended until
superseded, so "at now" is "the latest of everything").

**Correlating feedback back to the arm** that earned it needs no separate
in-flight table: the delivered `Moment` published on `MOMENT_DELIVERED` (and
therefore the one that comes back on `MOMENT_FEEDBACK` — `interface/
notifier.py` only ever echoes what it was handed) carries the bucket baked
into its own `context` dict (`dataclasses.replace`, since `Moment` is
frozen) — `moment.context["focus_bucket"]` etc. — so `on_moment_feedback`
recomputes the exact same key the delivery decision used, however long ago
that was.

Decay (`guardian_decay`, daily) is lazy and on-touch, the same idiom
`drive/pressure.py` already uses for its own decay: every read (a new
proposal) or write (feedback) first applies `gamma ** days_elapsed` to
`(a, b)` from the arm's own `updated_at`. `n` — the burn-in counter — never
decays; it counts real observations, not belief mass.
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from uuid import uuid4

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment, system_error_event

_log = logging.getLogger(__name__)

# Beta(1, 3) — a reasoned conservative prior (VISION.md §3.6's own example):
# a fresh arm starts believing a moment is unwelcome more often than not.
_PRIOR_A = 1.0
_PRIOR_B = 3.0
# "ignored -> a small b increment" (VISION.md §3.6) — smaller than a real
# dismissal (b += 1): silence is weaker evidence than an active wave-away.
_IGNORED_B_INCREMENT = 0.2
_DISMISSAL_WINDOW = timedelta(hours=24)
_HELD_QUEUE_MAXSIZE = 20


def hour_bucket(hour: int) -> str:
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def dismissal_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    return "2+"


@dataclass(slots=True)
class _Arm:
    """The mutable interior of one (kind, bucket)'s Beta posterior. Never
    handed out — callers go through `Guardian`'s decision methods."""

    a: float
    b: float
    n: int
    updated_at: datetime


class Guardian(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        episode_store: EpisodeStore | None = None,
    ) -> None:
        super().__init__("guardian", event_bus, config)
        self._clock: Clock = clock or SystemClock()
        self._rng = rng if rng is not None else random.Random()
        self._episodes = episode_store
        self._arms: dict[tuple[str, str], _Arm] = {}
        self._idle_since: datetime | None = None
        self._resumed_at: datetime | None = None
        self._recent_dismissals: deque[datetime] = deque()
        self._held: deque[Moment] = deque()
        self._budget_day: date | None = None
        self._budget_used = 0
        self._delivered = 0
        self._held_count = 0
        self._held_dropped_full = 0
        self._held_expired = 0
        self._dropped_sample = 0
        self._dropped_budget = 0
        self._feedback_seen = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        if self._episodes is not None:
            await self._rehydrate()
        self.event_bus.subscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        self.event_bus.subscribe(EventType.MOMENT_FEEDBACK, self.on_moment_feedback)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        self.event_bus.unsubscribe(EventType.MOMENT_FEEDBACK, self.on_moment_feedback)
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._delivered} delivered · {self._held_count} held · "
                f"{self._dropped_sample} dropped-sample · {self._dropped_budget} "
                f"dropped-budget · {self._held_expired} held-expired · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- focus signal
    async def on_idle_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = self._clock.now()
            self._resumed_at = None
            await self._reevaluate_held()
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_activity_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = None
            self._resumed_at = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    def _focus_bucket(self, now: datetime) -> str:
        """Unknown state (nothing seen yet, right after startup) reads as
        `focused` — the conservative default: never assume it is fine to
        interrupt when there is no signal either way."""
        if self._idle_since is not None:
            return "normal"
        if self._resumed_at is not None:
            just_ended_window = timedelta(minutes=self.config.guardian_just_ended_minutes)
            if now - self._resumed_at < just_ended_window:
                return "just_ended"
        return "focused"

    def _dismissal_bucket(self, now: datetime) -> str:
        cutoff = now - _DISMISSAL_WINDOW
        while self._recent_dismissals and self._recent_dismissals[0] < cutoff:
            self._recent_dismissals.popleft()
        return dismissal_bucket(len(self._recent_dismissals))

    def _bucket_key(self, now: datetime) -> str:
        return f"{self._focus_bucket(now)}|{hour_bucket(now.hour)}|{self._dismissal_bucket(now)}"

    # -------------------------------------------------------------- moments
    async def on_moment_proposed(self, event: Event) -> None:
        try:
            if not self.is_running or not self.config.guardian_enabled:
                return
            moment = event.payload.get("moment")
            if not isinstance(moment, Moment):
                return
            now = self._clock.now()
            if self._focus_bucket(now) == "focused":
                self._hold(moment)
                return
            await self._decide_and_maybe_deliver(moment, now)
        except Exception as exc:
            self._on_error(exc)

    def _hold(self, moment: Moment) -> None:
        if len(self._held) >= _HELD_QUEUE_MAXSIZE:
            self._held_dropped_full += 1
            return
        self._held.append(moment)
        self._held_count += 1

    async def _reevaluate_held(self) -> None:
        """The session just ended (`IDLE_DETECTED`) — try every held moment,
        oldest first, dropping whatever has already expired."""
        pending, self._held = self._held, deque()
        now = self._clock.now()
        for moment in pending:
            if moment.expires_at <= now:
                self._held_expired += 1
                continue
            await self._decide_and_maybe_deliver(moment, now)

    def _p_hat(self, arm: _Arm) -> float:
        """VISION.md §3.6's burn-in: the deterministic posterior mean below
        `guardian_burn_in_n` observations, a real Thompson draw at or above
        it."""
        if arm.n < self.config.guardian_burn_in_n:
            return arm.a / (arm.a + arm.b)
        return self._rng.betavariate(arm.a, arm.b)

    async def _decide_and_maybe_deliver(self, moment: Moment, now: datetime) -> None:
        bucket = self._bucket_key(now)
        arm = self._touch_arm(moment.kind, bucket, now)
        cost = (
            self.config.interrupt_cost_focus
            if self._focus_bucket(now) == "focused"
            else self.config.interrupt_cost_normal
        )
        if self._p_hat(arm) * moment.value <= cost:
            self._dropped_sample += 1
            return
        if self._budget_used >= self.config.nudge_daily_budget:
            self._dropped_budget += 1
            return
        self._spend_budget(now)
        await self._deliver(moment, bucket, now)

    def _spend_budget(self, now: datetime) -> None:
        today = now.date()
        if self._budget_day != today:
            self._budget_day = today
            self._budget_used = 0
        self._budget_used += 1

    async def _deliver(self, moment: Moment, bucket: str, now: datetime) -> None:
        focus, hour, dismiss = bucket.split("|", 2)
        enriched = replace(
            moment,
            context={
                **moment.context,
                "focus_bucket": focus,
                "hour_bucket": hour,
                "dismissal_bucket": dismiss,
            },
        )
        self._delivered += 1
        self._last_at = now
        self.event_bus.publish(
            Event(
                event_type=EventType.MOMENT_DELIVERED,
                source="guardian",
                payload={"moment": enriched},
            )
        )
        # The same description-only path A0 always used (D-16); L7 still owns
        # the class registry, the gate, and the audit log.
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="guardian",
                payload={
                    "proposal_id": uuid4().hex[:12],
                    "action_type": "notification",
                    "kwargs": {"text": enriched.text, "node_ids": list(enriched.evidence)},
                    "reason": f"guardian: {enriched.kind}",
                    "trigger": enriched.kind,
                },
            )
        )

    # -------------------------------------------------------------- feedback
    async def on_moment_feedback(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            moment = event.payload.get("moment")
            outcome = event.payload.get("outcome")
            if not isinstance(moment, Moment) or not isinstance(outcome, str):
                return
            focus = moment.context.get("focus_bucket")
            hour = moment.context.get("hour_bucket")
            dismiss = moment.context.get("dismissal_bucket")
            if not (isinstance(focus, str) and isinstance(hour, str) and isinstance(dismiss, str)):
                return  # feedback for a moment this guardian never delivered
            now = self._clock.now()
            bucket = f"{focus}|{hour}|{dismiss}"
            arm = self._touch_arm(moment.kind, bucket, now)
            if outcome == "accepted":
                arm.a += 1.0
            elif outcome == "dismissed":
                arm.b += 1.0
                self._recent_dismissals.append(now)
            elif outcome == "ignored":
                arm.b += _IGNORED_B_INCREMENT
            else:
                return
            arm.n += 1
            arm.updated_at = now
            self._feedback_seen += 1
            self._last_at = now
            self._persist_arm(moment.kind, bucket, arm)
        except Exception as exc:
            self._on_error(exc)

    # ------------------------------------------------------------- arm state
    def _touch_arm(self, kind: str, bucket: str, now: datetime) -> _Arm:
        key = (kind, bucket)
        arm = self._arms.get(key)
        if arm is None:
            arm = _Arm(a=_PRIOR_A, b=_PRIOR_B, n=0, updated_at=now)
            self._arms[key] = arm
            return arm
        days = (now - arm.updated_at).total_seconds() / 86400.0
        if days > 0.0:
            factor = self.config.guardian_decay**days
            arm.a *= factor
            arm.b *= factor
            arm.updated_at = now
        return arm

    def _persist_arm(self, kind: str, bucket: str, arm: _Arm) -> None:
        if self._episodes is None:
            return
        blob = {"a": arm.a, "b": arm.b, "n": arm.n, "updated_at": arm.updated_at.isoformat()}
        self._episodes.assert_fact(
            EpisodeKind.GUARDIAN_POSTERIOR,
            f"guardian:{kind}:{bucket}",
            json.dumps(blob),
            valid_from=arm.updated_at,
            source="guardian",
        )

    async def _rehydrate(self) -> None:
        assert self._episodes is not None
        try:
            records = await self._episodes.at(self._clock.now())
        except Exception as exc:  # a restart must not fail because of this
            self._on_error(exc)
            return
        for record in records:
            if record.kind != str(EpisodeKind.GUARDIAN_POSTERIOR):
                continue
            parts = record.subject.split(":", 2)
            if len(parts) != 3 or parts[0] != "guardian" or record.object is None:
                continue
            _prefix, kind, bucket = parts
            try:
                blob = json.loads(record.object)
                arm = _Arm(
                    a=float(blob["a"]),
                    b=float(blob["b"]),
                    n=int(blob["n"]),
                    updated_at=datetime.fromisoformat(blob["updated_at"]),
                )
            except (KeyError, ValueError, TypeError) as exc:
                self._on_error(exc)
                continue
            self._arms[(kind, bucket)] = arm

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("guardian handler failed")
        self.event_bus.publish(
            system_error_event(module="guardian", exception=str(exc), severity="handler")
        )
