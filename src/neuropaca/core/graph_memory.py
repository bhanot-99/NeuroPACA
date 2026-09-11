# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L1 · `GraphMemory` — the single behavioural graph (Architecture.md §3.2, D-5/D-6).

Rules that shape this file:
- `networkx.MultiDiGraph`: a pair of nodes can carry several `RelationType`s at
  once, keyed by the relation (D-5).
- one `asyncio.Lock`, held for exactly one graph mutation. Public methods take
  the lock and call a lock-free `_*_unsafe` worker; compound work takes the lock
  once. `asyncio.Lock` is not reentrant — a public method calling another public
  method under the lock deadlocks forever (problems.md 1.10).
- a whole-graph batch job (`recalculate_importance`, `save`) is NOT one atomic
  call: it takes the lock per bounded chunk and `await asyncio.sleep(0)`s between
  chunks so a 10k-node graph never stalls the event loop (rules.md §3,
  problems.md T4). The chunks see a slightly shifting graph — fine for periodic
  best-effort work.
- the 11 routing hubs (`YOU` + 10 `domain:*`) are a protected set: `prune()`
  never removes them, and `find_related()` never traverses *through* them.
- `save()` is atomic: temp file -> fsync -> `os.replace`.
- `relevance_score` is a fixed-scale 0-10 composite; `bridge_value` is live from
  B2.5b (D-10) — a node's distinct `domain:*` hub reach, 0.0 / 0.5 / 1.0.
