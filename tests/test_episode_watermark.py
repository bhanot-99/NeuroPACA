# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · graph-store consistency — the watermark catch-up (VISION_PHASES.md).

The bus is fire-and-forget and drops under backpressure by design, and an
isolated subscriber failure (rules.md §2) can leave `EpisodicWriter` recording
a span that `SignalCorrelator` never got to mutate the graph for. This
simulates exactly that: a span lands in the store (as if `EpisodicWriter` ran)
but the *sensor's own* graph mutation never happened — a dropped/failed
handler. One `Scheduler._tick()` must heal it: the node, its structure, a
Hebbian bump derived from the graph's own `last_seen_at`, and the sighting.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.graph_rebuild import catch_up_focus_span
from neuropaca.orchestration.scheduler import Scheduler

_T0 = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


async def test_forced_drop_self_heals_within_one_tick(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    assert gm.get_node("app:code").last_seen_at is None  # the "dropped event" state

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    # The event the sensor's own handler would have mutated the graph from —
    # but never did, simulating a bus-backpressure drop.
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    await store.flush()
    assert gm.last_episode_seq == 0

    scheduler = Scheduler(gm, Config(inference_backend="fake"), store)
    await scheduler._tick()

    assert gm.last_episode_seq == 1
    assert gm.get_node("app:code").last_seen_at == _T0 + timedelta(minutes=5)
    await store.stop()


async def test_watermark_is_idempotent_across_ticks(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    await store.flush()

    scheduler = Scheduler(gm, Config(inference_backend="fake"), store)
    await scheduler._tick()
    await scheduler._tick()  # nothing new since — must not re-replay or regress

    assert gm.last_episode_seq == 1
    await store.stop()


async def test_no_episode_store_is_a_silent_noop(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    scheduler = Scheduler(gm, Config(inference_backend="fake"), None)
    await scheduler._tick()  # must not raise
    assert gm.last_episode_seq == 0


async def test_catch_up_wires_a_hebbian_bump_from_a_warm_peer(tmp_path) -> None:
    """`app:terminal` was correctly focused and sighted by the live path;
    `app:code`'s own switch got dropped. The catch-up must wire them together
    using `app:terminal`'s real `last_seen_at`, not skip straight to `mark_seen`."""
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    await gm.add_node("app:terminal", NodeType.APP, {"label": "Terminal"})
    await gm.mark_seen("app:terminal", _T0)  # the live path's own correct sighting

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    # Same instant as terminal's own sighting -> full credit (gap 0).
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    await store.flush()

    cfg = Config(inference_backend="fake")
    scheduler = Scheduler(gm, cfg, store)
    await scheduler._tick()

    edges = {e.target_id: e.weight for e in gm.get_edges("app:code")}
    assert edges.get("app:terminal") == cfg.hebbian_delta  # one full-credit saturating step
    await store.stop()


async def test_catch_up_recovers_domain_and_browser_structure(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:code",
        _T0,
        _T0 + timedelta(minutes=5),
        obj="domain:engineering",
    )
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "webapp:gmail",
        _T0 + timedelta(minutes=5),
        _T0 + timedelta(minutes=10),
        obj="domain:comms",
        attrs={"browser": "app:brave"},
    )
    await store.flush()

    scheduler = Scheduler(gm, Config(inference_backend="fake"), store)
    await scheduler._tick()

    code_edges = {(e.target_id, e.relation) for e in gm.get_edges("app:code")}
    assert ("domain:engineering", RelationType.PART_OF) in code_edges
    gmail_edges = {(e.target_id, e.relation) for e in gm.get_edges("webapp:gmail")}
    assert ("app:brave", RelationType.PART_OF) in gmail_edges
    assert ("domain:comms", RelationType.PART_OF) in gmail_edges
    await store.stop()


async def test_catch_up_structure_is_idempotent_when_replayed_twice(tmp_path) -> None:
    """The scheduler's own watermark never replays a row twice, but the
    building block it calls must be safe if something ever did — re-adding a
    `PART_OF` edge resets it (T7), so a second pass must not duplicate one."""
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:code",
        _T0,
        _T0 + timedelta(minutes=5),
        obj="domain:engineering",
    )
    await store.flush()
    (row,) = await store.since(0)

    cfg = Config(inference_backend="fake")
    await catch_up_focus_span(gm, row, cfg)
    await catch_up_focus_span(gm, row, cfg)  # replayed again — must not duplicate

    part_of = [
        e
        for e in gm.get_edges("app:code")
        if e.relation is RelationType.PART_OF and e.target_id == "domain:engineering"
    ]
    assert len(part_of) == 1
    await store.stop()


async def test_catch_up_stays_cheap_at_10k_nodes(tmp_path) -> None:
    """The O(graph size) scan for warm peers (§0's load budget) is only ever
    paid on a missed row — rare — but must still stay well under the 50 ms
    retrieval budget even on a large graph."""
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    for i in range(10_000):
        await gm.add_node(f"app:filler{i}", NodeType.APP, {"label": f"filler{i}"})
    await gm.add_node("app:terminal", NodeType.APP, {"label": "Terminal"})
    await gm.mark_seen("app:terminal", _T0)

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    await store.flush()
    (row,) = await store.since(0)

    start = time.perf_counter()
    await catch_up_focus_span(gm, row, Config(inference_backend="fake"))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"catch_up_focus_span at 10k nodes took {elapsed * 1000:.1f} ms"
    await store.stop()


async def test_idle_span_catch_up_still_only_marks_seen(tmp_path) -> None:
    """`SignalCorrelator` never reacts to idle/activity events at all — an
    idle span's catch-up has nothing else to redo."""
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    store.record_span(EpisodeKind.IDLE_SPAN, "YOU", _T0, _T0 + timedelta(minutes=20))
    await store.flush()

    scheduler = Scheduler(gm, Config(inference_backend="fake"), store)
    await scheduler._tick()

    assert gm.get_node("YOU").last_seen_at == _T0 + timedelta(minutes=20)
    await store.stop()


# gen-ref: e21f8a4c
