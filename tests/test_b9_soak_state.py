"""B9 · soak bookkeeping across power cycles (`scripts/b9_soak_state.py`).

The 7-day soak is no longer one uninterrupted process. It starts with the
graphical session and stops when the machine does, so a week is a *sum* of
sessions -- and the ways that sum can be wrong are all silent. It can credit
hours the machine spent switched off, lose a session to a power cut, or declare
completion early. Every one of those produces a plausible-looking number that
invalidates a week of work, which is why the arithmetic lives in an importable
module and is tested here rather than living in the driver's shell.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "b9_soak_state.py"
_spec = importlib.util.spec_from_file_location("b9_soak_state", _MODULE_PATH)
assert _spec and _spec.loader
soak = importlib.util.module_from_spec(_spec)
# Registered before exec: the module defines a slotted dataclass, and
# `dataclasses` resolves annotations through `sys.modules[cls.__module__]`.
sys.modules["b9_soak_state"] = soak
_spec.loader.exec_module(soak)


T0 = datetime(2026, 9, 4, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def state() -> dict:
    return soak.load_state(Path("/nonexistent/state.json"))


def test_a_fresh_state_starts_at_zero_against_a_seven_day_target(state: dict) -> None:
    assert state["target_seconds"] == 7 * 24 * 60 * 60
    assert state["sessions"] == []
    assert state["completed_utc"] is None
    assert soak.accrued_seconds(state, T0) == 0.0


def test_runtime_accumulates_across_separate_sessions(state: dict) -> None:
    """Two boots of six hours each are twelve hours of soak, not six."""
    soak.open_session(state, T0)
    soak.close_session(state, "sigterm", T0 + timedelta(hours=6))
    soak.open_session(state, T0 + timedelta(hours=20))
    soak.close_session(state, "sigterm", T0 + timedelta(hours=26))

    assert soak.accrued_seconds(state, T0 + timedelta(days=2)) == 12 * 3600


def test_the_hours_a_machine_spends_switched_off_are_not_counted(state: dict) -> None:
    """The whole point of accrued-runtime accounting. Six hours of soak followed
    by fourteen hours of the box being off is six hours, and a wall-clock reading
    would say twenty."""
    soak.open_session(state, T0)
    soak.close_session(state, "sigterm", T0 + timedelta(hours=6))

    much_later = T0 + timedelta(hours=20)
    assert soak.accrued_seconds(state, much_later) == 6 * 3600


def test_an_open_session_counts_up_to_now(state: dict) -> None:
    soak.open_session(state, T0)
    assert soak.accrued_seconds(state, T0 + timedelta(hours=3)) == 3 * 3600


def test_a_power_cut_is_healed_from_the_last_heartbeat_not_from_now(state: dict) -> None:
    """No SIGTERM ever arrives on a hard power loss, so the session stays open.
    Counting it as running until the next boot would credit the soak with every
    hour the machine was unplugged -- here, eleven of them."""
    soak.open_session(state, T0)
    soak.beat(state, T0 + timedelta(hours=1))  # last moment the daemon is known alive
    # ...power cut. Machine comes back eleven hours later.
    next_boot = T0 + timedelta(hours=12)
    soak.open_session(state, next_boot)

    healed = state["sessions"][0]
    assert healed["reason"] == "unclean"
    assert soak.accrued_seconds(state, next_boot) == pytest.approx(3600, abs=1)


def test_an_unclean_session_is_labelled_so_the_record_shows_the_interruption(
    state: dict,
) -> None:
    soak.open_session(state, T0)
    soak.beat(state, T0 + timedelta(minutes=30))
    soak.open_session(state, T0 + timedelta(hours=5))

    assert [s["reason"] for s in state["sessions"]] == ["unclean", None]
    assert soak.summarise(state, [], T0 + timedelta(hours=5)).count("1 ended unclean")


def test_completion_fires_only_at_seven_days_of_accrued_runtime(state: dict) -> None:
    soak.open_session(state, T0)
    soak.beat(state, T0 + timedelta(days=6, hours=23))
    assert state["completed_utc"] is None

    soak.beat(state, T0 + timedelta(days=7, minutes=1))
    assert state["completed_utc"] is not None


def test_completion_is_not_reached_by_wall_clock_alone(state: dict) -> None:
    """One hour of soak, then the machine sits off for eight days. Not complete."""
    soak.open_session(state, T0)
    soak.close_session(state, "sigterm", T0 + timedelta(hours=1))

    soak.open_session(state, T0 + timedelta(days=8))
    assert state["completed_utc"] is None
    assert "0.6%" in soak.summarise(state, [], T0 + timedelta(days=8))


def test_state_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = soak.load_state(path)
    soak.open_session(state, T0)
    soak.close_session(state, "sigterm", T0 + timedelta(hours=2))
    soak.save_state(path, state)

    assert soak.accrued_seconds(soak.load_state(path), T0 + timedelta(days=1)) == 2 * 3600


def test_a_state_file_from_a_future_build_is_refused_not_silently_loaded(
    tmp_path: Path,
) -> None:
    """Same stance as the graph's schema_version (criterion 6): guessing at a
    format this build does not know would corrupt the one record of the week
    that cannot be regenerated."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "sessions": []}))

    with pytest.raises(SystemExit, match="refusing to guess"):
        soak.load_state(path)


