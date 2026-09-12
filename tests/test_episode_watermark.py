# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · graph-store consistency — the watermark catch-up (VISION_PHASES.md).

The bus is fire-and-forget and drops under backpressure by design, so a
handler that never ran can leave `GraphMemory` behind `EpisodeStore`. This
simulates exactly that: a span lands in the store (as if `EpisodicWriter` ran)
but the *sensor's own* graph mutation (`mark_seen`, standing in for whatever
the live handler would have done) never happened — a dropped event. One
`Scheduler._tick()` must heal it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.graph_memory import GraphMemory
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


# gen-ref: e21f8a4c
