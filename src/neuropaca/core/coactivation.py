# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""T7 · `CoactivationWindow` — the rolling "recently focused" state that drives
Hebbian coactivation wiring on every switch (V-1, star-shaped: new focus <->
each recent app, never the recent apps among themselves).

Extracted from `diagnosis/correlator.py` (S0, VISION_PHASES.md §0's "graph-store
consistency") so the live `SignalCorrelator` and the deterministic
`core/graph_rebuild.py` replay apply **the exact same math from the exact same
state shape** — two independent reimplementations of "fire together, wire
together" would drift the moment one changed and the other didn't, and the
whole point of a rebuild is that it reproduces the live graph exactly.

Pure Python, no I/O, no graph access: it only decides *who is warm and by how
much credit*; the caller does the actual `GraphMemory.wire_coactivation` call
and reports back what it wired via `record_wiring`. `now` is a caller-supplied
float — wall-clock seconds for the rebuild (derived from episode timestamps),
`CLOCK_BOOTTIME` for the live correlator — this class only ever computes
differences, never reads a clock itself.
"""

from __future__ import annotations

import math
from collections import deque

# V-1 · a focus held longer than this is not assumed "in use right up to the
# next switch" — past it the user most likely walked away, and the app left
# focused overnight must not wire to the first app of the morning.
MAX_FOCUS_DWELL_SECONDS = 2 * 3600.0


class CoactivationWindow:
    def __init__(self, *, window_seconds: float, max_nodes: int, refractory_seconds: float) -> None:
        self._coactive: deque[tuple[str, float]] = deque(maxlen=max_nodes)
        self._window = window_seconds
        self._refractory = refractory_seconds
        # V-1 · when each pair was last stepped; entries older than the
        # refractory period are swept, so the map holds only recent pairs.
        self._pair_wired_at: dict[frozenset[str], float] = {}

    @property
    def coactive(self) -> tuple[tuple[str, float], ...]:
        """A read-only snapshot — for health/tests, never mutated by a caller."""
        return tuple(self._coactive)

    def warm_peers(self, focus_id: str, now: float) -> list[tuple[str, float]]:
        """Extend the previous focus's dwell (capped at
        `MAX_FOCUS_DWELL_SECONDS` — it was "in use right up to now", not just
        until it was first focused), then return `(peer_id, credit)` for every
        node still inside the window and past its pair refractory. Credit
        falls linearly with the gap: the app switched straight from counts
        fully, one near the edge of the window barely (V-1)."""
        if self._coactive:
            prev, since = self._coactive[-1]
            self._coactive[-1] = (prev, min(now, since + MAX_FOCUS_DWELL_SECONDS))
        return [
            (nid, 1.0 - (now - ts) / self._window)
            for nid, ts in self._coactive
            if nid != focus_id
            and now - ts < self._window
            and now - self._pair_wired_at.get(frozenset((focus_id, nid)), -math.inf)
            >= self._refractory
        ]

    def record_wiring(self, focus_id: str, peers: list[tuple[str, float]], now: float) -> None:
        """Mark every `(focus_id, peer)` pair in `peers` as just wired, so the
        refractory gate skips it next switch. Sweeps the map past 4x the
        window's capacity, same bound the correlator always kept."""
        for nid, _ in peers:
            self._pair_wired_at[frozenset((focus_id, nid))] = now
        cap = 4 * (self._coactive.maxlen or 16)
        if len(self._pair_wired_at) > cap:
            self._pair_wired_at = {
                k: t for k, t in self._pair_wired_at.items() if now - t < self._refractory
            }

    def push_focus(self, focus_id: str, now: float) -> None:
        """Record this focus as the newest entry — a deque with `maxlen`
        evicts the oldest on a right append, so the newest focus always
        survives. Drops any stale copy of `focus_id` first (a re-focus moves
        it to the front rather than duplicating it)."""
        kept = [(nid, ts) for nid, ts in self._coactive if nid != focus_id]
        self._coactive.clear()
        self._coactive.extend(kept)
        self._coactive.append((focus_id, now))


# gen-ref: c3f8e6d2
