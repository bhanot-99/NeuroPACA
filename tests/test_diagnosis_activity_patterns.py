"""B2.5b · FocusSessionPattern + DistractionPattern (D-10).

Patterns are pure and synchronous, so these drive them directly with hand-built
`MetricSnapshot` windows — no bus, no clock, no graph (rules.md §8). The whole-
pipeline claim (replayed through a real `SignalCorrelator`) is in
`test_b2_5_recorded_fixtures.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from neuropaca.core.enums import RelationType, SignalType
from neuropaca.diagnosis.patterns import DistractionPattern, FocusSessionPattern
from neuropaca.sensing.snapshot import MetricSnapshot

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _NoBaseline:
    def zscore(self, collector: str, metric: str, value: float) -> float:
        return 0.0


def _act(domain: str, *, t: float, app_id: str = "dev.zed.Zed") -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="activity",
        timestamp=_BASE + timedelta(seconds=t),
        data={"app_id": app_id, "previous_app_id": None, "title": "", "domain": domain},
    )


def _sys(cpu: float, *, t: float) -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="system",
        timestamp=_BASE + timedelta(seconds=t),
        data={"cpu_percent": cpu},
    )


def _win(**kw: Sequence[MetricSnapshot]) -> Mapping[str, Sequence[MetricSnapshot]]:
    return dict(kw)


# ----------------------------------------------------------------- FocusSession


def _focus_windows(domain: str, *, span: float, cpu: float):
    activity = [_act(domain, t=0.0)]
    system = [_sys(cpu, t=s) for s in range(0, int(span) + 1, 60)]
    return _win(activity=activity, system=system)


def test_focus_session_fires_after_twenty_minutes_of_engineering() -> None:
    p = FocusSessionPattern()
    draft = p.evaluate(_focus_windows("domain:engineering", span=1260, cpu=42.0), _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.FOCUS_SESSION
    spec = draft.node_specs[0]
    assert spec.node_id == "app:dev.zed.Zed"
    assert spec.edges == (("domain:engineering", RelationType.PART_OF),)
    assert 0.0 < draft.confidence <= 1.0


def test_focus_session_fires_for_research_too() -> None:
    p = FocusSessionPattern()
    assert p.evaluate(_focus_windows("domain:research", span=1300, cpu=25.0), _NoBaseline())


def test_focus_session_silent_before_twenty_minutes() -> None:
    p = FocusSessionPattern()
    assert (
        p.evaluate(_focus_windows("domain:engineering", span=900, cpu=42.0), _NoBaseline()) is None
    )


def test_focus_session_silent_when_machine_is_idle() -> None:
    p = FocusSessionPattern()
    assert (
        p.evaluate(_focus_windows("domain:engineering", span=1300, cpu=2.0), _NoBaseline()) is None
    )


def test_focus_session_silent_for_a_non_focus_domain() -> None:
    p = FocusSessionPattern()
    assert p.evaluate(_focus_windows("domain:comms", span=1300, cpu=42.0), _NoBaseline()) is None


def test_focus_session_is_edge_triggered_and_rearms_on_leaving_focus() -> None:
    p = FocusSessionPattern()
    focus = _focus_windows("domain:engineering", span=1300, cpu=42.0)
    assert p.evaluate(focus, _NoBaseline()) is not None
    assert p.evaluate(focus, _NoBaseline()) is None  # still focused — no repeat

    # switch away, then back and sustain again -> a second signal
    left = _win(
        activity=[_act("domain:engineering", t=0.0), _act("domain:comms", t=1400.0)],
        system=focus["system"],
    )
    assert p.evaluate(left, _NoBaseline()) is None  # re-arms here
    back = _win(
        activity=[*left["activity"], _act("domain:engineering", t=1500.0)],
        system=[*focus["system"], _sys(42.0, t=2760.0)],
    )
    assert p.evaluate(back, _NoBaseline()) is not None


# ----------------------------------------------------------------- Distraction


def test_distraction_fires_on_six_switches_in_two_minutes() -> None:
    p = DistractionPattern()
    activity = [_act("", t=float(i * 18), app_id=f"app{i}") for i in range(6)]  # 0..90s
    draft = p.evaluate(_win(activity=activity), _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.DISTRACTION
    assert draft.node_specs == ()
    assert "6 app switches" in draft.reason


def test_distraction_silent_when_switches_are_spread_out() -> None:
    p = DistractionPattern()
    activity = [_act("", t=float(i * 60), app_id=f"app{i}") for i in range(6)]  # 0..300s
    assert p.evaluate(_win(activity=activity), _NoBaseline()) is None


def test_distraction_is_edge_triggered_and_rearms_when_settled() -> None:
    p = DistractionPattern()
    burst = [_act("", t=float(i * 15), app_id=f"app{i}") for i in range(6)]  # 0..75s
    assert p.evaluate(_win(activity=burst), _NoBaseline()) is not None
    assert p.evaluate(_win(activity=burst), _NoBaseline()) is None  # firing — suppressed

    # a lone later switch: trailing-120s window now holds just it -> re-arm
    settled = [*burst, _act("", t=400.0, app_id="calm")]
    assert p.evaluate(_win(activity=settled), _NoBaseline()) is None
    # and a fresh burst fires again
    burst2 = [*settled, *[_act("", t=400.0 + i * 15, app_id=f"b{i}") for i in range(1, 7)]]
    assert p.evaluate(_win(activity=burst2), _NoBaseline()) is not None


def test_activity_patterns_silent_with_no_activity_window() -> None:
    assert FocusSessionPattern().evaluate(_win(system=[_sys(50.0, t=0)]), _NoBaseline()) is None
    assert DistractionPattern().evaluate(_win(), _NoBaseline()) is None
