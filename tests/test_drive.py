"""L5 · Drive — the pressure gradient (B7, D-14, Architecture.md §7).

The two exit criteria that live in this layer are tested as invariants, not as
tuned numbers:

- *a single signal never crosses the high threshold* — proved by piling an
  absurd amount of one source's pressure onto a node and watching the high tier
  stay shut;
- *pressure decays to < 1 % within 10 min of the last contribution* — proved
  against the exact exponential, with the clock under the test's control.
"""

from __future__ import annotations

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.drive.pressure import SOURCE_DIAGNOSIS, SOURCE_LEARNING, PressureAccumulator
from neuropaca.learning.insight import Insight


def _config(**over) -> Config:
    return Config(inference_backend="fake", **over)


async def _accumulator(tmp_path, *, clock: FakeClock | None = None, **over):
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    drive = PressureAccumulator(bus, _config(**over), graph, clock=clock or FakeClock())
    await drive.initialize()
    await drive.start()
    return drive, bus


async def _teardown(drive: PressureAccumulator, bus: EventBus) -> None:
    await drive.stop()
    await bus.stop()


def _signal(*, node_ids: tuple[str, ...], confidence: float = 0.9) -> Signal:
    return Signal(
        signal_type=SignalType.HIGH_LOAD,
        confidence=confidence,
        related_node_ids=node_ids,
        source_snapshots=(),
        reason="cpu pinned",
    )


def _insight(*, node_ids: tuple[str, ...], confidence: float = 0.9) -> Insight:
    return Insight(
        category="anomaly",
        cited_node_ids=node_ids,
        source_signal=SignalType.HIGH_LOAD,
        confidence=confidence,
        snapshot_count=1,
        node_id="insight:abc",
    )


class _Spy:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []

    async def __call__(self, event: Event) -> None:
        entry = event.payload["entry"]
        self.events.append((event.payload["tier"], entry.node_id, entry.pressure))

    @property
    def tiers(self) -> list[str]:
        return [tier for tier, _, _ in self.events]


# ------------------------------------------------------------------ accumulation


async def test_one_diagnosis_spike_fires_the_low_tier(tmp_path) -> None:
    drive, bus = await _accumulator(tmp_path)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        bus.publish(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": _signal(node_ids=("app:webpack",), confidence=1.0)},
            )
        )
        await bus.join()
        reason = drive.get_top_pressures(1)[0].reason
    finally:
        await _teardown(drive, bus)

    assert spy.tiers == ["low"]
    assert spy.events[0][1] == "app:webpack"
    assert "cpu pinned" in reason


async def test_a_single_source_can_never_reach_the_high_tier(tmp_path) -> None:
    """The B7 exit criterion, as a structural invariant: corroboration is a set
    test over {diagnosis, learning}, so no amount of one source's evidence — here
    fifty maximum-confidence L3 spikes, ~17x the high threshold — opens it."""
    drive, bus = await _accumulator(tmp_path)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        for _ in range(50):
            drive.add_pressure(
                "app:webpack", 1.0, "L3 spike", source=SOURCE_DIAGNOSIS, confidence=1.0
            )
        peak = drive.current_pressure("app:webpack")
        await bus.join()
    finally:
        await _teardown(drive, bus)

    assert peak > 16 * 3.0  # ~17x the high threshold, from one source
    assert "high" not in spy.tiers
    assert spy.tiers == ["low"]  # and it only announced the crossing once


async def test_l3_and_l4_together_open_the_high_tier(tmp_path) -> None:
    drive, bus = await _accumulator(tmp_path)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        bus.publish(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": _signal(node_ids=("app:webpack",), confidence=1.0)},
            )
        )
        for _ in range(3):
            bus.publish(
                Event(
                    event_type=EventType.INSIGHT_GENERATED,
                    source="learning",
                    payload={"insight": _insight(node_ids=("app:webpack",), confidence=1.0)},
                )
            )
        await bus.join()
        top = drive.get_top_pressures(1)[0]
    finally:
        await _teardown(drive, bus)

    assert spy.tiers == ["low", "high"]
    assert set(top.sources) == {SOURCE_DIAGNOSIS, SOURCE_LEARNING}
    assert top.pressure >= 3.0


async def test_an_l6_idle_thought_does_not_corroborate(tmp_path) -> None:
    """L6 publishes `INSIGHT_GENERATED` too, but an idle thought is the system
    talking to itself — it must not stand in for L4's independent evidence."""
    drive, bus = await _accumulator(tmp_path)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        for _ in range(6):
            bus.publish(
                Event(
                    event_type=EventType.SIGNAL_CORRELATED,
                    source="diagnosis",
                    payload={"signal": _signal(node_ids=("app:webpack",), confidence=1.0)},
                )
            )
            bus.publish(
                Event(
                    event_type=EventType.INSIGHT_GENERATED,
                    source="idle",  # L6, not L4
                    payload={"insight": _insight(node_ids=("app:webpack",), confidence=1.0)},
                )
            )
        await bus.join()
        sources = set(drive.get_top_pressures(1)[0].sources)
    finally:
        await _teardown(drive, bus)

    assert sources == {SOURCE_DIAGNOSIS}
    assert "high" not in spy.tiers