"""

from __future__ import annotations

import asyncio
import gc
import heapq
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import networkx as nx

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.labels import (
    DROP,
    KIND_PREFIX,
    LabelKind,
    LabelSpec,
    Mode,
    display,
    fact_id,
    is_well_formed,
    leaf_name,
    parse_legacy,
    ref_namer,
    render,
)
from neuropaca.core.models import Edge, Node

# v2 (B5): node records gain an optional `surfaced_at`.
# v3 (B13-B3, D-19): node records gain `ram_mb` / `cpu_percent` / `first_seen_at`
# / `last_seen_at` — durable resource attributes for `app:<id>` nodes. All four
# are optional and default (0.0 / None), so a v2 file loads unchanged: the keys
# are simply absent and `_deserialise` tolerates that.
# v4 (B14): `NodeType.WEBAPP` added — `webapp:<slug>` nodes for identified
# browser tabs. A v3 file loads on v4 code unchanged; a v4 file is REFUSED by a
# v3 reader (its `NodeType` enum has no `webapp` member) — the schema-version
# gate below catches that cleanly before any node is parsed.
# v5 (B18): node records gain `spec` — the structured `LabelSpec` a generated
# node (`insight:` / `idle:` / `ephemeral:`) is about; `label` becomes a render
# cache for those. A v4 file is migrated on load (`_migrate_v4_facts_unsafe`):
# the four legacy label templates are parsed back into specs, ids re-derived
# from fingerprints, duplicates merged. A backup `<file>.pre-b18-backup` is
# written first.
# v6 (V-2): node records gain `activity` — a decaying access counter that
# replaces the separate frequency and recency terms of `relevance_score`. A v5
# file loads unchanged: a missing `activity` defaults to `access_count + 1` as of
# `last_accessed` (the lifetime tally is the best estimate a young graph has).
_SCHEMA_VERSION = 6
_FACT_PREFIXES: tuple[str, ...] = tuple(KIND_PREFIX.values())

# V-2 · relevance_score = 6·activity + 2·strength + 2·bridge, each term 0-1.
# - activity: log1p(decayed access counter) / log1p(the graph's largest);
# - strength: log1p(association strength) / log1p(the graph's largest), where
#   strength is the sum of learned Hebbian weights plus a small constant per
#   other structural edge. Edges to hubs, and a generated node's edges to what
#   it is about (provenance, not relevance), count nothing;
# - bridge: distinct `domain:*` hubs reached directly or through an association
#   of at least `_BRIDGE_MIN_WEIGHT`: one domain 0, two 0.5, three or more 1.
_ACTIVITY_HALF_LIFE_DAYS = 7.0
_W_ACTIVITY, _W_STRENGTH, _W_BRIDGE = 6.0, 2.0, 2.0
_STRUCTURAL_EDGE_STRENGTH = 0.1
_BRIDGE_MIN_WEIGHT = 0.1  # ~ one full-credit co-use at the default hebbian_delta
_DERIVED_PREFIXES: tuple[str, ...] = (*_FACT_PREFIXES, "action:")
# `labels.py` is stdlib-only (the graph window loads it by path), so the
# kind -> NodeType half of the table lives here.
_KIND_NODE_TYPE: dict[LabelKind, NodeType] = {
    LabelKind.INSIGHT: NodeType.INSIGHT,
    LabelKind.THOUGHT: NodeType.IDLE_THOUGHT,
    LabelKind.PROBE: NodeType.CONCEPT,
}

# The oldest on-disk version this build can still read. v1/v2/v3 differ only by
# added optional keys that `_deserialise` already tolerates when absent, so no
# migration step is needed yet — when one is, add it to `_migrate` rather than
# widening this window silently.
_MIN_READABLE_SCHEMA_VERSION = 1


def graph_schema_version() -> int:
    """The on-disk graph schema version this build writes (B9/BL-3)."""
    return _SCHEMA_VERSION


DOMAIN_SLUGS: tuple[str, ...] = (
    "engineering",
    "research",
    "tools",
    "system",
    "habits",
    "projects",
    "meetings",
    "comms",
    "mental_models",
    "learning",
)
DOMAIN_HUB_IDS: frozenset[str] = frozenset(f"domain:{slug}" for slug in DOMAIN_SLUGS)
HUB_NODE_IDS: frozenset[str] = DOMAIN_HUB_IDS | {"YOU"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_cooccurrence_node(node_id: str) -> bool:
    """Node ids eligible for a *new* Hebbian co-occurrence edge (T7). Existing
    edges between any node types are still reinforced — this gate only keeps
    edge *creation* to app/web-app affinity so the mesh does not fill with
    `file:` / `concept:` noise."""
    return node_id.startswith(("app:", "webapp:"))


def _hebbian_step(weight: float, rate: float) -> float:
    """V-1 · saturating Hebbian update: close `rate` of the remaining gap to 1.0.
    Weights stay in [0, 1) however often a pair recurs, so a weight reads as an
    affinity on a fixed scale instead of a raw, unbounded co-occurrence count."""
    return weight + rate * (1.0 - weight)


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _activity_at(data: dict[str, Any], when: datetime) -> float:
    """V-2 · a node's decaying access counter as of `when`. The stored value is
    as of `last_accessed`; it halves every `_ACTIVITY_HALF_LIFE_DAYS` after."""
    last = _as_dt(data.get("last_accessed", when))
    age_days = max(0.0, (when - last).total_seconds() / 86400.0)
    return float(data.get("activity", 0.0)) * math.pow(0.5, age_days / _ACTIVITY_HALF_LIFE_DAYS)


def _as_dt_opt(value: Any) -> datetime | None:
    if value is None:
        return None
    return _as_dt(value)


def _as_spec(value: Any) -> LabelSpec | None:
    if value is None or isinstance(value, LabelSpec):
        return value
    return LabelSpec.from_record(value)


class GraphMemory:
    """CRUD, traversal, scoring, consolidation, and atomic persistence for the graph."""

    _instance: ClassVar[GraphMemory | None] = None

    def __init__(self, persistence_path: str | Path) -> None:
        self._path = Path(persistence_path)
        self._graph: Any = nx.MultiDiGraph()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._last_save: datetime | None = None
        self._ref_name = ref_namer(self._lookup)  # B18 · names refs inside labels

    # ------------------------------------------------------------------ singleton
    @classmethod
    def get_instance(cls, persistence_path: str | Path | None = None) -> GraphMemory:
        if cls._instance is None:
            if persistence_path is None:
                raise GraphMemoryError("first GraphMemory.get_instance() needs a persistence_path")
            cls._instance = cls(persistence_path)
        return cls._instance

    @classmethod
    def _reset_for_tests(cls) -> None:
        cls._instance = None
        gc.unfreeze()  # undo load()'s gc.freeze so test graphs stay collectable

    # ---------------------------------------------------------------- properties
    @property
    def node_count(self) -> int:
        return int(self._graph.number_of_nodes())

    @property
    def edge_count(self) -> int:
        return int(self._graph.number_of_edges())

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def node_ids(self) -> list[str]:
        """A snapshot of every node id (read-only — for ranking / iteration)."""
        return list(self._graph.nodes)

    # ----------------------------------------------------------------- mutations
    async def add_node(
        self, node_id: str, node_type: NodeType, attributes: dict[str, Any] | None = None
    ) -> Node:
        async with self._lock:
            return self._add_node_unsafe(node_id, node_type, attributes or {})

    async def upsert_node(
        self, node_id: str, node_type: NodeType, attributes: dict[str, Any] | None = None
    ) -> Node:
        """Get-or-create under one lock (D-8). On a missing node, create it like
        `add_node()`. On an existing node, merge only the supplied `attributes`
        (except the protected `created_at` / `relevance_score` / `node_type`),
        bump `access_count`, refresh `last_accessed` — never reset the score.
        Use this, not `add_node()`, for any entity a module touches repeatedly."""
        async with self._lock:
            return self._upsert_node_unsafe(node_id, node_type, attributes or {})

    async def upsert_fact(self, spec: LabelSpec) -> tuple[Node, bool]:
        """B18 · the one write path for every generated node (L4 insight, L6
        thought, L8 probe). The id is `fact_id(spec)`, so the same fact is the
        same node — across restarts too, with no side index. A repeat is
        *reinforced* (`access_count`, `last_accessed`, latest `value`); a new
        fact is created and edged `RELATED_TO` each ref that exists. One lock
        cycle. Returns `(node, created)`."""
        async with self._lock:
            return self._upsert_fact_unsafe(spec)

    def find_fact(self, spec: LabelSpec) -> Node | None:
        """The stored node for this fact, or None. A pure id lookup."""
        return self.get_node(fact_id(spec))

    def display_name(self, node_id: str, mode: Mode = "full") -> str:
        """B18 · the readable name of any node, rendered now from its spec (so
        it reflects every rename) or, for a leaf, from its label."""
        if node_id not in self._graph:
            return leaf_name(node_id, "")
        data = self._graph.nodes[node_id]
        return display(node_id, str(data.get("label", "")), data.get("spec"), self._ref_name, mode)

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: RelationType,
        weight: float = 0.0,
    ) -> Edge:
        async with self._lock:
            return self._add_edge_unsafe(source_id, target_id, relation, weight)

    async def update_node(self, node_id: str, attributes: dict[str, Any]) -> None:
        async with self._lock:
            self._update_node_unsafe(node_id, attributes)

    async def reinforce_edge(self, node_a: str, node_b: str, delta: float = 0.01) -> int:
        """Hebbian co-occurrence bump (Architecture.md §6, D-11): add `delta` to
        the `weight` of every existing edge between `node_a` and `node_b`, in
        either direction and across all parallel `RelationType`s. Creates
        nothing — "if the edge exists". Returns the number of edges bumped. One
        `_lock` cycle."""
        async with self._lock:
            return self._reinforce_edge_unsafe(node_a, node_b, delta)

    async def reinforce_cooccurrence(self, node_ids: Sequence[str], delta: float = 0.01) -> int:
        """One episode's Hebbian update: bump every *existing* edge between every
        pair of `node_ids` (both directions, all parallel relations) by `delta`.
        Creates nothing. Returns the number of edges bumped. **One `_lock`
        cycle** — this is a single insight's co-occurrence set, bounded by
        `Config`'s L4 context K (a few nodes in production); the pair count is
        O(k^2) of small dict lookups, well inside a chunk (rules.md §3)."""
        unique = list(dict.fromkeys(node_ids))
        async with self._lock:
            bumped = 0
            for i, a in enumerate(unique):
                for b in unique[i + 1 :]:
                    bumped += self._reinforce_edge_unsafe(a, b, delta)
            return bumped

    async def wire_cooccurrence(
        self,
        node_ids: Sequence[str],
        *,
        delta: float,
        max_episode: int = 8,
        max_new_edges: int = 12,
    ) -> tuple[int, int]:
        """Hebbian "fire together, wire together" for one *episode* whose
        members genuinely co-occurred all at once — the L4 insight path (T7).
        For every unordered pair of `node_ids`:

        - two activity nodes (`app:` / `webapp:`) get their association edge
          (`RELATED_TO`) stepped by `delta` with the saturating rule
          (`_hebbian_step`), created on first co-occurrence — unless the pair is
          already structurally linked by `PART_OF` (a tab and its own browser),
          which says nothing new (V-1);
        - any other pair only has an *existing* `RELATED_TO` edge bumped by
          `delta`. `PART_OF` edges are structure and never carry Hebbian weight.

        A focus switch is *not* an episode — it is one new focus against a set of
        recent ones; use `wire_coactivation` for that, or every recent app gets
        re-wired to every other on each switch (the V-1 clique). `node_ids` is
        taken most-relevant-first and truncated to `max_episode`; no more than
        `max_new_edges` edges are created per call. Ids absent from the graph are
        skipped. **One `_lock` cycle** — O(max_episode^2) small dict ops.
        Returns `(created, bumped)`.
        """
        unique = list(dict.fromkeys(node_ids))[:max_episode]
        created = bumped = 0
        async with self._lock:
            present = [n for n in unique if n in self._graph]
            for i, a in enumerate(present):
                for b in present[i + 1 :]:
                    if _is_cooccurrence_node(a) and _is_cooccurrence_node(b):
                        if created >= max_new_edges and not self._related_between_unsafe(a, b):
                            continue
                        outcome = self._association_step_unsafe(a, b, delta)
                        created += outcome == "created"
                        bumped += outcome == "bumped"
                    else:
                        bumped += self._reinforce_edge_unsafe(
                            a, b, delta, relation=RelationType.RELATED_TO
                        )
        return created, bumped

    async def wire_coactivation(
        self, focus_id: str, peers: Sequence[tuple[str, float]], *, rate: float
    ) -> tuple[int, int]:
        """V-1 · the focus-switch Hebbian update. Step the association edge
        between `focus_id` and each recently active `(peer, strength)` by
        `rate * strength` (saturating, `_hebbian_step`), creating it on first
        co-activation. `strength` in (0, 1] is how close in time the peer was
        last active — a switch straight from A to B is full credit, one near
        the edge of the window is little.

        **Star-shaped:** only focus<->peer pairs are touched, never peer<->peer.
        Wiring all pairs of the window on every switch re-bumped every recent
        app against every other no matter what was focused, which is exactly
        what grew the 6-node clique. Only activity nodes (`app:` / `webapp:`)
        take part; a pair already linked by `PART_OF` is skipped. One `_lock`
        cycle, O(len(peers)). Returns `(created, bumped)`."""
        created = bumped = 0
        async with self._lock:
            if focus_id not in self._graph or not _is_cooccurrence_node(focus_id):
                return 0, 0
            for peer, strength in peers:
                if (
                    peer == focus_id
                    or strength <= 0.0
                    or peer not in self._graph
                    or not _is_cooccurrence_node(peer)
                ):
                    continue
                outcome = self._association_step_unsafe(focus_id, peer, rate * min(1.0, strength))
                created += outcome == "created"
                bumped += outcome == "bumped"
        return created, bumped

    async def decay_cooccurrence_edges(self, factor: float, floor: float) -> int:
        """ "Use it or lose it" for the Hebbian mesh (T7, B6 idle sweep). Every
        `RELATED_TO` edge with `weight > 0` is multiplied by `factor` (<= 1 —
        the caller derives it from elapsed time, V-1); an edge that then sits
        below `floor` is removed — unless it is the last edge on either endpoint
        (never re-orphan a node; `link_orphan_nodes` would only re-add a
        `-> YOU` edge right after). Hebbian weight on any other relation is
        stray — pre-V-1 builds bumped structural `PART_OF` edges — and is reset
        to 0.0 here, so the sweep also heals an old graph. One `_lock` cycle.
        Returns edges pruned.
        """
        async with self._lock:
            faded: list[tuple[str, str, Any]] = []
            touched = False
            for u, v, key, data in self._graph.edges(keys=True, data=True):
                weight = float(data.get("weight", 0.0))
                if weight <= 0.0:
                    continue
                if RelationType(key) is not RelationType.RELATED_TO:
                    data["weight"] = 0.0
                    touched = True
                    continue
                data["weight"] = round(weight * factor, 6)
                touched = True
                if data["weight"] < floor:
                    faded.append((u, v, key))
            pruned = 0
            for u, v, key in faded:
                if int(self._graph.degree(u)) <= 1 or int(self._graph.degree(v)) <= 1:
                    continue
                self._graph.remove_edge(u, v, key)
                pruned += 1
            if touched:
                self._dirty = True
            return pruned

    async def delete_node(self, node_id: str) -> None:
        async with self._lock:
            self._delete_node_unsafe(node_id)

    async def prune(self, older_than: timedelta, min_importance: float) -> int:
        async with self._lock:
            return self._prune_unsafe(older_than, min_importance)

    _RECALC_CHUNK: ClassVar[int] = 250

    async def recalculate_importance(self) -> None:
        """Rescore every node. This is a long CPU job, so — per rules.md §3 — it
        does NOT hold the lock around the batch loop: it takes the lock for one
        bounded chunk at a time and yields between chunks so queued events (and,
        from B4, in-process inference) get the event loop. A node added or
        removed between chunks is simply picked up on the next pass.

        V-2: two passes. The first, read-only, finds the graph's largest
        activity and association strength — the scale the second pass scores
        against, so the 0-10 range stays in use as the graph ages instead of a
        fixed constant saturating the busiest nodes and flattening the rest."""
        now = _utcnow()
        node_ids = list(self._graph.nodes)  # sync snapshot of ids, no await
        chunks = [
            node_ids[start : start + self._RECALC_CHUNK]
            for start in range(0, len(node_ids), self._RECALC_CHUNK)
        ]
        top_activity = top_strength = 0.0
        profiles: dict[str, tuple[float, float]] = {}  # pass one's edge walks
        for chunk in chunks:
            async with self._lock:
                act, strength = self._score_scale_unsafe(chunk, now, profiles)
            top_activity, top_strength = max(top_activity, act), max(top_strength, strength)
            await asyncio.sleep(0)
        scale = (math.log1p(top_activity), math.log1p(top_strength))
        for chunk in chunks:
            async with self._lock:
                self._recalculate_chunk_unsafe(chunk, now, scale, profiles)
            await asyncio.sleep(0)  # explicit yield to the event loop

    # B6 · Idle Cognition (L6, D-13). The DMN's "reminiscence" — housekeeping the
    # graph while you are away. Each of the three is a whole-graph best-effort
    # job, so — like `recalculate_importance` — it takes the lock **once per
    # atomic mutation** and yields between, never once around a batch loop
    # (rules.md §3). A mid-cycle `asyncio.CancelledError` (activity resumed) then
    # lands cleanly between two mutations, never inside one.

    async def consolidate(self) -> int:
        """Merge exact-duplicate nodes. A duplicate is **identical `node_type`
        AND case-insensitive `label`** (D-13). The older `created_at` survives;
        `access_count` sums, `relevance_score` averages, every edge rewires to the
        survivor. The 11 routing hubs are never touched.

        Still one `_lock` cycle per merge with a yield between — cancellation
        lands between two merges, never inside one — but the *scan* is one pass
        per sweep rather than one per merge. Re-scanning per merge made this
        O(duplicates x nodes): every merge walked the whole graph again to find
        the next pair. The sweep repeats until a pass merges nothing, so a
        duplicate that appears mid-run is still caught. Returns merges done."""
        merged = 0
        while True:
            async with self._lock:
                pairs = self._find_duplicate_pairs_unsafe()
            if not pairs:
                break
            swept = 0
            for survivor, victim in pairs:
                async with self._lock:
                    # Re-checked under the lock: the pair was chosen from a
                    # snapshot, and an earlier merge in this sweep may already
                    # have consumed one end of it.
                    if self._merge_nodes_unsafe(survivor, victim):
                        swept += 1
                await asyncio.sleep(0)
            merged += swept
            if swept == 0:
                break  # nothing in that pass was still mergeable — stop, do not spin
        return merged

    async def canonicalise_app_nodes(
        self,
        resolve: Callable[[str], str],
        is_non_app: Callable[[str], bool],
    ) -> tuple[int, int]:
        """B17 · fold every `app:` / `webapp:` node onto one canonical id.

        The focus sensor keys these by Wayland `app_id`
        (`app:com.system76.CosmicFiles`), the B13 census by process name
        (`app:cosmic-files`) — so one real app is two or three nodes, the
        behavioural edges on one and the RAM/CPU on another. `resolve(bare id)`
        gives the canonical slug; nodes that collapse to the same slug are merged
        (`_merge_nodes_unsafe` — D-13 math, edge rewiring, resource-attr fold),
        the surviving node renamed to `app:<slug>` / `webapp:<slug>`.

        A node whose bare id `is_non_app` (a thread label / bare shell the census
        mistook for a process — `is_non_app` is written to only ever match those)
        is deleted, with its edges. `access_count` is not a "was focused" signal —
        it bumps on every census upsert too — so `is_non_app` alone is the gate.

        One `_lock` cycle per mutation, a yield between (cancellation lands
        between two mutations, never inside one). **Idempotent** — a second call
        returns `(0, 0)`. Run once by the orchestrator after `load()`.
        Returns `(merged, dropped)`.
        """
        merged = dropped = 0

        # (1) drop thread-label / bare-shell nodes the census mistook for apps
        async with self._lock:
            junk = [
                nid
                for nid in list(self._graph.nodes)
                if nid.startswith("app:") and is_non_app(nid[4:])
            ]
        for nid in junk:
            async with self._lock:
                if nid in self._graph and nid not in HUB_NODE_IDS:
                    self._graph.remove_node(nid)
                    self._dirty = True
                    dropped += 1
            await asyncio.sleep(0)

        # (2) group the survivors by canonical id
        async with self._lock:
            groups: dict[str, list[str]] = {}
            for nid in list(self._graph.nodes):
                for prefix in ("app:", "webapp:"):
                    if nid.startswith(prefix):
                        bare = nid[len(prefix) :]
                        canon = resolve(bare) or bare
                        groups.setdefault(f"{prefix}{canon}", []).append(nid)
                        break

        for canon_id, members in groups.items():
            if len(members) == 1 and members[0] == canon_id:
                continue  # already canonical, nothing to do
            # survivor: prefer the exact canonical id, else the most-connected
            async with self._lock:
                present = [m for m in members if m in self._graph]
                if not present:
                    continue
                if canon_id in present:
                    survivor = canon_id
                else:
                    survivor = max(
                        present,
                        key=lambda m: (
                            self._graph.degree(m),
                            -_as_dt(self._graph.nodes[m].get("created_at", _utcnow())).timestamp(),
                        ),
                    )
                    nx.relabel_nodes(self._graph, {survivor: canon_id}, copy=False)
                    self._graph.nodes[canon_id]["label"] = canon_id.split(":", 1)[1]
                    self._dirty = True
                    survivor = canon_id
            for victim in present:
                if victim == survivor:
                    continue
                async with self._lock:
                    if self._merge_nodes_unsafe(survivor, victim):
                        merged += 1
                await asyncio.sleep(0)

        # (3) B18 · heal fact refs. A spec naming `app:brave-browser` now names
        # `app:brave`; its fingerprint (and so its id) moves with it, and two
        # facts that became one are merged. Labels re-render from the new names.
        def remap(ref: str) -> str:
            for prefix in ("app:", "webapp:"):
                if ref.startswith(prefix):
                    bare = ref[len(prefix) :]
                    return f"{prefix}{resolve(bare) or bare}"
            return ref

        async with self._lock:
            pending = self._refresh_facts_unsafe(remap)
        for survivor, victim in pending:
            async with self._lock:
                if self._merge_nodes_unsafe(survivor, victim):
                    merged += 1
            await asyncio.sleep(0)
        async with self._lock:
            self._rerender_labels_unsafe()

        return merged, dropped

    async def release_you_links(self) -> int:
        """V-3a · take the `-> YOU` placeholder back off a node that has since
        gained a real edge. `link_orphan_nodes` gives a degree-0 node a
        `RELATED_TO YOU` edge so ordinary decay can manage it, but nothing ever
        removed that edge again — every app that was briefly an orphan (before
        its domain edge or first co-use arrived) kept it for life, and `YOU`
        became a hub of stale spokes. Only that exact placeholder (node ->
        `YOU`, `RELATED_TO`, weight 0) is removed, and only while the node keeps
        an edge to something other than `YOU` — this never re-orphans a node.
        Candidates in one pass; one `_lock` cycle per removal with a yield
        between (rules.md §3). Returns placeholders removed."""
        async with self._lock:
            candidates = (
                [n for n in self._graph.predecessors("YOU") if n not in HUB_NODE_IDS]
                if "YOU" in self._graph
                else []
            )
        released = 0
        for node_id in candidates:
            async with self._lock:
                if self._is_stale_you_link_unsafe(node_id):
                    self._graph.remove_edge(node_id, "YOU", RelationType.RELATED_TO)
                    self._dirty = True
                    released += 1
            await asyncio.sleep(0)
        return released

    def _is_stale_you_link_unsafe(self, node_id: str) -> bool:
        rel = RelationType.RELATED_TO
        if node_id not in self._graph or not self._graph.has_edge(node_id, "YOU", rel):
            return False
        if float(self._graph.edges[node_id, "YOU", rel].get("weight", 0.0)) > 0.0:
            return False  # not the placeholder — something set a real weight on it
        graph = self._graph
        return any(n != "YOU" for n in (*graph._succ[node_id], *graph._pred[node_id]))

    async def link_orphan_nodes(self) -> int:
        """Give every non-hub node with total degree 0 a `RELATED_TO` edge to
        `YOU`, so ordinary score decay can then manage it rather than it floating
        forever unreachable (D-13). One `_lock` cycle per link. Returns links
        made.

        Like `consolidate`, the orphan set is collected in one pass per sweep and
        each candidate re-checked under the lock before it is linked. The old
        shape restarted the scan at node 0 for *every* link, which on a graph of
        mostly-unlinked nodes cost seconds — more than the whole DMN cycle
        budget it runs inside."""
        linked = 0
        while True:
            async with self._lock:
                orphans = self._orphan_ids_unsafe()
            if not orphans:
                break
            swept = 0
            for orphan in orphans:
                async with self._lock:
                    if self._is_orphan_unsafe(orphan):
                        self._add_edge_unsafe(orphan, "YOU", RelationType.RELATED_TO, 0.0)
                        swept += 1
                await asyncio.sleep(0)
            linked += swept
            if swept == 0:
                break
        return linked

    async def prune_dead_hubs(self) -> int:
        """V-5 · drop a `domain:` hub that nothing routes to.

        All ten hubs are seeded on a fresh graph so a first run is
        self-describing, but only what you actually do ever attaches to one.
        On the live graph five sat at degree 0 forever — `domain:comms`,
        `domain:projects` and `domain:meetings` because those apps had not been
        opened yet, `domain:system` and `domain:mental_models` because **no
        entry in either map file routes to them at all**, so nothing could ever
        reach them. Either way they were dead weight in every graph view and an
        empty branch in `find_related`.

        Reaping rather than never-seeding is deliberate: it also catches a hub
        that *becomes* dead (its last app uninstalled, its mapping removed), and
        `_add_edge_unsafe` materialises a hub again the instant something routes
        to it, so nothing is lost — a reaped hub is one keystroke from
        returning. `YOU` is never reaped: it is the anchor `link_orphan_nodes`
        attaches true orphans to.

        One `_lock` cycle per removal with a yield between (rules.md §3).
        Returns hubs dropped.
        """
        async with self._lock:
            candidates = [hub for hub in sorted(DOMAIN_HUB_IDS) if self._is_dead_hub_unsafe(hub)]
        dropped = 0
        for hub in candidates:
            async with self._lock:
                if self._is_dead_hub_unsafe(hub):
                    self._graph.remove_node(hub)
                    self._dirty = True
                    dropped += 1
            await asyncio.sleep(0)
        return dropped

    def _is_dead_hub_unsafe(self, hub_id: str) -> bool:
        if hub_id not in DOMAIN_HUB_IDS or hub_id not in self._graph:
            return False
        return int(self._graph.degree(hub_id)) == 0

    async def prune_stale_nodes(self, ttl: timedelta) -> int:
        """Drop a non-hub node when its `relevance_score` has decayed to ~0, or
        it has aged past `ttl` (D-13). An `INSIGHT` / `IDLE_THOUGHT` node past
        `ttl` goes regardless of score — that is the 48 h idle-thought cache
        lifetime (Architecture.md §8). A node younger than `ttl` on **both**
        `created_at` and `last_accessed` is left alone: the Scheduler owns
        `recalculate_importance`, so a fresh node still sitting at the default
        score 0.0 must not be mistaken for a decayed one. One `_lock` cycle."""
        async with self._lock:
            return self._prune_stale_unsafe(ttl)

    # ------------------------------------------------------------------ queries
    def has_node(self, node_id: str) -> bool:
        """Membership only — no `Node` is built (a hot-path check, V-1)."""
        return node_id in self._graph

    def get_node(self, node_id: str) -> Node | None:
        if node_id not in self._graph:
            return None
        return self._node_from_attrs(node_id, self._graph.nodes[node_id])

    def get_edges(self, node_id: str) -> list[Edge]:
        if node_id not in self._graph:
            return []
        edges: list[Edge] = []
        for u, v, key, data in self._graph.out_edges(node_id, keys=True, data=True):
            edges.append(self._edge_from_attrs(u, v, key, data))
        for u, v, key, data in self._graph.in_edges(node_id, keys=True, data=True):
            edges.append(self._edge_from_attrs(u, v, key, data))
        return edges

    def find_related(self, node_id: str, depth: int, *, traverse_hubs: bool = False) -> list[Node]:
        """Breadth-first neighbourhood. Never expands a hub node's edges unless
        `traverse_hubs=True` — a depth-2 walk through `YOU` would otherwise reach
        the whole graph and blow the < 50 ms target (D-5)."""
        if node_id not in self._graph:
            return []
        visited: set[str] = {node_id}
        frontier: set[str] = {node_id}
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for current in frontier:
                if not traverse_hubs and current in HUB_NODE_IDS and current != node_id:
                    continue
                nxt.update(self._graph.successors(current))
                nxt.update(self._graph.predecessors(current))
            frontier = nxt - visited
            visited |= frontier
            if not frontier:
                break
        visited.discard(node_id)
        return [self._node_from_attrs(n, self._graph.nodes[n]) for n in visited if n in self._graph]

    def top_nodes_by_score(
        self,
        limit: int,
        *,
        exclude_types: frozenset[NodeType] | None = None,
        exclude_prefixes: tuple[str, ...] = (),
    ) -> list[Node]:
        """The `limit` highest-`relevance_score` non-hub nodes, best first.

        Ranking reads the raw attribute dicts and only the survivors are built
        into `Node`s. The caller-side shape this replaces (`get_node()` for every
        id, then sort) constructed the whole graph as dataclasses — datetime
        parsing included — to keep five of them, ~32 ms of unbroken event-loop
        block at 10k nodes, every DMN cycle. Equal scores break on id, so the
        result is deterministic. Kept here rather than in the caller because reaching into
        `graph_memory.graph` from outside this file is a review block (rules.md §3).
        """
        if limit <= 0:
            return []
        excluded = exclude_types or frozenset()
        ranked = heapq.nlargest(
            limit,
            (
                (float(data.get("relevance_score", 0.0)), node_id)
                for node_id, data in self._graph.nodes(data=True)
                if node_id not in HUB_NODE_IDS
                and NodeType(data["node_type"]) not in excluded
                and not node_id.startswith(exclude_prefixes)
            ),
            key=lambda pair: (pair[0], pair[1]),
        )
        return [self._node_from_attrs(node_id, self._graph.nodes[node_id]) for _, node_id in ranked]

    def search_by_label(self, query: str, limit: int = 10) -> list[Node]:
        """L9 retrieval entry point (B5, A1). A deliberately dumb lexical match —
        **zero embeddings, zero inference** (rules.md §4, problems.md 1.6 spirit):

        - case-insensitive substring of `query` (whole, then each word ≥ 3 chars)
          against every node's `label` — a strict O(N) scan;
        - plus an *exact* word match against a routing hub's slug (``engineering``
          -> ``domain:engineering``, ``you`` -> ``YOU``) so a domain question
          seeds from the hub even when nothing else matches.

        Results are ranked by `relevance_score` (desc), then label, and capped at
        `limit`. `_build_context` walks `find_related()` out from these seeds."""
        q = query.strip().lower()
        if not q:
            return []
        words = {w for w in q.replace("/", " ").split() if len(w) >= 3}
        needles = {q, *words}

        hits: dict[str, Node] = {}
        for word in words:
            if word in DOMAIN_SLUGS and f"domain:{word}" in self._graph:
                hits["domain:" + word] = self._node_from_attrs(
                    "domain:" + word, self._graph.nodes["domain:" + word]
                )
        if "you" in words and "YOU" in self._graph:
            hits["YOU"] = self._node_from_attrs("YOU", self._graph.nodes["YOU"])

        for node_id, data in self._graph.nodes(data=True):
            if node_id in hits:
                continue
            label = str(data.get("label", "")).lower()
            if any(n in label for n in needles):
                hits[node_id] = self._node_from_attrs(node_id, data)

        ranked = sorted(hits.values(), key=lambda n: (-n.relevance_score, n.label))
        return ranked[:limit]

    # --------------------------------------------------------------- persistence
    def _read_payload(self) -> Any:
        """BLOCKING — read + decode the graph file. Runs in a worker thread."""
        return json.loads(self._path.read_text("utf-8"))

    async def load(self) -> None:
        # Read and decode off the loop, *before* taking the lock. `save()` has
        # always offloaded its file I/O (rules.md §3); `load()` did not, so a
        # multi-MB graph blocked the event loop for the whole read plus decode —
        # while holding `_lock`, so every other module stalled behind it too.
        # Boot is not the only caller: BL-2 recovery re-enters this path.
        payload: Any = None
        exists = await asyncio.to_thread(self._path.exists)
        if exists:
            try:
                payload = await asyncio.to_thread(self._read_payload)
            except (OSError, ValueError) as exc:
                raise GraphMemoryError(f"cannot load graph {self._path}: {exc}") from exc
        # Only a pre-v5 file that actually holds generated nodes without a spec
        # has anything to migrate — an old graph of apps and files does not, and
        # must not leave a backup file behind (e.g. next to a test fixture).
        needs_fact_migration = (
            isinstance(payload, dict)
            and isinstance(payload.get("schema_version", 1), int)
            and payload.get("schema_version", 1) < 5
            and any(
                isinstance(raw, dict)
                and str(raw.get("id", "")).startswith(_FACT_PREFIXES)
                and raw.get("spec") is None
                for raw in payload.get("nodes", ())
            )
        )
        if needs_fact_migration:
            # B18 · keep the pre-migration file once — the migration rewrites ids.
            backup = self._path.with_name(self._path.name + ".pre-b18-backup")
            if not await asyncio.to_thread(backup.exists):
                await asyncio.to_thread(shutil.copy2, self._path, backup)

        async with self._lock:
            if payload is not None:
                if not isinstance(payload, dict):
                    raise GraphMemoryError(
                        f"cannot load graph {self._path}: top level is "
                        f"{type(payload).__name__}, expected an object"
                    )
                try:
                    self._graph = self._deserialise(payload)
                except GraphMemoryError:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    # A truncated or hand-edited record. Surface it as the one
                    # exception type callers handle (BL-2) rather than leaking a
                    # bare KeyError out of the node loop.
                    raise GraphMemoryError(
                        f"cannot load graph {self._path}: malformed record ({exc!r})"
                    ) from exc
            if self._graph.number_of_nodes() == 0:
                self._seed_hubs_unsafe()
            self._dirty = False
            if needs_fact_migration:
                self._migrate_v4_facts_unsafe()
                self._dirty = True  # persist the migrated form on the next tick
        # Move the whole graph into GC's permanent generation: it is long-lived
        # and large (10k+ node/edge attr dicts), and without this every gen-2
        # collection triggered by unrelated churn — notably save()'s transient
        # records — rescans it, stalling the loop ~25 ms (problems.md T4).
        gc.collect()
        gc.freeze()

    async def reset_to_seed(self) -> None:
        """Drop everything and come back as a bare 11-hub graph (B9/BL-2).

        Used only by the orchestrator's boot recovery, after the on-disk graph
        has been quarantined. It mutates *this* instance rather than building a
        new one because `GraphMemory` is a singleton already handed to the
        modules, so a replacement object would leave them pointing at the old
        one. `_dirty` is left True so the reseeded graph is persisted on the next
        scheduler tick.
        """
        async with self._lock:
            self._graph = nx.MultiDiGraph()
            self._seed_hubs_unsafe()
            self._dirty = True
        gc.collect()
        gc.freeze()

    _SAVE_CHUNK: ClassVar[int] = 500

    async def save(self) -> None:
        """Serialise the graph in bounded chunks — take the lock, encode one
        chunk of nodes/edges to JSON, release, yield — so the event loop never
        stalls longer than a chunk even for a 10k-node graph (problems.md T4,
        rules.md §3). `indent`/`sort_keys` force json's *pure-Python* encoder
        (~200 ms for 10k nodes); per-object compact `dumps` uses the C encoder,
        µs each. The atomic file write (GIL-releasing I/O) then runs in a worker
        thread (Architecture.md §14, rules.md §1).

        `_dirty` is cleared *before* streaming: a mutation mid-save flips it back
        on, so the next tick re-persists — the on-disk file is always valid JSON,
        at most one save behind.

        If the save does not complete, `_dirty` goes back on. Clearing it up front
        is what makes the concurrent-mutation case work, but on a cancelled or
        failed save it stranded every pending change: the graph looked clean, the
        scheduler skipped it, and nothing was written. The DMN hits this on the
        ordinary path — `ACTIVITY_DETECTED` cancels an idle cycle exactly when its
        save is most likely to be in flight."""
        self._dirty = False
        try:
            text = await self._serialise_streamed()
            await asyncio.to_thread(self._write_atomic, text)
        except BaseException:  # including CancelledError — nothing reached the file
            self._dirty = True
            raise
        self._last_save = _utcnow()

    async def _serialise_streamed(self) -> str:
        parts: list[str] = ['{"schema_version": ', str(_SCHEMA_VERSION), ', "nodes": [']
        node_ids = list(self._graph.nodes)  # sync id snapshot, no await
        sep = ""
        for start in range(0, len(node_ids), self._SAVE_CHUNK):
            async with self._lock:
                for node_id in node_ids[start : start + self._SAVE_CHUNK]:
                    if node_id not in self._graph:
                        continue
                    parts.append(
                        sep + json.dumps(self._node_record(node_id, self._graph.nodes[node_id]))
                    )
                    sep = ", "
            await asyncio.sleep(0)

        parts.append('], "edges": [')
        edge_keys = list(self._graph.edges(keys=True))  # sync (u, v, relation) snapshot
        sep = ""
        for start in range(0, len(edge_keys), self._SAVE_CHUNK):
            async with self._lock:
                for u, v, key in edge_keys[start : start + self._SAVE_CHUNK]:
                    if not self._graph.has_edge(u, v, key):
                        continue
                    parts.append(
                        sep + json.dumps(self._edge_record(u, v, key, self._graph.edges[u, v, key]))
                    )
                    sep = ", "
            await asyncio.sleep(0)

        parts.append("]}")
        return "".join(parts)

    @staticmethod
    def _node_record(node_id: str, data: dict[str, Any]) -> dict[str, Any]:
        surfaced = _as_dt_opt(data.get("surfaced_at"))
        first_seen = _as_dt_opt(data.get("first_seen_at"))
        last_seen = _as_dt_opt(data.get("last_seen_at"))
        spec = data.get("spec")
        return {
            "id": node_id,
            "node_type": str(data["node_type"]),
            "label": str(data["label"]),
            "created_at": _as_dt(data["created_at"]).isoformat(),
            "last_accessed": _as_dt(data["last_accessed"]).isoformat(),
            "access_count": int(data["access_count"]),
            "relevance_score": float(data["relevance_score"]),
            "priority": int(data["priority"]),
            "surfaced_at": surfaced.isoformat() if surfaced is not None else None,
            "ram_mb": float(data.get("ram_mb", 0.0)),
            "cpu_percent": float(data.get("cpu_percent", 0.0)),
            "first_seen_at": first_seen.isoformat() if first_seen is not None else None,
            "last_seen_at": last_seen.isoformat() if last_seen is not None else None,
            "spec": spec.to_record() if isinstance(spec, LabelSpec) else None,
            "activity": round(float(data.get("activity", 0.0)), 6),
        }

    @staticmethod
    def _edge_record(u: str, v: str, key: Any, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": u,
            "target": v,
            "relation": str(key),
            "weight": float(data.get("weight", 0.0)),
            "created_at": _as_dt(data.get("created_at", _utcnow())).isoformat(),
        }

    def _write_atomic(self, text: str) -> None:
        # A unique temp name per call: `save()` runs the write in a worker thread
        # *outside* the lock, so two saves can overlap (e.g. a scheduler tick and
        # shutdown). A shared `<name>.tmp` would let one call's os.replace consume
        # the other's temp file — each write must own its temp.
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=self._path.name + ".", suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                # `fdopen` takes ownership of fd, but only once it succeeds — if
                # it raises, the descriptor is ours and leaks. On a daemon that
                # saves every 5 minutes for months, leaked fds end as EMFILE.
                try:
                    fh = os.fdopen(fd, "w", encoding="utf-8")
                except BaseException:
                    os.close(fd)
                    raise
                with fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise GraphMemoryError(f"atomic save of {self._path} failed: {exc}") from exc
        self._fsync_dir(parent)

    # ------------------------------------------------------------ lock-free workers
    def _add_node_unsafe(
        self, node_id: str, node_type: NodeType, attributes: dict[str, Any]
    ) -> Node:
        now = _utcnow()
        node = Node(
            id=node_id,
            node_type=NodeType(node_type),
            label=str(attributes.get("label", node_id)),
            created_at=_as_dt(attributes.get("created_at", now)),
            last_accessed=_as_dt(attributes.get("last_accessed", now)),
            access_count=int(attributes.get("access_count", 0)),
            relevance_score=float(attributes.get("relevance_score", 0.0)),
            priority=int(attributes.get("priority", 0)),
            surfaced_at=_as_dt_opt(attributes.get("surfaced_at")),
            ram_mb=float(attributes.get("ram_mb", 0.0)),
            cpu_percent=float(attributes.get("cpu_percent", 0.0)),
            first_seen_at=_as_dt_opt(attributes.get("first_seen_at")),
            last_seen_at=_as_dt_opt(attributes.get("last_seen_at")),
            # creation is one sighting (V-2); a caller seeding `access_count`
            # gets the same estimate a v5 file load does
            activity=float(attributes.get("activity", int(attributes.get("access_count", 0)) + 1)),
            spec=_as_spec(attributes.get("spec")),
        )
        self._graph.add_node(node_id, **self._node_to_attrs(node))
        self._dirty = True
        return node

    # ------------------------------------------------------------ B18 · facts
    def _lookup(self, ref: str) -> tuple[str, LabelSpec | None] | None:
        """`labels.Lookup` over the live graph (sync read, no lock needed for
        a single dict get — same as `get_node`)."""
        if ref not in self._graph:
            return None
        data = self._graph.nodes[ref]
        spec = data.get("spec")
        return str(data.get("label", "")), spec if isinstance(spec, LabelSpec) else None

    def _upsert_fact_unsafe(self, spec: LabelSpec) -> tuple[Node, bool]:
        node_id = fact_id(spec)
        if node_id in self._graph:
            data = self._graph.nodes[node_id]
            data["spec"] = spec  # the latest value (pressure, confidence) wins
            self._touch_unsafe(data)
            data["label"] = render(spec, self._ref_name)
            self._dirty = True
            return self._node_from_attrs(node_id, data), False
        node = self._add_node_unsafe(
            node_id,
            _KIND_NODE_TYPE[spec.kind],
            {"label": render(spec, self._ref_name), "spec": spec},
        )
        for ref in dict.fromkeys(spec.refs):
            if ref in self._graph and ref != node_id:
                self._add_edge_unsafe(node_id, ref, RelationType.RELATED_TO, 0.0)
        return node, True

    def _refresh_facts_unsafe(
        self, remap: Callable[[str], str] | None = None
    ) -> list[tuple[str, str]]:
        """Re-establish the invariant `node id == fact_id(spec)` after refs moved
        (`remap`) or on migrated nodes. A fact whose new id is free is renamed in
        place; one whose new id is taken is returned as `(survivor, victim)` for
        the caller to merge (one lock cycle each). A fact left malformed — a
        relational thought whose two refs became one app — is deleted."""
        pending: list[tuple[str, str]] = []
        for node_id in list(self._graph.nodes):
            if node_id not in self._graph:
                continue
            data = self._graph.nodes[node_id]
            spec = data.get("spec")
            if not isinstance(spec, LabelSpec):
                continue
            if remap is not None:
                refs = tuple(dict.fromkeys(remap(r) for r in spec.refs))
                if refs != spec.refs:
                    spec = replace(spec, refs=refs)
                    data["spec"] = spec
                    self._dirty = True
            if not is_well_formed(spec):
                self._graph.remove_node(node_id)
                self._dirty = True
                continue
            want = fact_id(spec)
            if want == node_id:
                continue
            if want in self._graph:
                pending.append((want, node_id))
            else:
                nx.relabel_nodes(self._graph, {node_id: want}, copy=False)
            self._dirty = True
        return pending

    def _rerender_labels_unsafe(self) -> None:
        """Refresh every fact's `label` cache from its spec and current names."""
        for _node_id, data in self._graph.nodes(data=True):
            spec = data.get("spec")
            if isinstance(spec, LabelSpec):
                data["label"] = render(spec, self._ref_name)

    _LEGACY_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(r"[\w.\-]+")

    def _migrate_v4_facts_unsafe(self) -> tuple[int, int]:
        """Schema v4 -> v5 (B18). Parse each pre-B18 generated label back into a
        spec, drop the thoughts that quoted other labels (issue 4), re-derive ids
        from fingerprints, merge what collides, re-render. Refs are still raw
        here (`app:brave-browser`); `canonicalise_app_nodes` heals them right
        after load. Returns `(parsed, dropped)`."""
        by_name: dict[str, str] = {}
        for node_id, data in self._graph.nodes(data=True):
            if node_id.startswith(("app:", "webapp:")):
                bare = node_id.split(":", 1)[1]
                label = str(data.get("label", ""))
                for key in (bare, label, leaf_name(node_id, label)):
                    by_name.setdefault(key.casefold(), node_id)
                    by_name.setdefault(re.sub(r"[\s_]+", "-", key.casefold()), node_id)

        def ref_for_name(name: str) -> str | None:
            key = name.casefold()
            hit = by_name.get(key) or by_name.get(re.sub(r"[\s_]+", "-", key))
            if hit is not None:
                return hit
            # an app name the graph no longer holds (pre-B17) — resolved later
            return f"app:{name}" if self._LEGACY_NAME_RE.fullmatch(name) else None

        parsed = dropped = 0
        for node_id in list(self._graph.nodes):
            data = self._graph.nodes[node_id]
            if data.get("spec") is not None or not node_id.startswith(_FACT_PREFIXES):
                continue
            spec = parse_legacy(node_id, str(data.get("label", "")), ref_for_name)
            if spec == DROP:
                self._graph.remove_node(node_id)
                dropped += 1
            elif isinstance(spec, LabelSpec):
                data["spec"] = spec
                parsed += 1
        for survivor, victim in self._refresh_facts_unsafe():
            self._merge_nodes_unsafe(survivor, victim)
        self._rerender_labels_unsafe()
        return parsed, dropped

    def _add_edge_unsafe(
        self, source_id: str, target_id: str, relation: RelationType, weight: float
    ) -> Edge:
        rel = RelationType(relation)
        # Upsert, never overwrite (T7). networkx `add_edge` on an existing
        # `(u, v, key)` merges the kwargs into the live edge dict — so a plain
        # re-add of an edge a pattern re-asserts every fire (or the APP_SWITCH
        # path re-classifies after a restart) would reset its accumulated
        # Hebbian `weight` to 0.0 and bump `created_at`. Re-adding an existing
        # edge is therefore a no-op that returns it unchanged; only a genuinely
        # new edge is written.
        if self._graph.has_edge(source_id, target_id, rel):
            return self._edge_from_attrs(
                source_id, target_id, rel, self._graph.edges[source_id, target_id, rel]
            )
        # V-5 · a routing hub is materialised the moment something routes to it.
        # `prune_dead_hubs` reaps the ones nothing reaches, so a hub may legally
        # be absent when its first app finally shows up; and networkx would
        # otherwise auto-create a bare, attribute-less node that every later
        # read (`_node_from_attrs`) raises KeyError on.
        for endpoint in (source_id, target_id):
            if endpoint in DOMAIN_HUB_IDS and endpoint not in self._graph:
                self._seed_hub_unsafe(endpoint)
        edge = Edge(
            source_id=source_id,
            target_id=target_id,
            relation=rel,
            weight=float(weight),
            created_at=_utcnow(),
        )
        self._graph.add_edge(
            source_id,
            target_id,
            key=edge.relation,
            weight=edge.weight,
            created_at=edge.created_at,
        )
        self._dirty = True
        return edge

    def _related_between_unsafe(self, a: str, b: str) -> bool:
        rel = RelationType.RELATED_TO
        return bool(self._graph.has_edge(a, b, rel) or self._graph.has_edge(b, a, rel))

    def _association_step_unsafe(self, a: str, b: str, rate: float) -> str | None:
        """One Hebbian step on the association edge between two activity nodes
        (T7 / V-1): the `RELATED_TO` edge in either direction is stepped with
        `_hebbian_step` (``"bumped"``), or created as `a -> b` at
        `_hebbian_step(0, rate)` (``"created"``). A pair already linked by
        `PART_OF` (a web-app and its own browser) is left alone — ``None``.
        Both nodes must already exist (caller's job)."""
        part_of = RelationType.PART_OF
        if self._graph.has_edge(a, b, part_of) or self._graph.has_edge(b, a, part_of):
            return None
        rel = RelationType.RELATED_TO
        for u, v in ((a, b), (b, a)):
            if self._graph.has_edge(u, v, rel):
                data = self._graph.edges[u, v, rel]
                data["weight"] = _hebbian_step(float(data.get("weight", 0.0)), rate)
                self._dirty = True
                return "bumped"
        self._graph.add_edge(a, b, key=rel, weight=_hebbian_step(0.0, rate), created_at=_utcnow())
        self._dirty = True
        return "created"

    # `first_seen_at` joins the protected set: like `created_at`, it is write-once
    # — the census refreshes `ram_mb` / `cpu_percent` / `last_seen_at` on every
    # sighting but must never move the first-sighting timestamp (B13-B3, D-19).
    _UPSERT_PROTECTED: ClassVar[frozenset[str]] = frozenset(
        {
            "created_at",
            "relevance_score",
            "access_count",
            "node_type",
            "last_accessed",
            "first_seen_at",
            "activity",
        }
    )
    _UPSERT_DT_OPT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"surfaced_at", "first_seen_at", "last_seen_at"}
    )

    def _upsert_node_unsafe(
        self, node_id: str, node_type: NodeType, attributes: dict[str, Any]
    ) -> Node:
        if node_id not in self._graph:
            return self._add_node_unsafe(node_id, node_type, attributes)
        data = self._graph.nodes[node_id]
        for key, value in attributes.items():
            if key not in self._UPSERT_PROTECTED:
                data[key] = _as_dt_opt(value) if key in self._UPSERT_DT_OPT_KEYS else value
        # A first census sighting still sets `first_seen_at` once, even though the
        # key is protected against later overwrites.
        if data.get("first_seen_at") is None and attributes.get("first_seen_at") is not None:
            data["first_seen_at"] = _as_dt_opt(attributes["first_seen_at"])
        self._touch_unsafe(data)
        self._dirty = True
        return self._node_from_attrs(node_id, data)

    @staticmethod
    def _touch_unsafe(data: dict[str, Any]) -> None:
        """One access: the decaying `activity` counter is aged to now and gains
        1 (V-2), the lifetime `access_count` gains 1, `last_accessed` moves."""
        now = _utcnow()
        data["activity"] = _activity_at(data, now) + 1.0
        data["access_count"] = int(data.get("access_count", 0)) + 1
        data["last_accessed"] = now

    def _reinforce_edge_unsafe(
        self, node_a: str, node_b: str, delta: float, *, relation: RelationType | None = None
    ) -> int:
        bumped = 0
        for u, v in ((node_a, node_b), (node_b, node_a)):
            if not self._graph.has_edge(u, v):
                continue
            for key in list(self._graph[u][v]):
                if relation is not None and RelationType(key) is not relation:
                    continue
                data = self._graph[u][v][key]
                data["weight"] = float(data.get("weight", 0.0)) + delta
                bumped += 1
        if bumped:
            self._dirty = True
        return bumped

    def _update_node_unsafe(self, node_id: str, attributes: dict[str, Any]) -> None:
        if node_id not in self._graph:
            raise GraphMemoryError(f"update_node: no such node {node_id!r}")
        self._graph.nodes[node_id].update(attributes)
        self._dirty = True

    def _delete_node_unsafe(self, node_id: str) -> None:
        if node_id in self._graph:
            self._graph.remove_node(node_id)
            self._dirty = True

    def _prune_unsafe(self, older_than: timedelta, min_importance: float) -> int:
        now = _utcnow()
        victims: list[str] = []
        for node_id, data in self._graph.nodes(data=True):
            if node_id in HUB_NODE_IDS:
                continue  # the routing skeleton is never pruned (D-5)
            score = float(data.get("relevance_score", 0.0))
            last = _as_dt(data.get("last_accessed", now))
            if score < min_importance and (now - last) > older_than:
                victims.append(node_id)
        for node_id in victims:
            self._graph.remove_node(node_id)
        if victims:
            self._dirty = True
        return len(victims)

    # ------------------------------------------------------------ B6 · L6 workers
    _STALE_SCORE_EPS: ClassVar[float] = 1e-9
    _THOUGHT_TYPES: ClassVar[frozenset[NodeType]] = frozenset(
        {NodeType.INSIGHT, NodeType.IDLE_THOUGHT}
    )

    def _find_duplicate_pairs_unsafe(self) -> list[tuple[str, str]]:
        """Every `(survivor, victim)` in one pass over the graph.

        Two non-hub nodes are duplicates when they share `node_type` and
        case-folded `label`; the survivor is the older `created_at` (tiebreak:
        id). A run of three or more nodes on one key yields one pair per victim
        against the same survivor, so a chain collapses in a single sweep.
        """
        best: dict[tuple[str, str], tuple[datetime, str]] = {}
        members: dict[tuple[str, str], list[str]] = {}
        for node_id, data in self._graph.nodes(data=True):
            if node_id in HUB_NODE_IDS or data.get("spec") is not None:
                continue  # B18: a fact is unique by id already — never merge on text
            key = (str(data.get("node_type", "")), str(data.get("label", "")).strip().casefold())
            created = _as_dt(data.get("created_at", _utcnow()))
            members.setdefault(key, []).append(node_id)
            incumbent = best.get(key)
            if incumbent is None or (created, node_id) < incumbent:
                best[key] = (created, node_id)

        pairs: list[tuple[str, str]] = []
        for key, group in members.items():
            if len(group) < 2:
                continue
            survivor = best[key][1]
            pairs.extend((survivor, victim) for victim in group if victim != survivor)
        return pairs

    def _merge_nodes_unsafe(self, survivor_id: str, victim_id: str) -> bool:
        """Fold `victim` into `survivor` (D-13 merge math), then delete `victim`.
        Returns whether the merge happened: the caller works from a snapshot, so
        either end may already be gone, and neither may be a routing hub."""
        if survivor_id in HUB_NODE_IDS or victim_id in HUB_NODE_IDS:
            return False
        if survivor_id not in self._graph or victim_id not in self._graph:
            return False
        if survivor_id == victim_id:
            return False
        s = self._graph.nodes[survivor_id]
        v = self._graph.nodes[victim_id]

        s["created_at"] = min(_as_dt(s["created_at"]), _as_dt(v["created_at"]))
        # V-2 · both counters aged to the later of the two touches, then summed,
        # so the merged node carries the combined decayed activity as of its
        # (new) `last_accessed`
        latest = max(_as_dt(s["last_accessed"]), _as_dt(v["last_accessed"]))
        s["activity"] = _activity_at(s, latest) + _activity_at(v, latest)
        s["last_accessed"] = latest
        s["access_count"] = int(s.get("access_count", 0)) + int(v.get("access_count", 0))
        s["relevance_score"] = round(
            (float(s.get("relevance_score", 0.0)) + float(v.get("relevance_score", 0.0))) / 2.0, 3
        )
        s["priority"] = max(int(s.get("priority", 0)), int(v.get("priority", 0)))
        if s.get("surfaced_at") is None and v.get("surfaced_at") is not None:
            s["surfaced_at"] = _as_dt_opt(v.get("surfaced_at"))

        # B13-B3 · resource attributes: keep the earliest first-sighting and the
        # most recent census — and the ram/cpu numbers from whichever side owns
        # that most recent sighting.
        s_first, v_first = _as_dt_opt(s.get("first_seen_at")), _as_dt_opt(v.get("first_seen_at"))
        if v_first is not None and (s_first is None or v_first < s_first):
            s["first_seen_at"] = v_first
        s_last, v_last = _as_dt_opt(s.get("last_seen_at")), _as_dt_opt(v.get("last_seen_at"))
        if v_last is not None and (s_last is None or v_last > s_last):
            s["last_seen_at"] = v_last
            s["ram_mb"] = float(v.get("ram_mb", 0.0))
            s["cpu_percent"] = float(v.get("cpu_percent", 0.0))

        # Rewire every edge on the victim to the survivor, dropping any edge
        # between the two (it would become a self-loop) and folding a weight into
        # an edge the survivor already carries for that (neighbour, relation).
        for u, _v, key, data in list(self._graph.in_edges(victim_id, keys=True, data=True)):
            if u == survivor_id:
                continue
            self._rewire_edge_unsafe(u, survivor_id, key, float(data.get("weight", 0.0)))
        for _u, w, key, data in list(self._graph.out_edges(victim_id, keys=True, data=True)):
            if w == survivor_id:
                continue
            self._rewire_edge_unsafe(survivor_id, w, key, float(data.get("weight", 0.0)))

        self._graph.remove_node(victim_id)
        self._dirty = True
        return True

    def _rewire_edge_unsafe(self, source: str, target: str, key: Any, weight: float) -> None:
        if self._graph.has_edge(source, target, key):
            data = self._graph.edges[source, target, key]
            data["weight"] = float(data.get("weight", 0.0)) + weight
            return
        self._graph.add_edge(source, target, key=key, weight=weight, created_at=_utcnow())

    def _is_orphan_unsafe(self, node_id: str) -> bool:
        """A non-hub node that still exists and still has total degree 0."""
        if node_id in HUB_NODE_IDS or node_id not in self._graph:
            return False
        return int(self._graph.degree(node_id)) == 0

    def _orphan_ids_unsafe(self) -> list[str]:
        """Every orphan, in one pass. `degree` over the whole graph is a single
        O(N) walk; asking for one orphan at a time made it O(orphans x N)."""
        return [
            str(node_id)
            for node_id, degree in self._graph.degree()
            if degree == 0 and node_id not in HUB_NODE_IDS
        ]

    def _prune_stale_unsafe(self, ttl: timedelta) -> int:
        now = _utcnow()
        victims: list[str] = []
        for node_id, data in self._graph.nodes(data=True):
            if node_id in HUB_NODE_IDS:
                continue
            created = _as_dt(data.get("created_at", now))
            last = _as_dt(data.get("last_accessed", now))
            age_since_touch = now - last
            if (now - created) <= ttl and age_since_touch <= ttl:
                continue  # too fresh to judge — scoring may not have run yet
            node_type = NodeType(data["node_type"])
            if node_type in self._THOUGHT_TYPES:
                if (now - created) > ttl:
                    victims.append(node_id)
                continue
            score = float(data.get("relevance_score", 0.0))
            if score <= self._STALE_SCORE_EPS or age_since_touch > ttl:
                victims.append(node_id)
        for node_id in victims:
            self._graph.remove_node(node_id)
        if victims:
            self._dirty = True
        return len(victims)

    def _score_scale_unsafe(
        self, node_ids: list[str], now: datetime, profiles: dict[str, tuple[float, float]]
    ) -> tuple[float, float]:
        """V-2 · pass one of `recalculate_importance`: the largest (activity,
        association strength) among `node_ids`' non-hub nodes. Each node's
        `(strength, bridge)` is stored in `profiles` so pass two does no edge
        walk at all."""
        top_activity = top_strength = 0.0
        for node_id in node_ids:
            if node_id not in self._graph or node_id in HUB_NODE_IDS:
                continue
            profile = profiles[node_id] = self._edge_profile_unsafe(node_id)
            top_activity = max(top_activity, _activity_at(self._graph.nodes[node_id], now))
            top_strength = max(top_strength, profile[0])
        return top_activity, top_strength

    def _recalculate_chunk_unsafe(
        self,
        node_ids: list[str],
        now: datetime,
        scale: tuple[float, float],
        profiles: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        """Score one chunk: `6·activity + 2·strength + 2·bridge` (V-2; the terms
        and why are on the module constants). `scale` is the graph's
        `(log1p(top activity), log1p(top strength))` from pass one; a node that
        grew since then is clamped to 1, not over-scored. `profiles` are pass
        one's edge walks — a node added between the passes is walked here."""
        activity_scale, strength_scale = scale
        known = profiles or {}
        changed = False
        for node_id in node_ids:
            if node_id not in self._graph:
                continue  # removed since the id snapshot — skip, catch it next pass
            data = self._graph.nodes[node_id]
            strength, bridge = known.get(node_id) or self._edge_profile_unsafe(node_id)
            activity = (
                math.log1p(_activity_at(data, now)) / activity_scale if activity_scale else 0.0
            )
            tie = math.log1p(strength) / strength_scale if strength_scale else 0.0
            raw = (
                _W_ACTIVITY * min(1.0, activity) + _W_STRENGTH * min(1.0, tie) + _W_BRIDGE * bridge
            )
            data["relevance_score"] = round(min(10.0, max(0.0, raw)), 3)
            changed = True
        if changed:
            self._dirty = True

    def _edge_profile_unsafe(self, node_id: str) -> tuple[float, float]:
        """V-2 · `(strength, bridge)` from ONE walk of the node's raw adjacency.

        - **strength** — how strongly the node is tied in: the sum of learned
          Hebbian weights on its `RELATED_TO` edges, plus
          `_STRUCTURAL_EDGE_STRENGTH` per other edge. Edges to a hub count
          nothing (`YOU` is the orphan placeholder; domains are the bridge
          term), nor does a generated node's provenance edge, in either
          direction — the system's own notes neither earn nor lend relevance by
          existing. Plain degree (the old term) scored a probe's one
          bookkeeping edge like a real app's one real edge.
        - **bridge** (D-10, reworked) — distinct `domain:*` hubs reached directly
          or through an association of weight >= `_BRIDGE_MIN_WEIGHT`: one
          domain 0, two 0.5, three or more 1. Before V-2 only direct domain
          edges counted, and an app has one, so the term measured "is it in
          app_map" rather than "does it link different parts of your work".

        Reads `_succ` / `_pred` directly: the networkx edge views cost a view
        object per call and an enum conversion per edge, which was ~70 % of a
        10k-node recalc. Keys are stored as `RelationType` members already."""
        if node_id in HUB_NODE_IDS:
            return 0.0, 0.0
        derived = node_id.startswith(_DERIVED_PREFIXES)
        strength = 0.0
        domains: set[str] = set()
        associates: list[str] = []
        for adjacency in (self._graph._succ[node_id], self._graph._pred[node_id]):
            for other, keyed in adjacency.items():
                if other in HUB_NODE_IDS:
                    if other in DOMAIN_HUB_IDS:
                        domains.add(other)
                    continue
                provenance = derived or other.startswith(_DERIVED_PREFIXES)
                for key, data in keyed.items():
                    weight = float(data.get("weight", 0.0))
                    if key == RelationType.RELATED_TO and weight > 0.0:
                        strength += weight
                        if weight >= _BRIDGE_MIN_WEIGHT:
                            associates.append(other)
                    elif not provenance:
                        strength += _STRUCTURAL_EDGE_STRENGTH
        for other in associates:
            domains |= self._domains_of_unsafe(other)
        return strength, min(1.0, max(0.0, (len(domains) - 1) / 2.0))

    def _strength_unsafe(self, node_id: str) -> float:
        return self._edge_profile_unsafe(node_id)[0]

    def _bridge_value_unsafe(self, node_id: str) -> float:
        return self._edge_profile_unsafe(node_id)[1]

    def _domains_of_unsafe(self, node_id: str) -> set[str]:
        graph = self._graph
        return set(graph._succ[node_id].keys() | graph._pred[node_id].keys()) & DOMAIN_HUB_IDS

    def _seed_hubs_unsafe(self) -> None:
        self._add_node_unsafe("YOU", NodeType.CONCEPT, {"label": "YOU"})
        for slug in DOMAIN_SLUGS:
            self._seed_hub_unsafe(f"domain:{slug}")

    def _seed_hub_unsafe(self, hub_id: str) -> None:
        """One `domain:` hub, with the label every seeding path has always given
        it. Shared by the fresh-graph seed and V-5's materialise-on-demand, so a
        reaped hub comes back identical to the one it replaces."""
        slug = hub_id.removeprefix("domain:")
        self._add_node_unsafe(hub_id, NodeType.CONCEPT, {"label": slug.replace("_", " ").title()})

    # ---------------------------------------------------------------- (de)serialise
    @staticmethod
    def _validate_schema_version(payload: dict[str, Any]) -> int:
        """Read and check `schema_version` *before* touching any node data (B9/BL-3).

        Until B9 this value was written on every save and never read back, so a
        file from a future build was parsed optimistically: unknown-but-required
        keys raised `KeyError` deep inside the node loop, and a renamed field was
        worse — it loaded "successfully" with data silently dropped. Both are
        checked here instead, where the error can say what actually happened.

        A file with no `schema_version` at all is v1: the key was introduced with
        the field, so its absence is meaningful rather than missing.
        """
        raw = payload.get("schema_version", 1)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise GraphMemoryError(
                f"graph schema_version must be an integer, got {raw!r} — "
                "the file is not a NeuroPACA graph, or is corrupt"
            )
        if raw > _SCHEMA_VERSION:
            raise GraphMemoryError(
                f"graph schema v{raw} was written by a newer NeuroPACA than this one "
                f"(reads up to v{_SCHEMA_VERSION}). Refusing to load it rather than "
                "drop the fields this build does not know about — upgrade, or point "
                "`graph_db_path` elsewhere."
            )
        if raw < _MIN_READABLE_SCHEMA_VERSION:
            raise GraphMemoryError(
                f"graph schema v{raw} is older than the oldest readable version "
                f"(v{_MIN_READABLE_SCHEMA_VERSION}); no migration path exists"
            )
        return int(raw)

    def _deserialise(self, payload: dict[str, Any]) -> Any:
        self._validate_schema_version(payload)
        graph = nx.MultiDiGraph()
        for raw in payload.get("nodes", []):
            node = Node(
                id=raw["id"],
                node_type=NodeType(raw["node_type"]),
                label=raw["label"],
                created_at=_as_dt(raw["created_at"]),
                last_accessed=_as_dt(raw["last_accessed"]),
                access_count=int(raw["access_count"]),
                relevance_score=float(raw["relevance_score"]),
                priority=int(raw["priority"]),
                surfaced_at=_as_dt_opt(raw.get("surfaced_at")),  # absent in a v1 file
                ram_mb=float(raw.get("ram_mb", 0.0)),  # absent in a v1/v2 file
                cpu_percent=float(raw.get("cpu_percent", 0.0)),
                first_seen_at=_as_dt_opt(raw.get("first_seen_at")),
                last_seen_at=_as_dt_opt(raw.get("last_seen_at")),
                spec=LabelSpec.from_record(raw.get("spec")),  # absent before v5
                # absent before v6: the lifetime tally plus the creation sighting
                activity=float(raw.get("activity", int(raw["access_count"]) + 1)),
            )
            graph.add_node(node.id, **self._node_to_attrs(node))
        for raw in payload.get("edges", []):
            relation = RelationType(raw["relation"])
            graph.add_edge(
                raw["source"],
                raw["target"],
                key=relation,
                weight=float(raw.get("weight", 0.0)),
                created_at=_as_dt(raw.get("created_at", _utcnow().isoformat())),
            )
        return graph

    @staticmethod
    def _node_to_attrs(node: Node) -> dict[str, Any]:
        return {
            "node_type": node.node_type,
            "label": node.label,
            "created_at": node.created_at,
            "last_accessed": node.last_accessed,
            "access_count": node.access_count,
            "relevance_score": node.relevance_score,
            "priority": node.priority,
            "surfaced_at": node.surfaced_at,
            "ram_mb": node.ram_mb,
            "cpu_percent": node.cpu_percent,
            "first_seen_at": node.first_seen_at,
            "last_seen_at": node.last_seen_at,
            "spec": node.spec,
            "activity": node.activity,
        }

    @staticmethod
    def _node_from_attrs(node_id: str, data: dict[str, Any]) -> Node:
        return Node(
            id=node_id,
            node_type=NodeType(data["node_type"]),
            label=str(data["label"]),
            created_at=_as_dt(data["created_at"]),
            last_accessed=_as_dt(data["last_accessed"]),
            access_count=int(data["access_count"]),
            relevance_score=float(data["relevance_score"]),
            priority=int(data["priority"]),
            surfaced_at=_as_dt_opt(data.get("surfaced_at")),
            ram_mb=float(data.get("ram_mb", 0.0)),
            cpu_percent=float(data.get("cpu_percent", 0.0)),
            first_seen_at=_as_dt_opt(data.get("first_seen_at")),
            last_seen_at=_as_dt_opt(data.get("last_seen_at")),
            spec=_as_spec(data.get("spec")),
            activity=float(data.get("activity", 0.0)),
        )

    @staticmethod
    def _edge_from_attrs(source_id: str, target_id: str, key: Any, data: dict[str, Any]) -> Edge:
        return Edge(
            source_id=source_id,
            target_id=target_id,
            relation=RelationType(key),
            weight=float(data.get("weight", 0.0)),
            created_at=_as_dt(data.get("created_at", _utcnow())),
        )

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # some filesystems do not support directory fsync


# gen-ref: a3242257
