# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · curiosity wired into the DMN's seed choice (VISION_PHASES.md §3.7).

`_choose_seeds()` mixes: with probability `1 - dmn_curiosity_epsilon` the
highest-information-gain pair among the V-4 candidate pool; with
`dmn_curiosity_epsilon` (and always as the fallback) the existing V-4
score-weighted sample. No model is loaded (`FakeInferenceBackend`, rules.md §8).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.idle.dmn import DefaultModeNetwork

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


class _AlwaysCurious(random.Random):
    """Forces `_choose_seeds()` down the curiosity branch every draw.

    The production check is `rng.random() >= dmn_curiosity_epsilon` — curious
    with probability `1 - epsilon`. `random()` returning 1.0 satisfies that
    for every valid epsilon in `[0.0, 1.0]`, including the edge `epsilon =
    1.0` itself (an earlier version of this file had this and `_NeverCurious`
    backwards: 0.0 here and 1.0 there, which silently forced the *opposite*
    branch in every test using them for any `epsilon > 0`)."""

    def random(self) -> float:
        return 1.0


class _NeverCurious(random.Random):
    """Forces `_choose_seeds()` down the V-4 fallback every draw, for any
    `epsilon > 0.0` (the only values every test here actually uses)."""

    def random(self) -> float:
        return 0.0


async def _store(tmp_path) -> EpisodeStore:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    return store


async def _dmn(tmp_path, *, episodes: EpisodeStore | None, rng: random.Random, **cfg):
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
        clock=FakeClock(wall=_NOW),
        rng=rng,
        episode_store=episodes,
    )
    await dmn.initialize()
    await dmn.start()
    return dmn, bus, gm


async def _populate(gm, count: int) -> None:
    for i in range(count):
        await gm.add_node(
            f"app:{i}", NodeType.APP, {"label": f"service {i}", "relevance_score": 5.0}
        )


def _span(store, subject, at, seconds=30):
    store.record_span(EpisodeKind.FOCUS_SPAN, subject, at, at + timedelta(seconds=seconds))


async def test_falls_back_to_v4_sampling_with_no_episode_store(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path, episodes=None, rng=_AlwaysCurious(), dmn_top_k=4)
    try:
        await _populate(gm, 10)
        seeds = await dmn._choose_seeds()
        assert len(seeds) == 4
    finally:
        await dmn.stop()
        await bus.stop()


async def test_falls_back_to_v4_sampling_below_epsilon_roll(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path, episodes=store, rng=_NeverCurious(), dmn_top_k=4, dmn_curiosity_epsilon=0.2
        )
        try:
            await _populate(gm, 10)
            _span(store, "app:0", _NOW - timedelta(hours=1))
            _span(store, "app:1", _NOW - timedelta(hours=1))
            await store.flush()
            seeds = await dmn._choose_seeds()
            assert len(seeds) == 4  # the V-4 sample size, not a 2-node pair
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_falls_back_to_v4_sampling_with_a_pool_of_one(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path, episodes=store, rng=_AlwaysCurious(), dmn_top_k=1, dmn_candidate_pool_k=1
        )
        try:
            await _populate(gm, 1)
            seeds = await dmn._choose_seeds()
            assert seeds == dmn._seed_nodes()
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_falls_back_to_v4_sampling_with_no_evidence_at_all(tmp_path) -> None:
    """An empty episode store has no pairs to rank — every IG is tied at the
    Beta(1,1) prior, and `top_information_gain_pairs` still returns a ranking,
    but it carries zero real evidence. Curiosity still degrades gracefully:
    the seeds it returns are simply the two members of whichever pair sorted
    first, not a crash — verified via the shape, not a specific pair."""
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path, episodes=store, rng=_AlwaysCurious(), dmn_top_k=2, dmn_candidate_pool_k=5
        )
        try:
            await _populate(gm, 5)
            seeds = await dmn._choose_seeds()
            assert len(seeds) == 2
            assert len({n.id for n in seeds}) == 2
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_curiosity_does_not_echo_chamber_when_ig_is_tied(tmp_path) -> None:
    """A regression for V-4's own bug, reintroduced: with no differentiating
    evidence, every pair ties at the `Beta(1,1)` prior and a bare stable sort
    would pick the identical pair forever. `_choose_seeds` must move on once
    `_recent_seeds` (populated the same way `_imagination` populates it) marks
    the previous pair as just used — the same refractory idea V-4 already
    relies on for its own sampling path."""
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path,
            episodes=store,
            rng=_AlwaysCurious(),
            dmn_top_k=2,
            dmn_candidate_pool_k=6,
            dmn_curiosity_top_pairs=15,  # every C(6,2)=15 pair, all tied
        )
        try:
            await _populate(gm, 6)
            seen_pairs: set[frozenset[str]] = set()
            for _ in range(10):
                seeds = await dmn._choose_seeds()
                seen_pairs.add(frozenset(n.id for n in seeds))
                dmn._recent_seeds.extend(n.id for n in seeds)
            assert len(seen_pairs) > 1
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_curiosity_picks_the_least_settled_pair_over_the_settled_one(tmp_path) -> None:
    """app:settled_a / app:settled_b co-occur every time (confidently "always
    together"); app:novel_a / app:novel_b appear only once each, never together
    (maximally uncertain). Curiosity must prefer the novel pair's nodes."""
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path,
            episodes=store,
            rng=_AlwaysCurious(),
            dmn_top_k=2,
            dmn_candidate_pool_k=4,
        )
        try:
            for name in ("settled_a", "settled_b", "novel_a", "novel_b"):
                await gm.add_node(
                    f"app:{name}", NodeType.APP, {"label": name, "relevance_score": 5.0}
                )
            for i in range(10):
                base = _NOW - timedelta(days=1, minutes=10 * i)
                _span(store, "app:settled_a", base)
                _span(store, "app:settled_b", base + timedelta(seconds=10))
            _span(store, "app:novel_a", _NOW - timedelta(hours=5))
            _span(store, "app:novel_b", _NOW - timedelta(hours=6))
            await store.flush()

            seeds = await dmn._choose_seeds()
            assert {n.id for n in seeds} == {"app:novel_a", "app:novel_b"}
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_curiosity_never_grows_the_recent_seeds_deque_unbounded(tmp_path) -> None:
    """`_imagination()` extends `_recent_seeds` with whatever `_choose_seeds()`
    returned — curiosity's seeds go through the same V-4 refractory bookkeeping,
    not a separate untracked path."""
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path,
            episodes=store,
            rng=_AlwaysCurious(),
            dmn_top_k=2,
            dmn_candidate_pool_k=4,
            dmn_seed_refractory_cycles=3,
        )
        try:
            for name in ("a", "b", "c", "d"):
                await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
            _span(store, "app:a", _NOW - timedelta(hours=1))
            _span(store, "app:b", _NOW - timedelta(hours=2))
            await store.flush()
            await dmn._imagination()
            assert dmn._recent_seeds.maxlen == 6
            assert len(dmn._recent_seeds) <= 6
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()


async def test_a_broken_episode_read_falls_back_rather_than_raising(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        dmn, bus, gm = await _dmn(
            tmp_path, episodes=store, rng=_AlwaysCurious(), dmn_top_k=4, dmn_candidate_pool_k=10
        )
        try:
            await _populate(gm, 10)

            async def _boom(_t0, _t1):
                raise RuntimeError("disk went away")

            store.between = _boom  # type: ignore[method-assign]
            seeds = await dmn._choose_seeds()
            assert len(seeds) == 4  # the V-4 fallback, not a raised exception
        finally:
            await dmn.stop()
            await bus.stop()
    finally:
        await store.stop()
