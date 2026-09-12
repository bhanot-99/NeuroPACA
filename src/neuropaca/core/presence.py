# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the tray's state machine (VISION_PHASES.md, "Living presence").

`compute_presence_state` is the one place the precedence
`THINKING > NOTICED > FOCUSED > IDLE > AWAKE` is encoded. It is a pure
function over plain data — no `EventBus`, no socket, no GTK — so it is
testable on its own and so `InterfaceLayer` (which tracks the inputs from
whatever it already subscribes to) and any future caller always agree on
what "noticed" or "focused" *means*, rather than each re-deriving it.

Every "since" input is `None` until its own condition has ever been true —
a fresh daemon reports `AWAKE` from `awake_since` (its own start time) until
the first real signal arrives, never a state it cannot actually justify.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from neuropaca.core.enums import PresenceState

# VISION_PHASES.md: "noticed" = an undelivered moment or new insight in the
# last 10 minutes.
NOTICED_WINDOW_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class PresenceInputs:
    """Everything `compute_presence_state` needs, gathered by the caller from
    whatever events it has already seen. Plain data — safe to construct in a
    test with no daemon, no bus, nothing running."""

    now: datetime
    awake_since: datetime
    dmn_thinking: bool
    dmn_thinking_since: datetime | None
    last_moment_at: datetime | None
    last_insight_at: datetime | None
    idle_since: datetime | None
    focused_since: datetime | None


def compute_presence_state(inputs: PresenceInputs) -> tuple[PresenceState, datetime]:
    """`(state, since)` — `since` is always a timestamp that actually
    justifies the returned state, never a guess."""
    if inputs.dmn_thinking and inputs.dmn_thinking_since is not None:
        return PresenceState.THINKING, inputs.dmn_thinking_since

    noticed_at = _latest(inputs.last_moment_at, inputs.last_insight_at)
    if (
        noticed_at is not None
        and (inputs.now - noticed_at).total_seconds() < NOTICED_WINDOW_SECONDS
    ):
        return PresenceState.NOTICED, noticed_at

    # An open idle spell always wins over "focused" — you cannot be both.
    if inputs.idle_since is not None:
        return PresenceState.IDLE, inputs.idle_since

    if inputs.focused_since is not None:
        return PresenceState.FOCUSED, inputs.focused_since

    return PresenceState.AWAKE, inputs.awake_since


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
