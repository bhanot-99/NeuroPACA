"""B1 exit criteria · performance (phases.md B1).

Against a deterministic 10 000-node / 25 000-edge graph:
  - `GraphMemory.load()` completes in < 2.0 s
  - `find_related(depth=2, traverse_hubs=False)` averages < 50 ms

The fixture is generated once per session (seeded, reproducible) and cached at
``tests/fixtures/graph_10k.json`` — gitignored, ~4 MB.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from neuropaca.core.graph_memory import HUB_NODE_IDS, GraphMemory
from tests.fixtures.generate_10k_graph import FIXTURE_PATH, write_fixture

_LOAD_BUDGET_S = 2.0
_TRAVERSAL_BUDGET_S = 0.05
_SAMPLE = 5


@pytest.fixture(scope="session")
def graph_10k() -> tuple[Path, dict]:
    if not FIXTURE_PATH.exists():
        write_fixture(FIXTURE_PATH)
    payload = json.loads(FIXTURE_PATH.read_text("utf-8"))
    return FIXTURE_PATH, payload


async def _loaded(path: Path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(path))
    await gm.load()
    return gm


async def test_load_10k_graph_under_two_seconds(graph_10k: tuple[Path, dict]) -> None:
    path, payload = graph_10k
    gm = GraphMemory.get_instance(persistence_path=str(path))

    start = time.perf_counter()
    await gm.load()
    elapsed = time.perf_counter() - start

    assert gm.node_count == len(payload["nodes"]) == 10_011
    assert gm.edge_count == len(payload["edges"]) == 25_000
    assert elapsed < _LOAD_BUDGET_S, f"load() took {elapsed:.3f}s (budget {_LOAD_BUDGET_S}s)"


async def test_find_related_depth2_averages_under_50ms(graph_10k: tuple[Path, dict]) -> None:
    path, _ = graph_10k
    gm = await _loaded(path)

    rng = random.Random(1234)
    leaves = [n for n in gm.node_ids if n not in HUB_NODE_IDS]
    sample = rng.sample(leaves, _SAMPLE)

    durations: list[float] = []
    for node_id in sample:
        start = time.perf_counter()
        gm.find_related(node_id, depth=2, traverse_hubs=False)
        durations.append(time.perf_counter() - start)

    average = sum(durations) / len(durations)
    assert average < _TRAVERSAL_BUDGET_S, (
        f"find_related(depth=2) averaged {average * 1000:.1f} ms over {_SAMPLE} runs "
        f"(budget {_TRAVERSAL_BUDGET_S * 1000:.0f} ms); "
        f"per-run {[round(d * 1000, 1) for d in durations]}"
    )


async def test_find_related_excludes_hub_through_routes_at_scale(
    graph_10k: tuple[Path, dict],
) -> None:
    path, _ = graph_10k
    gm = await _loaded(path)

    # A leaf wired only to YOU must not surface YOU's other neighbours at depth 2.
    probe_type = gm.get_node("leaf:00000").node_type
    a_relation = next(iter(gm.get_edges("YOU"))).relation
    await gm.add_node("leaf:probe", probe_type, None)
    await gm.add_edge("leaf:probe", "YOU", a_relation)

    guarded = {n.id for n in gm.find_related("leaf:probe", depth=2, traverse_hubs=False)}
    opened = {n.id for n in gm.find_related("leaf:probe", depth=2, traverse_hubs=True)}

    assert guarded <= {"YOU"}
    assert len(opened) > len(guarded)
