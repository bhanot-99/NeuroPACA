"""L1 · `GraphMemory` — the single behavioural graph (Architecture.md §3.2, D-5/D-6).

Rules that shape this file:
- `networkx.MultiDiGraph`: a pair of nodes can carry several `RelationType`s at
  once, keyed by the relation (D-5).
- one `asyncio.Lock`, held for exactly one graph mutation. Public methods take
  the lock and call a lock-free `_*_unsafe` worker; compound work takes the lock
  once. `asyncio.Lock` is not reentrant — a public method calling another public
  method under the lock deadlocks forever (problems.md 1.10).
- the 11 routing hubs (`YOU` + 10 `domain:*`) are a protected set: `prune()`
  never removes them, and `find_related()` never traverses *through* them.
- `save()` is atomic: temp file -> fsync -> `os.replace`.
- `relevance_score` is a fixed-scale 0-10 composite with `bridge_value = 0.0`
  until the domain layer exists (B2/B3, D-6).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import networkx as nx

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.models import Edge, Node

_SCHEMA_VERSION = 1

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

    async def delete_node(self, node_id: str) -> None:
        async with self._lock:
            self._delete_node_unsafe(node_id)

    async def prune(self, older_than: timedelta, min_importance: float) -> int:
        async with self._lock:
            return self._prune_unsafe(older_than, min_importance)

    async def recalculate_importance(self) -> None:
        async with self._lock:
            self._recalculate_unsafe()

    async def consolidate(self) -> None:
        async with self._lock:
            # B1 has no duplicate-detection heuristic yet (that is B6); the lock
            # discipline and the entry point are what B1 establishes.
            return None

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

    # --------------------------------------------------------------- persistence
    async def load(self) -> None:
        async with self._lock:
            if self._path.exists():
                try:
                    payload = json.loads(self._path.read_text("utf-8"))
                except (OSError, ValueError) as exc:
                    raise GraphMemoryError(f"cannot load graph {self._path}: {exc}") from exc
                self._graph = self._deserialise(payload)
            if self._graph.number_of_nodes() == 0:
                self._seed_hubs_unsafe()
            self._dirty = False

    async def save(self) -> None:
        # Build the snapshot under the lock (fast, pure); do the blocking write
        # off the event loop (Architecture.md §14, rules.md §1).
        async with self._lock:
            text = json.dumps(self._serialise_unsafe(), indent=2, sort_keys=True)
        await asyncio.to_thread(self._write_atomic, text)
        self._dirty = False
        self._last_save = _utcnow()

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
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
                data[key] = value
        data["access_count"] = int(data.get("access_count", 0)) + 1
        data["last_accessed"] = _utcnow()
        self._dirty = True
        return self._node_from_attrs(node_id, data)

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

    def _recalculate_unsafe(self) -> None:
        now = _utcnow()
        for node_id, data in self._graph.nodes(data=True):
            frequency = min(1.0, int(data.get("access_count", 0)) / 100.0)
            age_days = max(
                0.0, (now - _as_dt(data.get("last_accessed", now))).total_seconds() / 86400.0
            )
            recency = 0.5 ** (age_days / 7.0)
            degree = int(self._graph.degree(node_id))
            connectivity = min(1.0, math.log1p(degree) / math.log1p(20))
            bridge_value = 0.0  # D-6 — no cross-domain signal until B2/B3
            raw = frequency * 3.0 + recency * 3.0 + connectivity * 2.0 + bridge_value * 2.0
            data["relevance_score"] = round(min(10.0, max(0.0, raw)), 3)
        self._dirty = True

    def _seed_hubs_unsafe(self) -> None:
        self._add_node_unsafe("YOU", NodeType.CONCEPT, {"label": "YOU"})
        for slug in DOMAIN_SLUGS:
            self._add_node_unsafe(
                f"domain:{slug}", NodeType.CONCEPT, {"label": slug.replace("_", " ").title()}
            )

    # ---------------------------------------------------------------- (de)serialise
    def _serialise_unsafe(self) -> dict[str, Any]:
        nodes = [
            {
                "id": node_id,
                "node_type": str(data["node_type"]),
                "label": data["label"],
                "created_at": _as_dt(data["created_at"]).isoformat(),
                "last_accessed": _as_dt(data["last_accessed"]).isoformat(),
                "access_count": int(data["access_count"]),
                "relevance_score": float(data["relevance_score"]),
                "priority": int(data["priority"]),
            }
            for node_id, data in self._graph.nodes(data=True)
        ]
        edges = [
            {
                "source": u,
                "target": v,
                "relation": str(key),
                "weight": float(data.get("weight", 0.0)),
                "created_at": _as_dt(data.get("created_at", _utcnow())).isoformat(),
            }
            for u, v, key, data in self._graph.edges(keys=True, data=True)
        ]
        return {"schema_version": _SCHEMA_VERSION, "nodes": nodes, "edges": edges}

    def _deserialise(self, payload: dict[str, Any]) -> Any:
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