def test_the_summary_reports_memory_growth_as_a_daily_slope(state: dict) -> None:
    soak.open_session(state, T0)
    samples = [
        {"ts": "2026-09-04T09:00:00Z", "uptime_seconds": 60, "rss_mib": 1000},
        {"ts": "2026-09-05T09:00:00Z", "uptime_seconds": 86460, "rss_mib": 1050},
    ]
    trend = soak.rss_trend(samples)
    assert trend is not None
    assert trend.delta_mib == 50
    assert trend.mib_per_day == pytest.approx(50.0)
    assert "+50.0 MiB/day" in soak.summarise(state, samples, T0 + timedelta(days=1))


def test_a_slope_is_not_extrapolated_from_a_few_seconds_of_samples() -> None:
    """A 30 MiB startup allocation measured over two minutes is 21 GiB/day if you
    divide naively. Reporting nothing beats reporting that."""
    samples = [
        {"ts": "2026-09-04T09:00:00Z", "uptime_seconds": 60, "rss_mib": 1000},
        {"ts": "2026-09-04T09:02:00Z", "uptime_seconds": 180, "rss_mib": 1030},
    ]
    trend = soak.rss_trend(samples)
    assert trend is not None
    assert trend.mib_per_day == 0.0


def test_a_sample_truncated_by_a_power_cut_does_not_break_the_summary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "samples.jsonl"
    path.write_text(
        '{"ts": "2026-09-04T09:00:00Z", "rss_mib": 1000}\n'
        '{"ts": "2026-09-04T10:00:00Z", "rss_mib": 1010}\n'
        '{"ts": "2026-09-04T11:00:00Z", "rss_mi'  # cut mid-write
    )
    assert len(soak.read_samples(path)) == 2


def test_the_popup_text_leads_with_progress_and_the_leak_number(state: dict) -> None:
    """What a person reads in five seconds at login decides whether a dead soak
    is caught on day two or on day seven -- the B7 failure, restated."""
    soak.open_session(state, T0)
    samples = [
        {
            "ts": "2026-09-04T09:00:00Z",
            "uptime_seconds": 60,
            "rss_mib": 1000,
            "graph_nodes": 59,
            "graph_edges": 43,
            "activity_edges": 4,
            "pressure_events": 1,
            "errors": 0,
        },
        {
            "ts": "2026-09-05T09:00:00Z",
            "uptime_seconds": 86460,
            "rss_mib": 1001,
            "graph_nodes": 61,
            "graph_edges": 45,
            "activity_edges": 7,
            "pressure_events": 2,
            "errors": 0,
        },
    ]
    text = soak.summarise(state, samples, T0 + timedelta(days=1))

    assert "Progress" in text
    assert "of 7d accrued runtime" in text
    assert "Leak slope" in text
    assert "7 idle/active edges" in text  # the cumulative LATEST, not 4 + 7
    assert "61 nodes, 45 edges" in text  # latest, not first


