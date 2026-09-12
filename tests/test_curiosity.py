# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · curiosity (VISION_PHASES.md §3.7, `core/curiosity.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from neuropaca.core.curiosity import (
    association_evidence,
    beta_entropy,
    information_gain,
    top_information_gain_pairs,
)
from neuropaca.core.enums import EpisodeKind
from neuropaca.core.episodes import EpisodeRecord

_T0 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _span(subject: str, at: datetime, seq: int) -> EpisodeRecord:
    return EpisodeRecord(
        episode_seq=seq,
        id=f"episode:{seq}",
        kind=str(EpisodeKind.FOCUS_SPAN),
        subject=subject,
        object=None,
        t_start=at,
        t_end=at + timedelta(seconds=30),
        t_valid=None,
        t_invalid=None,
        t_seen=at,
        source="",
        attrs={},
    )


# --------------------------------------------------------------------------- beta_entropy


def test_beta_entropy_is_maximal_at_the_uniform_prior() -> None:
    """Beta(1,1) is uniform — maximum entropy over [0,1]."""
    uniform = beta_entropy(1.0, 1.0)
    confident = beta_entropy(20.0, 1.0)
    assert uniform > confident


def test_beta_entropy_is_symmetric() -> None:
    assert beta_entropy(3.0, 7.0) == pytest.approx(beta_entropy(7.0, 3.0))


# ------------------------------------------------------------------------ information_gain


def test_ig_is_higher_for_uncertain_than_settled_pairs() -> None:
    uncertain = information_gain(0, 0)  # Beta(1,1), maximum uncertainty
    settled_together = information_gain(20, 0)  # confidently "always co-use"
    settled_apart = information_gain(0, 20)  # confidently "never co-use"
    assert uncertain > settled_together
    assert uncertain > settled_apart


def test_ig_is_symmetric_in_s_and_f() -> None:
    """Relabelling which side is "co-use" vs "solo" must not change how much
    is left to learn — Beta(1+s,1+f) and Beta(1+f,1+s) are mirror images."""
    assert information_gain(3, 8) == pytest.approx(information_gain(8, 3))


def test_ig_is_never_negative() -> None:
    for s, f in [(0, 0), (1, 0), (0, 1), (5, 5), (50, 1), (1, 50), (100, 100)]:
        assert information_gain(s, f) >= 0.0


def test_ig_decreases_as_evidence_accumulates_one_sided() -> None:
    """More and more "yes, they co-occur" evidence should keep narrowing the
    posterior — each additional confirming observation teaches a little less."""
    values = [information_gain(s, 0) for s in range(0, 20, 2)]
    assert all(a >= b for a, b in pairwise(values))


# --------------------------------------------------------------------- association_evidence


def test_association_evidence_counts_co_uses_within_the_window() -> None:
    rows = [
        _span("app:a", _T0, 1),
        _span("app:b", _T0 + timedelta(seconds=60), 2),  # within a 300s window
    ]
    s, f = association_evidence(rows, "app:a", "app:b", window_seconds=300.0)
    # the very first occurrence of either app has nothing preceding it to
    # correlate with, so it is unavoidably counted as a solo occurrence —
    # only the second row's "b right after a" is a genuine co-use.
    assert (s, f) == (1, 1)


def test_association_evidence_counts_solo_occurrences_outside_the_window() -> None:
    rows = [
        _span("app:a", _T0, 1),
        _span("app:b", _T0 + timedelta(minutes=30), 2),  # well outside 300s
    ]
    s, f = association_evidence(rows, "app:a", "app:b", window_seconds=300.0)
    assert (s, f) == (0, 2)


def test_association_evidence_is_symmetric_in_u_and_v() -> None:
    rows = [
        _span("app:a", _T0, 1),
        _span("app:b", _T0 + timedelta(seconds=30), 2),
        _span("app:a", _T0 + timedelta(minutes=10), 3),
    ]
    assert association_evidence(rows, "app:a", "app:b", 300.0) == association_evidence(
        rows, "app:b", "app:a", 300.0
    )


def test_association_evidence_ignores_unrelated_subjects_and_kinds() -> None:
    rows = [
        _span("app:a", _T0, 1),
        _span("app:c", _T0 + timedelta(seconds=1), 2),  # a third app, irrelevant
        EpisodeRecord(  # an idle span — not a focus_span, must not count
            episode_seq=3,
            id="episode:3",
            kind=str(EpisodeKind.IDLE_SPAN),
            subject="app:b",
            object=None,
            t_start=_T0,
            t_end=_T0 + timedelta(seconds=1),
            t_valid=None,
            t_invalid=None,
            t_seen=_T0,
            source="",
            attrs={},
        ),
    ]
    s, f = association_evidence(rows, "app:a", "app:b", 300.0)
    assert (s, f) == (0, 1)  # only app:a's solo occurrence counts


# --------------------------------------------------------------- top_information_gain_pairs


def test_top_information_gain_pairs_ranks_the_least_settled_pair_first() -> None:
    rows = [
        # app:a / app:b: always co-occur -> settled, low IG
        *[_span("app:a", _T0 + timedelta(minutes=10 * i), i * 2) for i in range(10)],
        *[
            _span("app:b", _T0 + timedelta(minutes=10 * i, seconds=10), i * 2 + 1)
            for i in range(10)
        ],
        # app:c / app:d: never co-occur, but few observations -> most uncertain
        _span("app:c", _T0 + timedelta(hours=5), 100),
        _span("app:d", _T0 + timedelta(hours=6), 101),
    ]
    ranked = top_information_gain_pairs(
        rows, ["app:a", "app:b", "app:c", "app:d"], window_seconds=300.0, top_n=3
    )
    assert ranked[0][:2] in {("app:c", "app:d")}


def test_top_information_gain_pairs_respects_top_n() -> None:
    rows: list[EpisodeRecord] = []
    ranked = top_information_gain_pairs(
        rows, ["app:a", "app:b", "app:c", "app:d"], window_seconds=300.0, top_n=2
    )
    assert len(ranked) == 2  # C(4,2) = 6 possible pairs, capped at top_n
