#!/usr/bin/env python3
"""B6 · Exit Criterion 3 — graph consolidation at scale (phases.md B6).

Runs against the real 16 GB Wayland/COSMIC box. Builds the deterministic 10 000-
node base graph, then injects **500** exact-duplicate nodes (same `node_type` +
`label` as a random real leaf, each newer so the original survives), scattered
across the graph with random edges to domain hubs and other real leaves. Then
triggers `GraphMemory.consolidate()` and verifies the D-13 merge contract.

Asserts:
  * `consolidate()` reports exactly 500 merges and `node_count` drops by exactly 500;
  * every duplicate is gone, every original survives;
  * each survivor's `access_count` == original + duplicate (summed);
  * each survivor's `relevance_score` == round((original + duplicate) / 2, 3);
  * every edge the duplicate carried is now on the survivor, same direction and
    relation (perfectly rewired) — and no `dup:` id is left as an edge endpoint.

Also prints the wall-clock time for the 500-merge consolidate over the ~10.5k-node
graph (Exit Criterion 3's "scale" number for `memory.md`).

    uv run --extra spike python scripts/validate_b6_consolidate.py

Exit 0 = all assertions pass. Exit 1 = any failure.
"""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.fixtures.generate_10k_graph import write_fixture

from neuropaca.core.enums import RelationType
from neuropaca.core.graph_memory import DOMAIN_HUB_IDS, GraphMemory

_DUPES = 500
_SEED = 20260906
_RELATIONS = tuple(RelationType)


class _Pair:
    __slots__ = ("dup_edges", "dup_id", "exp_access", "exp_score", "orig_id")

    def __init__(self, orig_id: str, dup_id: str, exp_access: int, exp_score: float) -> None:
        self.orig_id = orig_id
        self.dup_id = dup_id
        self.exp_access = exp_access
        self.exp_score = exp_score
        self.dup_edges: list[tuple[str, str, RelationType]] = []  # (direction, neighbour, relation)


async def _inject(gm: GraphMemory, rng: random.Random) -> list[_Pair]:
    leaf_ids = [nid for nid in gm.node_ids if nid.startswith("leaf:")]
    sampled = rng.sample(leaf_ids, _DUPES)
    sampled_set = set(sampled)
    stable_leaves = [nid for nid in leaf_ids if nid not in sampled_set]
    domains = sorted(DOMAIN_HUB_IDS)
    newer = datetime.now(UTC)

    pairs: list[_Pair] = []
    for i, orig_id in enumerate(sampled):
        orig = gm.get_node(orig_id)
        assert orig is not None
        dup_id = f"dup:{i:05d}"
        dup_access = rng.randint(0, 60)
        dup_score = round(rng.uniform(0.0, 10.0), 3)
        await gm.add_node(
            dup_id,
            orig.node_type,
            {
                "label": orig.label,
                "access_count": dup_access,
                "relevance_score": dup_score,
                "created_at": newer,
                "last_accessed": newer,
            },
        )
        pair = _Pair(
            orig_id,
            dup_id,
            exp_access=orig.access_count + dup_access,
            exp_score=round((orig.relevance_score + dup_score) / 2.0, 3),
        )

        # one guaranteed domain edge (scattered across domains) + a few random ones
        targets = [rng.choice(domains)]
        targets += [rng.choice(stable_leaves) for _ in range(rng.randint(1, 3))]
        for tgt in targets:
            rel = rng.choice(_RELATIONS)
            if rng.random() < 0.75:
                await gm.add_edge(dup_id, tgt, rel)
                pair.dup_edges.append(("out", tgt, rel))
            else:
                await gm.add_edge(tgt, dup_id, rel)
                pair.dup_edges.append(("in", tgt, rel))
        pairs.append(pair)
    return pairs


def _verify(gm: GraphMemory, pairs: list[_Pair], merged: int, start_count: int) -> list[str]:
    fails: list[str] = []
    if merged != _DUPES:
        fails.append(f"consolidate() reported {merged} merges, expected {_DUPES}")
    if gm.node_count != start_count - _DUPES:
        fails.append(
            f"node_count is {gm.node_count}, expected {start_count - _DUPES} "
            f"(start {start_count} - {_DUPES})"
        )

    bad_math = bad_rewire = missing_orig = live_dup = 0
    for p in pairs:
        if gm.get_node(p.dup_id) is not None:
            live_dup += 1
            continue
        survivor = gm.get_node(p.orig_id)
        if survivor is None:
            missing_orig += 1
            continue
        if survivor.access_count != p.exp_access:
            bad_math += 1
        elif round(survivor.relevance_score, 3) != p.exp_score:
            bad_math += 1

        present = {(e.source_id, e.target_id, e.relation) for e in gm.get_edges(p.orig_id)}
        for direction, neighbour, rel in p.dup_edges:
            edge = (
                (p.orig_id, neighbour, rel) if direction == "out" else (neighbour, p.orig_id, rel)
            )
            if edge not in present:
                bad_rewire += 1
                break

    if live_dup:
        fails.append(f"{live_dup} duplicate nodes were not removed")
    if missing_orig:
        fails.append(f"{missing_orig} original (survivor) nodes vanished")
    if bad_math:
        fails.append(f"{bad_math} survivors have wrong summed access_count / averaged score")
    if bad_rewire:
        fails.append(f"{bad_rewire} survivors are missing a rewired edge from their duplicate")

    dangling = [
        f"{u}->{v}"
        for u, v, _k in gm._graph.edges(keys=True)
        if u.startswith("dup:") or v.startswith("dup:")
    ]
    if dangling:
        fails.append(
            f"{len(dangling)} edges still reference a removed dup: node (e.g. {dangling[0]})"
        )
    return fails


async def _main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-b6-consolidate-"))
    graph_path = workdir / "graph.json"
    write_fixture(graph_path)

    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(graph_path))
    await gm.load()
    base_count = gm.node_count

    rng = random.Random(_SEED)
    pairs = await _inject(gm, rng)
    start_count = gm.node_count
    print(f"base graph {base_count} nodes -> {start_count} after injecting {_DUPES} duplicates")

    t0 = time.perf_counter()
    merged = await gm.consolidate()
    dt = time.perf_counter() - t0
    print(
        f"consolidate(): {merged} merges in {dt * 1000:.0f} ms  ({dt / _DUPES * 1e6:.0f} µs/merge)"
    )

    fails = _verify(gm, pairs, merged, start_count)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(f"data dir: {workdir}")
    print(
        f"=== RESULT (PASS) — {_DUPES} merges in {dt * 1000:.0f} ms, "
        f"sums/averages exact, all edges rewired ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
