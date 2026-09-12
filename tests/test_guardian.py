# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A3 · `Guardian` (VISION.md §3.6, VISION_PHASES.md).

No test loads a real model (`FakeClock`, rules.md §8). Handlers are invoked
directly, the same convention `tests/test_moments.py` uses; publishes from
the guardian itself still round-trip through a real `EventBus` so
`MOMENT_DELIVERED`/`ACTION_PROPOSAL`/`MOMENT_FEEDBACK` can be asserted on.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event, Moment
from neuropaca.drive.guardian import Guardian, _Arm, dismissal_bucket, hour_bucket

_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)  # afternoon


def _moment(
    *, kind: str = "welcome_back", value: float = 1.0, expires_in_minutes: float = 10.0, now=_NOW
) -> Moment:
    return Moment(
        kind=kind,
        text="Welcome back.",
        evidence=("app:code",),
        value=value,
        context={},
        expires_at=now + timedelta(minutes=expires_in_minutes),
    )


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def _guardian(
    clock=None, rng: random.Random | None = None, episode_store=None, **cfg
) -> tuple[Guardian, EventBus]:
    bus = EventBus.get_instance()
    await bus.start()
    guardian = Guardian(
        bus,
        Config(inference_backend="fake", **cfg),
        clock=clock or FakeClock(wall=_NOW),
        rng=rng,
        episode_store=episode_store,
    )
    await guardian.initialize()
    await guardian.start()
    return guardian, bus


# ------------------------------------------------------------------- buckets


def test_hour_bucket_covers_all_24_hours() -> None:
    assert hour_bucket(0) == "night"
    assert hour_bucket(5) == "night"
    assert hour_bucket(6) == "morning"
    assert hour_bucket(11) == "morning"
    assert hour_bucket(12) == "afternoon"
    assert hour_bucket(17) == "afternoon"
    assert hour_bucket(18) == "evening"
    assert hour_bucket(23) == "evening"


def test_dismissal_bucket_caps_at_two_plus() -> None:
    assert dismissal_bucket(0) == "0"
    assert dismissal_bucket(1) == "1"
    assert dismissal_bucket(2) == "2+"
    assert dismissal_bucket(9) == "2+"


# --------------------------------------------------------------- burn-in math


def test_burn_in_never_exceeds_the_posterior_mean() -> None:
    """1000 simulated draws pre-N0: the burn-in is deterministic, never a
    sample — so it can never exceed (or differ from) a/(a+b), whatever the
    rng would have drawn."""
    guardian = Guardian(EventBus.get_instance(), Config(inference_backend="fake"))
    arm = _Arm(a=1.0, b=3.0, n=0, updated_at=_NOW)
    expected = arm.a / (arm.a + arm.b)
    for _ in range(1000):
        assert guardian._p_hat(arm) == expected


def test_thompson_sample_used_at_or_above_burn_in() -> None:
    class _FixedRng(random.Random):
        def betavariate(self, alpha: float, beta: float) -> float:
            return 0.42

    guardian = Guardian(EventBus.get_instance(), Config(inference_backend="fake"), rng=_FixedRng())
    arm = _Arm(a=1.0, b=3.0, n=5, updated_at=_NOW)  # n == default guardian_burn_in_n
    assert guardian._p_hat(arm) == 0.42


# ----------------------------------------------------------------- focus hold


async def test_nothing_delivered_while_focused() -> None:
    """A fresh guardian (never seen IDLE_DETECTED/ACTIVITY_DETECTED) defaults
    to `focused` — the conservative unknown-state fallback — so even a
    high-value moment is held, never sampled."""
    guardian, bus = await _guardian()
    delivered: list[Event] = []
    actions: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))
    bus.subscribe(EventType.ACTION_PROPOSAL, _collect(actions))

    moment = _moment(value=100.0)  # would trivially clear any cost if sampled
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": moment})
    )
    await bus.join()

    assert delivered == []
    assert actions == []
    assert guardian._held_count == 1
    await bus.stop()


async def test_held_moment_delivered_once_focus_session_ends() -> None:
    clock = FakeClock(wall=_NOW)
    guardian, bus = await _guardian(clock=clock)
    delivered: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))

    moment = _moment(value=1.0, expires_in_minutes=10.0)
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": moment})
    )
    await bus.join()
    assert delivered == []  # held — no signal seen yet, defaults to focused

    # The session ends: IDLE_DETECTED re-evaluates the held queue. Fresh-arm
    # posterior mean is 1/(1+3) = 0.25; default interrupt_cost_normal = 0.1;
    # 0.25 * 1.0 > 0.1, so it clears the bar this time (focus is now "normal").
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert len(delivered) == 1
    assert guardian._held_count == 1  # unchanged — one held, one released, not two
    await bus.stop()


async def test_held_moment_past_its_own_expiry_is_dropped_not_delivered() -> None:
    clock = FakeClock(wall=_NOW)
    guardian, bus = await _guardian(clock=clock)
    delivered: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))

    moment = _moment(value=1.0, expires_in_minutes=5.0)
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": moment})
    )
    await clock.advance(6 * 60.0)  # past the moment's own expires_at
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert delivered == []
    assert guardian._held_expired == 1
    await bus.stop()


