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
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import networkx as nx

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.models import Edge, Node

# v2 (B5): node records gain an optional `surfaced_at`. Forward-compatible — a v1
# file loads unchanged (the key is simply absent -> None).
_SCHEMA_VERSION = 2

# The oldest on-disk version this build can still read. v1 differs from v2 only
# by the absent `surfaced_at` key, which `_deserialise` already tolerates, so no
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


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _as_dt_opt(value: Any) -> datetime | None:
    if value is None:
        return None
    return _as_dt(value)


class GraphMemory:
    """CRUD, traversal, scoring, consolidation, and atomic persistence for the graph."""

    _instance: ClassVar[GraphMemory | None] = None

    def __init__(self, persistence_path: str | Path) -> None:
        self._path = Path(persistence_path)
        self._graph: Any = nx.MultiDiGraph()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._last_save: datetime | None = None

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
        removed between chunks is simply picked up on the next pass."""
        now = _utcnow()
        node_ids = list(self._graph.nodes)  # sync snapshot of ids, no await
        for start in range(0, len(node_ids), self._RECALC_CHUNK):
            chunk = node_ids[start : start + self._RECALC_CHUNK]
            async with self._lock:
                self._recalculate_chunk_unsafe(chunk, now)
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
        self, limit: int, *, exclude_types: frozenset[NodeType] | None = None
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
                if node_id not in HUB_NODE_IDS and NodeType(data["node_type"]) not in excluded
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
        )
        self._graph.add_node(node_id, **self._node_to_attrs(node))
        self._dirty = True
        return node

    def _add_edge_unsafe(
        self, source_id: str, target_id: str, relation: RelationType, weight: float
    ) -> Edge:
        edge = Edge(
            source_id=source_id,
            target_id=target_id,
            relation=RelationType(relation),
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

    _UPSERT_PROTECTED: ClassVar[frozenset[str]] = frozenset(
        {"created_at", "relevance_score", "access_count", "node_type", "last_accessed"}
    )

    def _upsert_node_unsafe(
        self, node_id: str, node_type: NodeType, attributes: dict[str, Any]
    ) -> Node:
        if node_id not in self._graph:
            return self._add_node_unsafe(node_id, node_type, attributes)
        data = self._graph.nodes[node_id]
        for key, value in attributes.items():
            if key not in self._UPSERT_PROTECTED:
                data[key] = _as_dt_opt(value) if key == "surfaced_at" else value
        data["access_count"] = int(data.get("access_count", 0)) + 1
        data["last_accessed"] = _utcnow()
        self._dirty = True
        return self._node_from_attrs(node_id, data)

    def _reinforce_edge_unsafe(self, node_a: str, node_b: str, delta: float) -> int:
        bumped = 0
        for u, v in ((node_a, node_b), (node_b, node_a)):
            if not self._graph.has_edge(u, v):
                continue
            for key in list(self._graph[u][v]):
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
            if node_id in HUB_NODE_IDS:
                continue
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
        s["last_accessed"] = max(_as_dt(s["last_accessed"]), _as_dt(v["last_accessed"]))
        s["access_count"] = int(s.get("access_count", 0)) + int(v.get("access_count", 0))
        s["relevance_score"] = round(
            (float(s.get("relevance_score", 0.0)) + float(v.get("relevance_score", 0.0))) / 2.0, 3
        )
        s["priority"] = max(int(s.get("priority", 0)), int(v.get("priority", 0)))
        if s.get("surfaced_at") is None and v.get("surfaced_at") is not None:
            s["surfaced_at"] = _as_dt_opt(v.get("surfaced_at"))

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

    def _recalculate_chunk_unsafe(self, node_ids: list[str], now: datetime) -> None:
        changed = False
        for node_id in node_ids:
            if node_id not in self._graph:
                continue  # removed since the id snapshot — skip, catch it next pass
            data = self._graph.nodes[node_id]
            frequency = min(1.0, int(data.get("access_count", 0)) / 100.0)
            age_days = max(
                0.0, (now - _as_dt(data.get("last_accessed", now))).total_seconds() / 86400.0
            )
            recency = 0.5 ** (age_days / 7.0)
            degree = int(self._graph.degree(node_id))
            connectivity = min(1.0, math.log1p(degree) / math.log1p(20))
            bridge_value = self._bridge_value_unsafe(node_id)
            raw = frequency * 3.0 + recency * 3.0 + connectivity * 2.0 + bridge_value * 2.0
            data["relevance_score"] = round(min(10.0, max(0.0, raw)), 3)
            changed = True
        if changed:
            self._dirty = True

    def _bridge_value_unsafe(self, node_id: str) -> float:
        """0-1 cross-domain reach: a node wired to >= 2 `domain:*` hubs bridges
        the graph and earns the full bonus; one domain is half; none is zero
        (D-10 — the domain layer that made this non-trivial arrived in B2.5b).
        Hub nodes themselves are excluded — `YOU`/`domain:*` are structure."""
        if node_id in HUB_NODE_IDS:
            return 0.0
        neighbours = set(self._graph.successors(node_id)) | set(self._graph.predecessors(node_id))
        domains = neighbours & DOMAIN_HUB_IDS
        return min(1.0, len(domains) / 2.0)

    def _seed_hubs_unsafe(self) -> None:
        self._add_node_unsafe("YOU", NodeType.CONCEPT, {"label": "YOU"})
        for slug in DOMAIN_SLUGS:
            self._add_node_unsafe(
                f"domain:{slug}", NodeType.CONCEPT, {"label": slug.replace("_", " ").title()}
            )

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
