# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-4 · the idle-thought engine was an echo chamber (VISION.md §5).

`dmn_top_k` was both the candidate pool and the sample, so imagination was
seeded from the *same* five argmax nodes for the life of the graph, and every
stored thought was `how_does_x_affect_y` over a pair drawn from that set.

These tests pin the three properties the fix has to hold:

1. seeds are drawn from `dmn_candidate_pool_k`, weighted and non-repeating, so
   the reachable set is the pool rather than the argmax five;
2. the question menu rotates, so the facet vocabulary is actually used;
3. none of it costs an extra graph scan or an extra inference call — the budget
   and the number of `top_nodes_by_score` reads per cycle are unchanged.

No model is loaded (`FakeInferenceBackend`, rules.md §8).
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("neuropaca.idle.dmn")

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config, ConfigError
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import _offered_templates
from neuropaca.core.models import Event
from neuropaca.idle.dmn import DefaultModeNetwork
from neuropaca.learning.prompts import (
    PROACTIVE_TEMPLATES,
    RELATIONAL_THOUGHTS,
    build_proactive_grammar,
    template_rotation,
)


async def _dmn(tmp_path, *, seed: int = 0, **cfg):
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    runtime = BitNetRuntime.get_instance()
    dmn = DefaultModeNetwork(
        bus,
        Config(inference_backend="fake", **cfg),
        gm,
        runtime,
        clock=FakeClock(),
        rng=random.Random(seed),
    )
    await dmn.initialize()
    await dmn.start()
    return dmn, bus, gm


async def _populate(gm, count: int) -> None:
    """A score cliff like the live graph: two dominant apps, then a long tail."""
    for i in range(count):
        await gm.add_node(
            f"app:{i}",
            NodeType.APP,
            {"label": f"service {i}", "relevance_score": max(0.5, 9.0 - i * 0.4)},
        )


# ================================================= 1 · the seed set actually moves


async def test_seeds_are_drawn_from_the_wider_pool_not_the_argmax_five(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path, dmn_top_k=5, dmn_candidate_pool_k=20)
    try:
        await _populate(gm, 20)
        seen: set[str] = set()
        for _ in range(12):
            seen.update(n.id for n in dmn._seed_nodes())
        # The old shape could only ever return the same 5 ids, forever.
        assert len(seen) > 5
        # And it stays inside the pool — the tail past 20 is not reachable.
        assert seen <= {f"app:{i}" for i in range(20)}
    finally:
        await dmn.stop()
        await bus.stop()


async def test_a_seed_draw_has_no_duplicates_and_is_the_requested_size(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path, dmn_top_k=5, dmn_candidate_pool_k=20)
    try:
        await _populate(gm, 20)
        for _ in range(20):
            seeds = dmn._seed_nodes()
            assert len(seeds) == 5
            assert len({n.id for n in seeds}) == 5
    finally:
        await dmn.stop()
        await bus.stop()


async def test_high_score_nodes_are_still_favoured(tmp_path) -> None:
    """Sampling must not become a uniform shuffle — the apps you live in should
    still dominate, just not to the exclusion of everything else."""
    dmn, bus, gm = await _dmn(
        tmp_path, dmn_top_k=3, dmn_candidate_pool_k=20, dmn_seed_refractory_cycles=0
    )
    try:
        await _populate(gm, 20)
        hits: dict[str, int] = {}
        for _ in range(200):
            for node in dmn._seed_nodes():
                hits[node.id] = hits.get(node.id, 0) + 1
        assert hits["app:0"] > hits["app:19"]  # score 9.0 vs the tail floor
    finally:
        await dmn.stop()
        await bus.stop()


async def test_recent_seeds_are_penalised_so_the_next_cycle_moves_on(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path, dmn_top_k=5, dmn_candidate_pool_k=20)
    try:
        await _populate(gm, 20)
        first = {n.id for n in dmn._seed_nodes()}
        dmn._recent_seeds.extend(first)
        # Averaged over many draws, a just-used node comes back far less often.
        repeats = 0
        for _ in range(40):
            repeats += len(first & {n.id for n in dmn._seed_nodes()})
        assert repeats < 40 * 5 * 0.5  # would be ~5/5 every draw under argmax
    finally:
        await dmn.stop()
        await bus.stop()


async def test_refractory_memory_is_bounded_by_config(tmp_path) -> None:
    dmn, bus, _ = await _dmn(tmp_path, dmn_top_k=5, dmn_seed_refractory_cycles=3)
    try:
        assert dmn._recent_seeds.maxlen == 15
        dmn._recent_seeds.extend(f"app:{i}" for i in range(100))
        assert len(dmn._recent_seeds) == 15  # never grows without bound
    finally:
        await dmn.stop()
        await bus.stop()


