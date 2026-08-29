"""Deterministic 10 000-node graph fixture for the B1 performance harness.

Produces the exact on-disk form `GraphMemory.load()` consumes (Architecture.md
§3.2). Topology:

- 11 protected hubs: ``YOU`` + ``domain:{slug}`` for the 10 master domains
- exactly 10 000 ``leaf:NNNNN`` nodes
- ~25 000 directed edges; ~20 % are incident to a hub, to mimic the dense
  behavioural routing the real graph develops around ``YOU`` and the domains

Everything is driven by a single ``random.Random(_SEED)`` so the file — and every
number the perf test asserts against — is byte-for-byte reproducible.

The written file (``graph_10k.json``) is gitignored: at ~4 MB it is over the
repo's large-file limit, and it regenerates from this script in well under a
second.

    python -m tests.fixtures.generate_10k_graph        # writes graph_10k.json
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.graph_memory import HUB_NODE_IDS

_SEED = 20260829
_LEAF_COUNT = 10_000
_EDGE_COUNT = 25_000
_HUB_EDGE_FRACTION = 0.20

_BASE_TIME = datetime(2026, 8, 1, tzinfo=UTC)
_NODE_TYPES = tuple(NodeType)
_RELATIONS = tuple(RelationType)
_HUBS: tuple[str, ...] = tuple(sorted(HUB_NODE_IDS))

FIXTURE_PATH = Path(__file__).parent / "graph_10k.json"


def _leaf_id(index: int) -> str:
    return f"leaf:{index:05d}"


def build_payload(seed: int = _SEED) -> dict[str, Any]:
    """Return the serialised graph dict — 11 hubs + 10 000 leaves + ~25 000 edges."""
    rng = random.Random(seed)
    leaf_ids = [_leaf_id(i) for i in range(_LEAF_COUNT)]

    nodes: list[dict[str, Any]] = []
    for hub_id in _HUBS:
        nodes.append(_node_record(hub_id, hub_id.replace("domain:", "").replace("_", " "), rng))
    for leaf_id in leaf_ids:
        nodes.append(_node_record(leaf_id, leaf_id, rng))

    hub_edge_target = round(_EDGE_COUNT * _HUB_EDGE_FRACTION)
    seen: set[tuple[str, str, str]] = set()
    edges: list[dict[str, Any]] = []

    while len(edges) < _EDGE_COUNT:
        through_hub = len(edges) < hub_edge_target
        if through_hub:
            hub = rng.choice(_HUBS)
            leaf = rng.choice(leaf_ids)
            source, target = (hub, leaf) if rng.random() < 0.5 else (leaf, hub)
        else:
            source, target = rng.sample(leaf_ids, 2)
        relation = rng.choice(_RELATIONS)
        key = (source, target, str(relation))
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "source": source,
                "target": target,
                "relation": str(relation),
                "weight": round(rng.uniform(0.0, 1.0), 4),
                "created_at": (_BASE_TIME - timedelta(days=rng.randint(0, 200))).isoformat(),
            }
        )

    return {"schema_version": 1, "nodes": nodes, "edges": edges}


def _node_record(node_id: str, label: str, rng: random.Random) -> dict[str, Any]:
    last_accessed = _BASE_TIME - timedelta(days=rng.randint(0, 400), seconds=rng.randint(0, 86_400))
    created_at = last_accessed - timedelta(days=rng.randint(0, 45))
    return {
        "id": node_id,
        "node_type": str(rng.choice(_NODE_TYPES)),
        "label": label,
        "created_at": created_at.isoformat(),
        "last_accessed": last_accessed.isoformat(),
        "access_count": rng.randint(0, 400),
        "relevance_score": round(rng.uniform(0.0, 10.0), 3),
        "priority": 0,
    }


def write_fixture(path: Path = FIXTURE_PATH, *, seed: int = _SEED) -> dict[str, Any]:
    """Build the payload and write it to `path`. Returns the payload."""
    payload = build_payload(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def main() -> None:
    payload = write_fixture()
    print(f"wrote {FIXTURE_PATH} — {len(payload['nodes'])} nodes, {len(payload['edges'])} edges")


if __name__ == "__main__":
    main()
