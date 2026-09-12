# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · the mirror (VISION_PHASES.md §3.8, `core/mirror.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from neuropaca.core.enums import EpisodeKind
from neuropaca.core.episodes import EpisodeRecord
from neuropaca.core.mirror import (
    baseline_distribution,
    bucket_seconds,
    compute_mirror,
    daily_distribution,
    kl_divergence,
    missing_contributors,
    normalize,
    smooth,
    top_contributors,
)

_DAY0 = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)  # a Tuesday


def _span(subject: str, at: datetime, seconds: float) -> EpisodeRecord:
    return EpisodeRecord(
        episode_seq=0,
        id="episode:x",
        kind=str(EpisodeKind.FOCUS_SPAN),
        subject=subject,
        object=None,
        t_start=at,
        t_end=at + timedelta(seconds=seconds),
        t_valid=None,
        t_invalid=None,
        t_seen=at,
        source="",
        attrs={},
    )


def _cfg(**overrides):
    base = {
        "baseline_days": 14,
        "half_life_days": 7.0,
        "same_weekday_boost": 2.0,
        "threshold": 0.5,
        "top_n": 3,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------- bucket_seconds


def test_bucket_seconds_buckets_by_subject_and_start_hour() -> None:
    rows = [_span("app:a", _DAY0 + timedelta(hours=9, minutes=30), 600)]
    out = bucket_seconds(rows, _DAY0, _DAY0 + timedelta(days=1))
    assert out == {("app:a", 9): 600.0}


def test_bucket_seconds_clips_to_the_window() -> None:
    """A span starting before `t0` only counts the part inside the window."""
    rows = [_span("app:a", _DAY0 - timedelta(minutes=5), 600)]
    out = bucket_seconds(rows, _DAY0, _DAY0 + timedelta(days=1))
    assert out[("app:a", 0)] == pytest.approx(300.0)


def test_bucket_seconds_ignores_non_focus_span_rows() -> None:
    idle_row = EpisodeRecord(
        episode_seq=0,
        id="episode:x",
        kind=str(EpisodeKind.IDLE_SPAN),
        subject="app:a",
        object=None,
        t_start=_DAY0,
        t_end=_DAY0 + timedelta(seconds=60),
        t_valid=None,
        t_invalid=None,
        t_seen=_DAY0,
        source="",
        attrs={},
    )
    assert bucket_seconds([idle_row], _DAY0, _DAY0 + timedelta(days=1)) == {}


# ------------------------------------------------------------------------ normalize


def test_normalize_sums_to_one() -> None:
    dist = normalize({("a", 0): 30.0, ("b", 1): 10.0})
    assert sum(dist.values()) == pytest.approx(1.0)
    assert dist[("a", 0)] == pytest.approx(0.75)


def test_normalize_of_empty_or_zero_is_empty() -> None:
    assert normalize({}) == {}
    assert normalize({("a", 0): 0.0}) == {}


# --------------------------------------------------------------------- daily_distribution


def test_daily_distribution_is_todays_bucket_seconds_normalized() -> None:
    rows = [
        _span("app:a", _DAY0 + timedelta(hours=9), 1800),
        _span("app:b", _DAY0 + timedelta(hours=10), 1800),
    ]
    q = daily_distribution(rows, _DAY0)
    assert q == {("app:a", 9): pytest.approx(0.5), ("app:b", 10): pytest.approx(0.5)}


# ---------------------------------------------------------------- baseline_distribution


def test_baseline_distribution_up_weights_the_same_weekday() -> None:
    """`_DAY0` is a Tuesday. A Tuesday a week back (age=7, more decayed) should
    still outweigh a Monday just one day back (age=1, less decayed) once the
    same-weekday boost is large enough to win against the extra week of decay."""
    rows = [
        _span("app:tue", _DAY0 - timedelta(days=7) + timedelta(hours=9), 3600),
        _span("app:mon", _DAY0 - timedelta(days=1) + timedelta(hours=9), 3600),
    ]
    p = baseline_distribution(
        rows, _DAY0, lookback_days=14, half_life_days=7.0, same_weekday_boost=3.0
    )
    assert p[("app:tue", 9)] > p[("app:mon", 9)]


def test_baseline_distribution_weights_nearer_days_more() -> None:
    rows = [
        _span("app:near", _DAY0 - timedelta(days=1) + timedelta(hours=9), 3600),
        _span("app:far", _DAY0 - timedelta(days=13) + timedelta(hours=9), 3600),
    ]
    p = baseline_distribution(
        rows, _DAY0, lookback_days=14, half_life_days=7.0, same_weekday_boost=1.0
    )
    assert p[("app:near", 9)] > p[("app:far", 9)]


def test_baseline_distribution_with_no_history_is_empty() -> None:
    p = baseline_distribution(
        [], _DAY0, lookback_days=14, half_life_days=7.0, same_weekday_boost=2.0
    )
    assert p == {}


# --------------------------------------------------------------------------- smooth


def test_smooth_covers_the_union_and_stays_positive() -> None:
    q = {("a", 0): 1.0}
    p = {("b", 1): 1.0}
    qs, ps = smooth(q, p)
    assert set(qs) == set(ps) == {("a", 0), ("b", 1)}
    assert all(v > 0.0 for v in qs.values())
    assert all(v > 0.0 for v in ps.values())
    assert sum(qs.values()) == pytest.approx(1.0)
    assert sum(ps.values()) == pytest.approx(1.0)


def test_smooth_of_two_empty_distributions_is_empty() -> None:
    assert smooth({}, {}) == ({}, {})


# --------------------------------------------------------------------------- KL math


def test_kl_is_zero_for_identical_distributions() -> None:
    q = {("a", 0): 0.6, ("b", 1): 0.4}
    qs, ps = smooth(q, dict(q))
    assert kl_divergence(qs, ps) == pytest.approx(0.0, abs=1e-9)


def test_kl_grows_as_q_shifts_away_from_p() -> None:
    p = {("a", 0): 0.9, ("b", 1): 0.1}
    prior: float | None = None
    for shift in (0.9, 0.7, 0.5, 0.3, 0.1):
        q = {("a", 0): shift, ("b", 1): 1.0 - shift}
        qs, ps = smooth(q, p)
        kl = kl_divergence(qs, ps)
        if prior is not None:
            assert kl >= prior - 1e-9
        prior = kl


def test_kl_is_never_negative() -> None:
    """Gibbs' inequality — true of any two probability distributions."""
    p = {("a", 0): 0.2, ("b", 1): 0.3, ("c", 2): 0.5}
    for q_raw in [
        {("a", 0): 0.5, ("b", 1): 0.5},
        {("a", 0): 0.1, ("b", 1): 0.1, ("c", 2): 0.8},
        {("d", 3): 1.0},
    ]:
        qs, ps = smooth(q_raw, p)
        assert kl_divergence(qs, ps) >= -1e-9


# ------------------------------------------------------------------- contributors


def test_top_contributors_sum_to_the_full_divergence() -> None:
    q_raw = {("a", 0): 0.6, ("b", 1): 0.3, ("c", 2): 0.1}
    p_raw = {("a", 0): 0.2, ("b", 1): 0.3, ("c", 2): 0.5}
    q, p = smooth(q_raw, p_raw)
    everything = top_contributors(q, p, len(q))
    assert sum(term for _, term in everything) == pytest.approx(kl_divergence(q, p))


def test_top_contributors_ranks_the_biggest_swing_first() -> None:
    q_raw = {("big_swing", 0): 0.8, ("tiny_swing", 1): 0.11}
    p_raw = {("big_swing", 0): 0.1, ("tiny_swing", 1): 0.1}
    q, p = smooth(q_raw, p_raw)
    top = top_contributors(q, p, 1)
    assert top[0][0] == ("big_swing", 0)


def test_missing_contributors_surfaces_a_usual_bucket_absent_today() -> None:
    q_raw = {("app:code", 9): 1.0}
    p_raw = {("app:code", 9): 0.5, ("app:obsidian", 20): 0.5}
    missing = missing_contributors(q_raw, p_raw, 3)
    assert missing == [(("app:obsidian", 20), 0.5)]


def test_missing_contributors_is_empty_when_nothing_usual_is_missing() -> None:
    q_raw = {("app:code", 9): 1.0}
    p_raw = {("app:code", 9): 1.0}
    assert missing_contributors(q_raw, p_raw, 3) == []


# --------------------------------------------------------------- compute_mirror (integration)


def test_no_mirror_below_tau_on_an_ordinary_day() -> None:
    """Fourteen identical days, then a fifteenth that matches them exactly —
    an "ordinary day" must never fire."""
    rows = []
    for age in range(1, 15):
        day = _DAY0 - timedelta(days=age)
        rows.append(_span("app:code", day + timedelta(hours=9), 3600))
    rows.append(_span("app:code", _DAY0 + timedelta(hours=9), 3600))
    result = compute_mirror(rows, _DAY0, **_cfg())
    assert not result.surprising
    assert result.top == []
    assert result.missing == []


def test_mirror_fires_on_a_genuinely_unusual_day() -> None:
    rows = []
    for age in range(1, 15):
        day = _DAY0 - timedelta(days=age)
        rows.append(_span("app:code", day + timedelta(hours=9), 3600 * 2))
    # today: hours doubled in a completely different app
    rows.append(_span("app:spreadsheet", _DAY0 + timedelta(hours=9), 3600 * 4))
    result = compute_mirror(rows, _DAY0, **_cfg(threshold=0.1))
    assert result.surprising
    assert result.kl > 0.1
    assert result.top


def test_no_mirror_with_no_baseline_history_at_all() -> None:
    """Day one of the daemon: today has data, but there is no "usual" yet to
    compare against — must stay silent, not spuriously "surprised" by
    everything."""
    rows = [_span("app:code", _DAY0 + timedelta(hours=9), 3600)]
    result = compute_mirror(rows, _DAY0, **_cfg())
    assert not result.surprising


def test_no_mirror_with_no_activity_today() -> None:
    rows = [_span("app:code", _DAY0 - timedelta(days=1) + timedelta(hours=9), 3600)]
    result = compute_mirror(rows, _DAY0, **_cfg())
    assert not result.surprising
