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
together, wire together", not two that could drift), sightings (`mark_seen`),
and — since `EpisodicWriter` started recording a span's `object` (the domain
it was classified into at focus time) and, for a webapp, `attrs["browser"]`
(its browser's own node id) — the `domain:*` / browser `PART_OF` structure
too, wired exactly once per subject the same way `SignalCorrelator.
_classify_into_graph` / `_classify_webapp_into_graph` do. It does **not**
reproduce `insight:` / `idle:` nodes: those are model output, not a
deterministic function of the log — replaying them would mean re-running
inference, not a graph replay. A rebuild made from episodes recorded *before*
this build's `EpisodicWriter` (which did not yet capture `object`/`browser`)
simply carries no structural edges for those older spans — the same
graceful-absence a v1 file's missing optional field gets elsewhere in this
codebase, not an error.

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
from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.episodes import EpisodeRecord, EpisodeStore
from neuropaca.core.graph_memory import GraphMemory


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


async def _wire_structure(
    graph: GraphMemory,
    row: EpisodeRecord,
    node_type: NodeType,
    *,
    known_apps: set[str],
    known_webapps: set[str],
) -> None:
    """`domain:*` / browser `PART_OF` edges, wired exactly once per subject —
    mirroring `SignalCorrelator._classify_into_graph` /
    `_classify_webapp_into_graph` (`diagnosis/correlator.py`) so a rebuild's
    structure matches the live one's, not a fresh guess at it. A `focus_span`
    from before `EpisodicWriter` recorded `object`/`attrs["browser"]` simply
    has neither — no edge, not an error."""
    if node_type is NodeType.APP:
        if row.subject in known_apps:
            return
        known_apps.add(row.subject)
        if row.object:
            await graph.add_edge(row.subject, row.object, RelationType.PART_OF)
        return

    if row.subject in known_webapps:
        return
    known_webapps.add(row.subject)
    browser_id = row.attrs.get("browser") if isinstance(row.attrs, dict) else None
    if isinstance(browser_id, str) and browser_id:
        await graph.upsert_node(browser_id, NodeType.APP, {})
        await graph.add_edge(row.subject, browser_id, RelationType.PART_OF)
    if row.object:
        await graph.add_edge(row.subject, row.object, RelationType.PART_OF)


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
    known_apps: set[str] = set()
    known_webapps: set[str] = set()

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
            await _wire_structure(
                fresh, row, node_type, known_apps=known_apps, known_webapps=known_webapps
            )
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


def _has_part_of(graph: GraphMemory, source: str, target: str) -> bool:
    return any(
        e.target_id == target and e.relation is RelationType.PART_OF
        for e in graph.get_edges(source)
    )


async def _wire_structure_if_missing(
    graph: GraphMemory, row: EpisodeRecord, node_type: NodeType
) -> None:
    """`_wire_structure`'s logic (above), but idempotent by *existence check*
    rather than a per-rebuild "known subjects" set — `Scheduler`'s per-tick
    catch-up has no such set to carry across ticks, and re-adding a `PART_OF`
    edge that is already there is a correctness risk (it resets weight, T7)
    that an existence check avoids for a handful of edges at effectively no
    extra cost."""
    if node_type is NodeType.APP:
        if row.object and not _has_part_of(graph, row.subject, row.object):
            await graph.add_edge(row.subject, row.object, RelationType.PART_OF)
        return
    browser_id = row.attrs.get("browser") if isinstance(row.attrs, dict) else None
    if isinstance(browser_id, str) and browser_id:
        await graph.upsert_node(browser_id, NodeType.APP, {})
        if not _has_part_of(graph, row.subject, browser_id):
            await graph.add_edge(row.subject, browser_id, RelationType.PART_OF)
    if row.object and not _has_part_of(graph, row.subject, row.object):
        await graph.add_edge(row.subject, row.object, RelationType.PART_OF)


async def catch_up_focus_span(graph: GraphMemory, row: EpisodeRecord, config: Config) -> None:
    """`Scheduler`'s per-tick self-heal for one missed `focus_span` episode —
    redo, on the *live* graph, what `SignalCorrelator.on_app_switch` would
    have done had its handler not failed (the episode still landed:
    `EpisodicWriter` is a separate, isolated bus subscriber — rules.md §2 —
    so one handler failing does not stop the other from recording what
    happened; only the graph mutation was lost).

    Deliberately **not** a byte-for-byte replay like `rebuild_graph`'s (that
    needs the whole log and a from-scratch `CoactivationWindow`, seeded on
    nothing): "warm peers" here are derived from the *live* graph's own
    `Node.last_seen_at` — a real, persisted, already-available signal — never
    from `SignalCorrelator`'s private in-memory window (rules.md §0: no
    module reaches into another's state). That makes this a reasonable,
    immediate self-heal, not an exact one — the deterministic rebuild
    (`rebuild_graph`, `neuropaca repair-graph`) stays the exact-match repair
    path for whenever that precision actually matters.

    O(graph size) per call (a scan for recently-seen peers) — acceptable
    because a missed row is rare (§0's "zero or one in the normal case"); it
    is never paid on the common, nothing-missed tick.
    """
    if row.kind != "focus_span" or row.t_start is None or row.t_end is None:
        return
    node_type = _node_type_for(row.subject)
    if node_type is None:
        return
    await graph.upsert_node(row.subject, node_type, {})
    await _wire_structure_if_missing(graph, row, node_type)

    warm = graph.warm_activity_peers(row.subject, row.t_start, config.coactivation_window_seconds)
    if warm:
        await graph.wire_coactivation(row.subject, warm, rate=config.hebbian_delta)
    await graph.mark_seen(row.subject, row.t_end)


# gen-ref: 9d4c7b21
