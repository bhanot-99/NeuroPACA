# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · `PresenceTracker` — the presence state machine, minus L9
(`core/presence_tracker.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, PresenceState
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event, Moment
from neuropaca.core.presence_tracker import PresenceTracker

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


async def _tracker(clock: FakeClock | None = None) -> tuple[PresenceTracker, EventBus]:
    bus = EventBus.get_instance()
    await bus.start()
    tracker = PresenceTracker(
        bus, Config(inference_backend="fake"), clock=clock or FakeClock(wall=_NOW)
    )
    await tracker.initialize()
    await tracker.start()
    return tracker, bus


def _state(tracker: PresenceTracker) -> PresenceState:
    detail = tracker.health().detail
    raw = dict(pair.split("=", 1) for pair in detail.split())
    return PresenceState(raw["state"])


async def test_starts_awake_with_nothing_known_yet() -> None:
    tracker, bus = await _tracker()
    try:
        assert _state(tracker) is PresenceState.AWAKE
        assert tracker.health().ok is True
    finally:
        await tracker.stop()
        await bus.stop()


async def test_idle_detected_moves_to_idle() -> None:
    tracker, bus = await _tracker()
    try:
        await tracker.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
        assert _state(tracker) is PresenceState.IDLE
    finally:
        await tracker.stop()
        await bus.stop()


async def test_activity_after_idle_moves_to_focused() -> None:
    tracker, bus = await _tracker()
    try:
        await tracker.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
        await tracker.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
        assert _state(tracker) is PresenceState.FOCUSED
    finally:
        await tracker.stop()
        await bus.stop()


async def test_dmn_cycle_started_is_thinking_and_beats_everything() -> None:
    tracker, bus = await _tracker()
    try:
        await tracker.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
        await tracker.on_dmn_cycle_started(Event(event_type=EventType.DMN_CYCLE_STARTED))
        assert _state(tracker) is PresenceState.THINKING
    finally:
        await tracker.stop()
        await bus.stop()


async def test_dmn_cycle_ended_clears_thinking() -> None:
    tracker, bus = await _tracker()
    try:
        await tracker.on_dmn_cycle_started(Event(event_type=EventType.DMN_CYCLE_STARTED))
        await tracker.on_dmn_cycle_ended(Event(event_type=EventType.DMN_CYCLE_ENDED))
        assert _state(tracker) is not PresenceState.THINKING
    finally:
        await tracker.stop()
        await bus.stop()


async def test_moment_proposed_is_noticed_within_the_window() -> None:
    tracker, bus = await _tracker()
    try:
        moment = Moment(
            kind="briefing", text="hi", evidence=(), value=1.0, context={}, expires_at=_NOW
        )
        await tracker.on_moment_proposed(
            Event(event_type=EventType.MOMENT_PROPOSED, payload={"moment": moment})
        )
        assert _state(tracker) is PresenceState.NOTICED
    finally:
        await tracker.stop()
        await bus.stop()


async def test_insight_generated_is_noticed_regardless_of_confidence() -> None:
    """Unlike the old L9 `insights` verb, no confidence/category filter and no
    daily cap here — this tracker has no surfaced-insights menu to protect."""
    tracker, bus = await _tracker()
    try:
        await tracker.on_insight_generated(Event(event_type=EventType.INSIGHT_GENERATED))
        assert _state(tracker) is PresenceState.NOTICED
    finally:
        await tracker.stop()
        await bus.stop()


async def test_events_after_stop_are_ignored() -> None:
    tracker, bus = await _tracker()
    await tracker.stop()
    await tracker.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    assert _state(tracker) is PresenceState.AWAKE  # unchanged — stop() means stop
    await bus.stop()


async def test_health_detail_is_machine_parseable_key_value_pairs() -> None:
    tracker, bus = await _tracker()
    try:
        detail = tracker.health().detail
        parsed = dict(pair.split("=", 1) for pair in detail.split())
        assert set(parsed) == {"state", "since", "errors"}
        assert parsed["errors"] == "0"
    finally:
        await tracker.stop()
        await bus.stop()


async def test_stop_is_idempotent() -> None:
    tracker, bus = await _tracker()
    await tracker.stop()
    await tracker.stop()  # must not raise
    await bus.stop()
