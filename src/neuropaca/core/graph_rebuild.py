# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · the deterministic full rebuild — replay the episode log into a fresh
graph (VISION_PHASES.md, "graph-store consistency"):

> a weekly (or on-demand, `neuropaca repair-graph`) full rebuild replays the
> entire log into a fresh graph and atomically replaces the live one — the
> deterministic-projection guarantee ... requires decay math ... to derive
> elapsed time from consecutive episode timestamps during replay, not the
> wall clock, so a rebuild reproduces the live graph exactly.

**Scope, honestly.** This reproduces the part of the graph the episode log
actually records: the Hebbian coactivation mesh built from `focus_span`
episodes (via the *same* `CoactivationWindow` the live `SignalCorrelator`
uses — core/coactivation.py — so there is one source of truth for "fire
together, wire together", not two that could drift) and sightings
(`mark_seen`). It does **not** reproduce `domain:*` / browser `PART_OF`
structure (that comes from `app_map`/`webapp_map` classification at focus
time, which today's `focus_span` episode does not carry) or `insight:` /
`idle:` nodes (those are model output, not a deterministic function of the
log — replaying them would mean re-running inference, not a graph replay).
A rebuilt graph is therefore the live one's Hebbian mesh exactly, not yet
its full node set. Widening the episode schema to carry classification is
the natural next step, not attempted here.

Decay is exact, not approximated: `decay_cooccurrence_edges(factor, floor)`'s
factor is `0.5 ** (elapsed_hours / half_life)`, and
`0.5**a * 0.5**b == 0.5**(a+b)` — applying it once per consecutive pair of
episode timestamps (however many there are) multiplies out to precisely the
factor one big elapsed-time step would have applied, live-clock or not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from neuropaca.core.coactivation import CoactivationWindow
from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.graph_memory import GraphMemory

_ACTIVITY_PREFIXES = ("app:", "webapp:")


@dataclass(frozen=True, slots=True)
class RebuildStats:
    episodes_replayed: int
    focus_spans: int
    idle_spans: int
    nodes: int
    edges: int
    last_episode_seq: int


async def _decay_for(
    graph: GraphMemory, elapsed: timedelta, half_life_hours: float, floor: float
) -> None:
    hours = elapsed.total_seconds() / 3600.0
    if hours <= 0.0:
        return
    factor = 0.5 ** (hours / half_life_hours)
    await graph.decay_cooccurrence_edges(factor, floor)


def _node_type_for(subject: str) -> NodeType | None:
    if subject.startswith("app:"):
        return NodeType.APP
    if subject.startswith("webapp:"):
        return NodeType.WEBAPP
    return None


async def rebuild_graph(
    store: EpisodeStore, config: Config, *, target_path: str
) -> tuple[GraphMemory, RebuildStats]:
    """Replay the whole log (from `episode_seq` 0) into a brand-new
    `GraphMemory` at `target_path` — a private instance, never the process
    singleton (a caller that also holds the live one is untouched by this).
    Does not save; the caller decides when the replacement is atomic
    (`neuropaca repair-graph`: only once the replay has fully succeeded)."""
    fresh = GraphMemory(target_path)
    await fresh.reset_to_seed()

    window = CoactivationWindow(
        window_seconds=config.coactivation_window_seconds,
        max_nodes=config.coactivation_max_nodes,
        refractory_seconds=config.coactivation_refractory_seconds,
    )
    half_life_hours = config.hebbian_half_life_hours
    hebbian_delta = config.hebbian_delta
    hebbian_floor = config.hebbian_floor

    rows = await store.since(0)
    prev_end: datetime | None = None
    anchor: datetime | None = None
    focus_count = idle_count = 0

    for row in rows:
        if row.kind not in ("focus_span", "idle_span"):
            continue  # a fact row, or an insight — not a span, nothing to replay here
        start, end = row.t_start, row.t_end
        if start is None or end is None:
            continue
        if anchor is None:
            anchor = start

        # The gap *before* this row (dead time between the previous episode
        # ending and this one starting) always decays. A focus span's own
        # duration does not — using it is usage time, not idle time; an idle
        # span's own duration does, below — idle *is* decay-only time. Doing
        # this per-row rather than once for the whole log is exact, not an
        # approximation: 0.5**a * 0.5**b == 0.5**(a+b) (module docstring).
        if prev_end is not None and start > prev_end:
            await _decay_for(fresh, start - prev_end, half_life_hours, hebbian_floor)

        if row.kind == "idle_span":
            idle_count += 1
            if end > start:
                await _decay_for(fresh, end - start, half_life_hours, hebbian_floor)
            prev_end = end
            continue

        focus_count += 1
        node_type = _node_type_for(row.subject)
        if node_type is not None:
            await fresh.upsert_node(row.subject, node_type, {})
            # Relative to `anchor`, never a raw `.timestamp()`: an epoch float
            # for "now" (~1.7e9 for 2026) leaves only ~6-7 digits of
            # sub-second precision in a float64 — enough to nudge a
            # supposedly-exact credit of 1.0 into 0.999... and the saturating
            # Hebbian step away from what the live correlator (whose clock
            # starts at 0, never at epoch scale) computes.
            now = (start - anchor).total_seconds()
            warm = window.warm_peers(row.subject, now)
            if warm:
                await fresh.wire_coactivation(row.subject, warm, rate=hebbian_delta)
                window.record_wiring(row.subject, warm, now)
            window.push_focus(row.subject, now)
            await fresh.mark_seen(row.subject, end)
        prev_end = end

    await fresh.recalculate_importance()
    if rows:
        await fresh.advance_last_episode_seq(rows[-1].episode_seq)

    stats = RebuildStats(
        episodes_replayed=len(rows),
        focus_spans=focus_count,
        idle_spans=idle_count,
        nodes=fresh.node_count,
        edges=fresh.edge_count,
        last_episode_seq=fresh.last_episode_seq,
    )
    return fresh, stats


async def rebuild_graph_to_file(
    store: EpisodeStore, config: Config, *, target_path: str
) -> RebuildStats:
    """`rebuild_graph`, then persist — the atomic-swap half. `GraphMemory.save()`
    writes to a fresh temp file and `os.replace()`s it over `target_path`
    (`graph_memory.py`'s `_write_atomic`), so a crash mid-rebuild never leaves
    a half-written graph where the real one used to be."""
    fresh, stats = await rebuild_graph(store, config, target_path=target_path)
    await fresh.save()
    return stats


# gen-ref: 9d4c7b21