def test_an_empty_run_says_so_rather_than_looking_healthy(state: dict) -> None:
    """A soak producing nothing must not render as a soak producing zeros that
    scroll past unnoticed."""
    soak.open_session(state, T0)
    assert "No measurements yet" in soak.summarise(state, [], T0)


# ------------------------------------------------------------------------------
# Cumulative counters and daemon restarts
#
# `neuropaca health` reports totals since the daemon started, not deltas since
# the last sample. Every test below guards the same failure: reading those
# totals as if they were deltas, which turns a quiet week into millions of
# phantom events and makes a dead system indistinguishable from a busy one.
# ------------------------------------------------------------------------------


def _run(start_uptime: int, values: list[int], key: str = "activity_edges") -> list[dict]:
    base = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    return [
        {
            "ts": soak._iso(base + timedelta(minutes=i)),
            "uptime_seconds": start_uptime + i * 60,
            "daemon_up": True,
            key: value,
            "rss_mib": 1000 + i,
        }
        for i, value in enumerate(values)
    ]


def test_a_cumulative_counter_is_taken_at_its_latest_value_not_summed() -> None:
    """Three samples reading 5, 9, 14 are fourteen events, not twenty-eight."""
    samples = _run(60, [5, 9, 14])
    assert soak.counter_total(samples, "activity_edges") == 14


def test_a_restart_is_detected_by_uptime_going_backwards() -> None:
    samples = _run(60, [5, 9]) + _run(30, [2, 4])
    assert len(soak.segments(samples)) == 2
    assert soak.restarts(samples) == 1


def test_counters_from_before_a_restart_are_not_lost() -> None:
    """The daemon restarts after 9 events and reaches 4 more. Thirteen, not four
    (which forgets the first life) and not twenty-two (which double-counts it)."""
    samples = _run(60, [5, 9]) + _run(30, [2, 4])
    assert soak.counter_total(samples, "activity_edges") == 13


def test_the_leak_slope_is_measured_within_one_daemon_life() -> None:
    """A restart drops RSS back to its startup value. Spanning one would subtract
    a fresh process from a week-old one and report a healthy negative slope for a
    daemon that had been leaking right up until it died."""
    leaking = [
        {
            "ts": soak._iso(datetime(2026, 9, 4, 9, tzinfo=UTC) + timedelta(hours=i)),
            "uptime_seconds": 3600 * (i + 1),
            "daemon_up": True,
            "rss_mib": 1000 + 10 * i,
        }
        for i in range(24)
    ]
    after_restart = [
        {
            "ts": soak._iso(datetime(2026, 9, 5, 10, tzinfo=UTC)),
            "uptime_seconds": 60,
            "daemon_up": True,
            "rss_mib": 1000,
        },
    ]
    trend = soak.rss_trend([*leaking, *after_restart])

    assert trend is not None
    assert trend.delta_mib == 230  # the leak, not 0
    assert trend.mib_per_day == pytest.approx(240.0, abs=1)


def test_samples_taken_while_the_daemon_was_down_do_not_form_a_segment() -> None:
    samples = [*_run(60, [5, 9]), {"ts": "2026-09-04T09:05:00Z", "daemon_up": False}]
    assert len(soak.segments(samples)) == 1
    assert soak.counter_total(samples, "activity_edges") == 9


def test_a_daemon_down_at_the_last_sample_is_stated_in_the_popup() -> None:
    """Silence in the summary is how B7 lost three soaks. A daemon that died at
    03:00 must say so at the next login, not render as a normal-looking row."""
    state = soak.load_state(Path("/nonexistent/state.json"))
    soak.open_session(state, T0)
    samples = [*_run(60, [5]), {"ts": "2026-09-04T09:05:00Z", "daemon_up": False}]

    assert "Daemon      DOWN at the last sample" in soak.summarise(state, samples, T0)


def test_a_degraded_module_is_named_in_the_popup() -> None:
    state = soak.load_state(Path("/nonexistent/state.json"))
    soak.open_session(state, T0)
    samples = _run(60, [5])
    samples[-1]["degraded"] = ["activity"]

    assert "DEGRADED    activity" in soak.summarise(state, samples, T0)