async def test_a_low_confidence_contribution_never_corroborates(tmp_path) -> None:
    clock = FakeClock()
    drive, bus = await _accumulator(tmp_path, clock=clock)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        drive.add_pressure("n", 2.0, "L3", source=SOURCE_DIAGNOSIS, confidence=1.0)
        drive.add_pressure("n", 2.0, "L4 hunch", source=SOURCE_LEARNING, confidence=0.5)
        entry = drive.get_top_pressures(1)[0]
    finally:
        await _teardown(drive, bus)

    assert entry.pressure >= 4.0  # well past the high threshold
    assert entry.sources == (SOURCE_DIAGNOSIS,)
    assert "high" not in spy.tiers


async def test_an_action_that_cannot_explain_itself_gets_no_pressure(tmp_path) -> None:
    drive, bus = await _accumulator(tmp_path)
    try:
        drive.add_pressure("n", 5.0, "", source=SOURCE_DIAGNOSIS, confidence=1.0)
        assert drive.current_pressure("n") == 0.0
    finally:
        await _teardown(drive, bus)


# ------------------------------------------------------------------------ decay


async def test_decay_is_exactly_one_half_per_minute(tmp_path) -> None:
    clock = FakeClock()
    drive, bus = await _accumulator(tmp_path, clock=clock)
    try:
        drive.add_pressure("n", 1.0, "spike", source=SOURCE_DIAGNOSIS, confidence=1.0)
        await clock.advance(60)
        assert drive.current_pressure("n") == 0.5
        await clock.advance(60)
        assert drive.current_pressure("n") == 0.25
    finally:
        await _teardown(drive, bus)


async def test_pressure_falls_below_one_percent_within_ten_minutes(tmp_path) -> None:
    """The B7 exit criterion, at the default 60 s half-life: 0.5**10 = 0.098 %."""
    clock = FakeClock()
    drive, bus = await _accumulator(tmp_path, clock=clock)
    try:
        drive.add_pressure("n", 4.0, "spike", source=SOURCE_DIAGNOSIS, confidence=1.0)
        peak = drive.current_pressure("n")
        await clock.advance(600)
        remaining = drive.current_pressure("n")
    finally:
        await _teardown(drive, bus)

    assert remaining / peak < 0.01
    assert remaining / peak == 0.5**10


async def test_the_decay_timer_evicts_faded_entries_and_re_arms_the_latch(tmp_path) -> None:
    clock = FakeClock()
    drive, bus = await _accumulator(tmp_path, clock=clock)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        drive.add_pressure("n", 1.5, "spike", source=SOURCE_DIAGNOSIS, confidence=1.0)
        await bus.join()
        assert spy.tiers == ["low"]

        # A second spike while still latched must not re-announce the crossing.
        drive.add_pressure("n", 0.2, "spike", source=SOURCE_DIAGNOSIS, confidence=1.0)
        await bus.join()
        assert spy.tiers == ["low"]

        # 20 min of silence: the timer runs, the entry falls under the eviction
        # floor and is dropped, so the next spike is a fresh crossing.
        await clock.advance(1200)
        assert drive.pressure_map == {}

        drive.add_pressure("n", 1.5, "spike", source=SOURCE_DIAGNOSIS, confidence=1.0)
        await bus.join()
        assert spy.tiers == ["low", "low"]
    finally:
        await _teardown(drive, bus)


async def test_yesterdays_spike_cannot_combine_with_todays(tmp_path) -> None:
    """Architecture.md §7: last week's spike cannot combine with today's.

    An hour after an L3 spike, a *large* L4 contribution lands on the same node —
    enough on its own to clear the high threshold. It does not open the high
    tier, because the L3 evidence is long gone: the decay timer evicted it, so
    the node carries one source again and corroboration fails."""
    clock = FakeClock()
    drive, bus = await _accumulator(tmp_path, clock=clock)
    spy = _Spy()
    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)
    try:
        drive.add_pressure("n", 2.0, "L3", source=SOURCE_DIAGNOSIS, confidence=1.0)
        await clock.advance(3600)  # an hour later
        drive.add_pressure("n", 4.0, "L4", source=SOURCE_LEARNING, confidence=1.0)
        entry = drive.get_top_pressures(1)[0]
        await bus.join()
        assert entry.pressure >= 3.0  # past the high threshold on magnitude alone
        assert set(entry.sources) == {SOURCE_LEARNING}
        assert "high" not in spy.tiers
    finally:
        await _teardown(drive, bus)


# ----------------------------------------------------------------------- health


async def test_health_reports_the_hottest_node(tmp_path) -> None:
    drive, bus = await _accumulator(tmp_path)
    try:
        drive.add_pressure("cold", 0.1, "r", source=SOURCE_DIAGNOSIS, confidence=1.0)
        drive.add_pressure("hot", 0.9, "r", source=SOURCE_DIAGNOSIS, confidence=1.0)
        report = drive.health()
    finally:
        await _teardown(drive, bus)

    assert report.ok is True
    assert "top hot@0.90" in report.detail
