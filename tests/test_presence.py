# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the tray's state machine (VISION_PHASES.md, `core/presence.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.enums import PresenceState
from neuropaca.core.presence import PresenceInputs, compute_presence_state

_T0 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _inputs(**overrides: object) -> PresenceInputs:
    base: dict[str, object] = {
        "now": _T0,
        "awake_since": _T0 - timedelta(hours=1),
        "dmn_thinking": False,
        "dmn_thinking_since": None,
        "last_moment_at": None,
        "last_insight_at": None,
        "idle_since": None,
        "focused_since": None,
    }
    base.update(overrides)
    return PresenceInputs(**base)  # type: ignore[arg-type]


def test_nothing_known_yet_is_awake() -> None:
    state, since = compute_presence_state(_inputs())
    assert state is PresenceState.AWAKE
    assert since == _T0 - timedelta(hours=1)


def test_focused_when_an_app_has_been_switched_to() -> None:
    since = _T0 - timedelta(minutes=5)
    state, got_since = compute_presence_state(_inputs(focused_since=since))
    assert state is PresenceState.FOCUSED
    assert got_since == since


def test_idle_beats_focused() -> None:
    state, since = compute_presence_state(
        _inputs(
            focused_since=_T0 - timedelta(minutes=30),
            idle_since=_T0 - timedelta(minutes=2),
        )
    )
    assert state is PresenceState.IDLE
    assert since == _T0 - timedelta(minutes=2)


def test_noticed_beats_idle_and_focused_within_the_window() -> None:
    state, since = compute_presence_state(
        _inputs(
            idle_since=_T0 - timedelta(minutes=2),
            last_moment_at=_T0 - timedelta(minutes=3),
        )
    )
    assert state is PresenceState.NOTICED
    assert since == _T0 - timedelta(minutes=3)


def test_noticed_expires_after_ten_minutes() -> None:
    state, _since = compute_presence_state(
        _inputs(
            focused_since=_T0 - timedelta(hours=1),
            last_moment_at=_T0 - timedelta(minutes=11),
        )
    )
    assert state is PresenceState.FOCUSED


def test_noticed_prefers_the_more_recent_of_moment_and_insight() -> None:
    state, since = compute_presence_state(
        _inputs(
            last_moment_at=_T0 - timedelta(minutes=8),
            last_insight_at=_T0 - timedelta(minutes=1),
        )
    )
    assert state is PresenceState.NOTICED
    assert since == _T0 - timedelta(minutes=1)


def test_thinking_beats_everything() -> None:
    state, since = compute_presence_state(
        _inputs(
            dmn_thinking=True,
            dmn_thinking_since=_T0 - timedelta(seconds=5),
            idle_since=_T0 - timedelta(minutes=2),
            last_moment_at=_T0 - timedelta(minutes=1),
        )
    )
    assert state is PresenceState.THINKING
    assert since == _T0 - timedelta(seconds=5)


def test_thinking_flag_without_a_since_does_not_win() -> None:
    """Defensive: a caller that forgets to pass `dmn_thinking_since` must not
    silently win on a `None` — nothing here should ever raise or misreport."""
    state, _since = compute_presence_state(
        _inputs(
            dmn_thinking=True, dmn_thinking_since=None, focused_since=_T0 - timedelta(minutes=1)
        )
    )
    assert state is PresenceState.FOCUSED