async def test_a_pool_no_bigger_than_the_sample_is_the_old_deterministic_top_k(tmp_path) -> None:
    """`dmn_candidate_pool_k == dmn_top_k` is the documented escape hatch back to
    fixed argmax seeding."""
    dmn, bus, gm = await _dmn(tmp_path, dmn_top_k=5, dmn_candidate_pool_k=5)
    try:
        await _populate(gm, 20)
        draws = [tuple(n.id for n in dmn._seed_nodes()) for _ in range(5)]
        assert len(set(draws)) == 1
        assert draws[0] == tuple(f"app:{i}" for i in range(5))
    finally:
        await dmn.stop()
        await bus.stop()


# ==================================================== 2 · the question menu rotates


def test_template_rotation_covers_the_whole_vocabulary() -> None:
    offered: set[str] = set()
    for step in range(8):
        offered.update(template_rotation(step))
    assert offered == set(PROACTIVE_TEMPLATES)


def test_every_rotation_offers_both_a_relational_and_a_single_subject_key() -> None:
    """Otherwise a step could force an abstain: a relational template with one
    usable node, or a single-subject template when the model wants a pair."""
    for step in range(8):
        keys = set(template_rotation(step))
        assert keys & RELATIONAL_THOUGHTS
        assert keys - RELATIONAL_THOUGHTS


def test_grammar_only_allows_the_offered_templates() -> None:
    g = build_proactive_grammar(["n1", "n2"], ("what_changed_in_x", "what_connects_x_and_y"))
    assert _offered_templates(g) == ["what_changed_in_x", "what_connects_x_and_y"]
    assert "how_does_x_affect_y" not in g
    assert "why_is_x_active" not in g


def test_grammar_defaults_to_the_full_menu_and_rejects_an_unknown_key() -> None:
    assert set(_offered_templates(build_proactive_grammar(["n1"]))) == set(PROACTIVE_TEMPLATES)
    with pytest.raises(ValueError, match="unknown query template"):
        build_proactive_grammar(["n1"], ("no_such_template",))
    with pytest.raises(ValueError, match="at least one query template"):
        build_proactive_grammar(["n1"], ())


async def test_stored_thoughts_are_not_all_one_facet(tmp_path) -> None:
    """The live-graph symptom: 8 idle thoughts, all `how_does_x_affect_y`."""
    dmn, bus, gm = await _dmn(
        tmp_path, dmn_top_k=5, dmn_candidate_pool_k=20, dmn_max_inferences_per_cycle=3
    )
    try:
        await _populate(gm, 20)
        for _ in range(4):
            await dmn._imagination()
        facets = {gm.get_node(nid).spec.facet for nid in gm.node_ids if nid.startswith("idle:")}
        assert len(facets) > 1
    finally:
        await dmn.stop()
        await bus.stop()


# ========================================================= 3 · no additional load


async def test_a_cycle_still_reads_the_graph_once_and_respects_the_budget(tmp_path) -> None:
    """V-4 must not buy diversity with extra work: still one ranked read and at
    most `dmn_max_inferences_per_cycle` model calls per cycle."""
    dmn, bus, gm = await _dmn(
        tmp_path, dmn_top_k=5, dmn_candidate_pool_k=24, dmn_max_inferences_per_cycle=3
    )
    try:
        await _populate(gm, 40)
        reads: list[int] = []
        real = gm.top_nodes_by_score

        def counting(limit, **kw):
            reads.append(limit)
            return real(limit, **kw)

        gm.top_nodes_by_score = counting  # type: ignore[method-assign]
        backend = dmn._runtime._backend  # the FakeInferenceBackend
        before = len(backend.calls)

        await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
        await dmn._idle_task
        await bus.join()

        assert reads == [24]  # exactly one ranked read, of the pool
        assert len(backend.calls) - before <= 3  # the budget, unchanged
    finally:
        await dmn.stop()
        await bus.stop()


async def test_thoughts_keep_being_produced_across_many_cycles(tmp_path) -> None:
    """The echo chamber's real cost: once every pair in the frozen top-5 had been
    asked, `upsert_fact` deduped every later thought and imagination went
    permanently silent while still spending its inference budget."""
    dmn, bus, gm = await _dmn(
        tmp_path, dmn_top_k=5, dmn_candidate_pool_k=24, dmn_max_inferences_per_cycle=2
    )
    try:
        await _populate(gm, 24)
        for _ in range(6):
            await dmn._imagination()
        early = dmn._thoughts
        for _ in range(6):
            await dmn._imagination()
        assert dmn._thoughts > early  # still finding new questions to ask
    finally:
        await dmn.stop()
        await bus.stop()


# ================================================================== 4 · the config


def test_pool_smaller_than_the_sample_is_rejected() -> None:
    with pytest.raises(ConfigError, match="dmn_candidate_pool_k"):
        Config(inference_backend="fake", dmn_top_k=10, dmn_candidate_pool_k=4)


def test_refractory_cycles_may_be_zero_but_not_negative() -> None:
    Config(inference_backend="fake", dmn_seed_refractory_cycles=0)
    with pytest.raises(ConfigError, match="dmn_seed_refractory_cycles"):
        Config(inference_backend="fake", dmn_seed_refractory_cycles=-1)


# gen-ref: 456c7b23