async def test_held_queue_is_bounded() -> None:
    guardian, bus = await _guardian()
    for _ in range(30):
        await guardian.on_moment_proposed(
            Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment()})
        )
    assert guardian._held_count <= 20
    assert guardian._held_dropped_full > 0
    await bus.stop()


# -------------------------------------------------------------------- budget


async def test_daily_budget_caps_deliveries() -> None:
    clock = FakeClock(wall=_NOW)
    guardian, bus = await _guardian(clock=clock, nudge_daily_budget=1)
    delivered: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))
    # Put the guardian in a non-focused state so proposals are decided now.
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))

    for _ in range(3):
        await guardian.on_moment_proposed(
            Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment(value=1.0)})
        )
    await bus.join()

    assert len(delivered) == 1
    assert guardian._dropped_budget == 2
    await bus.stop()


async def test_budget_resets_on_a_new_day_even_after_being_exhausted() -> None:
    """Regression: the day-rollover reset used to live inside the
    now-removed `_spend_budget`, only reached *after* the cap check passed —
    so once a day ended at the cap, the reset code became unreachable and
    the guardian went silent forever. `_roll_budget_day` now runs before the
    check, the same order `moments.py`'s `_cap_day` always used."""
    clock = FakeClock(wall=_NOW)
    guardian, bus = await _guardian(clock=clock, nudge_daily_budget=1)
    delivered: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))

    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment(value=1.0)})
    )
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment(value=1.0)})
    )
    await bus.join()
    assert len(delivered) == 1  # today's budget of 1 is exhausted

    await clock.advance(24 * 3600.0)  # a new day, same hour bucket
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment(value=1.0)})
    )
    await bus.join()

    assert len(delivered) == 2  # tomorrow's budget must not still read as spent
    await bus.stop()


# --------------------------------------------------------------------- decay


def test_decay_on_touch_matches_gamma_to_the_elapsed_days() -> None:
    guardian = Guardian(
        EventBus.get_instance(), Config(inference_backend="fake", guardian_decay=0.9)
    )
    arm = guardian._touch_arm("welcome_back", "normal|afternoon|0", _NOW)
    arm.a, arm.b = 5.0, 2.0
    later = _NOW + timedelta(days=3)
    touched = guardian._touch_arm("welcome_back", "normal|afternoon|0", later)
    assert touched is arm
    assert touched.a == 5.0 * (0.9**3)
    assert touched.b == 2.0 * (0.9**3)


# ------------------------------------------------------------------- feedback


async def test_feedback_updates_the_arm_named_by_the_enriched_context() -> None:
    clock = FakeClock(wall=_NOW)
    guardian, bus = await _guardian(clock=clock)
    delivered: list[Event] = []
    bus.subscribe(EventType.MOMENT_DELIVERED, _collect(delivered))
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))

    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": _moment(value=1.0)})
    )
    await bus.join()
    assert len(delivered) == 1
    enriched = delivered[0].payload["moment"]
    assert {"focus_bucket", "hour_bucket", "dismissal_bucket"} <= enriched.context.keys()

    key = (
        enriched.kind,
        f"{enriched.context['focus_bucket']}|{enriched.context['hour_bucket']}|"
        f"{enriched.context['dismissal_bucket']}",
    )
    before = guardian._arms[key]
    before_a = before.a

    await guardian.on_moment_feedback(
        Event(
            event_type=EventType.MOMENT_FEEDBACK,
            payload={"moment": enriched, "outcome": "accepted"},
        )
    )

    after = guardian._arms[key]
    assert after.a == before_a + 1.0
    assert after.n == 1
    await bus.stop()


async def test_feedback_for_a_moment_never_delivered_by_this_guardian_is_ignored() -> None:
    guardian, bus = await _guardian()
    before_arms = dict(guardian._arms)
    await guardian.on_moment_feedback(
        Event(
            event_type=EventType.MOMENT_FEEDBACK,
            payload={"moment": _moment(), "outcome": "accepted"},  # no bucket in context
        )
    )
    assert guardian._arms == before_arms
    assert guardian._feedback_seen == 0
    await bus.stop()


# ---------------------------------------------------------------- persistence


async def test_posteriors_survive_a_restart_via_the_episode_store(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    guardian, bus = await _guardian(clock=clock, episode_store=store)
    await guardian.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))

    moment = _moment(value=1.0)
    await guardian.on_moment_proposed(
        Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": moment})
    )
    await bus.join()
    delivered_key = next(iter(guardian._arms))
    await guardian.on_moment_feedback(
        Event(
            event_type=EventType.MOMENT_FEEDBACK,
            payload={
                "moment": Moment(
                    kind=delivered_key[0],
                    text="x",
                    evidence=(),
                    value=1.0,
                    context=dict(
                        zip(
                            ("focus_bucket", "hour_bucket", "dismissal_bucket"),
                            delivered_key[1].split("|"),
                            strict=True,
                        )
                    ),
                    expires_at=_NOW + timedelta(minutes=10),
                ),
                "outcome": "accepted",
            },
        )
    )
    await store.flush()
    arm_before = guardian._arms[delivered_key]

    guardian2 = Guardian(bus, Config(inference_backend="fake"), clock=clock, episode_store=store)
    await guardian2.initialize()

    assert delivered_key in guardian2._arms
    restored = guardian2._arms[delivered_key]
    assert restored.a == arm_before.a
    assert restored.b == arm_before.b
    assert restored.n == arm_before.n

    await store.stop()
    await bus.stop()
