# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-10 · provenance timestamps missing on most nodes (VISION.md §5).

`first_seen_at` / `last_seen_at` — "when did this first/last happen", which the
briefing depends on — had exactly one writer: the B13 census, and it only rows
apps over `process_min_rss_mb`. So a light app you focus all day (a terminal, an
editor, anything under 200 MB) never got either. A focus event is the most
direct sighting there is and wrote neither.

V-10 makes a focus a sighting, through `GraphMemory.mark_seen` — which is not an
access, so it cannot move `relevance_score` — behind a per-node gate so a focus
storm stays lock-free, the property V-1 went to lengths for. And because a focus
can now stamp a node before the census does, `first_seen_at` moves from
write-once to earliest-wins, or the census's real, earlier process-start time
would be thrown away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.enums import NodeType
from neuropaca.core.graph_memory import GraphMemory

T = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _graph(tmp_path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


# =========================================================== 1 · mark_seen


async def test_a_first_sighting_sets_both_times(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)

    assert await gm.mark_seen("app:foot", T)

    node = gm.get_node("app:foot")
    assert node.first_seen_at == T and node.last_seen_at == T


async def test_first_only_moves_earlier_and_last_only_later(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.mark_seen("app:foot", T)

    await gm.mark_seen("app:foot", T + timedelta(hours=1))
    await gm.mark_seen("app:foot", T - timedelta(hours=1))

    node = gm.get_node("app:foot")
    assert node.first_seen_at == T - timedelta(hours=1)
    assert node.last_seen_at == T + timedelta(hours=1)


async def test_a_sighting_inside_the_known_span_changes_nothing(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.mark_seen("app:foot", T - timedelta(hours=1))
    await gm.mark_seen("app:foot", T + timedelta(hours=1))

    assert not await gm.mark_seen("app:foot", T)


async def test_a_sighting_is_not_an_access(tmp_path) -> None:
    """ "When did I last see this" and "how much does this matter" are different
    questions. V-10 must not change the answer to the second (V-2)."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, {"relevance_score": 3.3})
    before = gm.get_node("app:foot")

    for i in range(10):
        await gm.mark_seen("app:foot", T + timedelta(minutes=i))

    after = gm.get_node("app:foot")
    assert after.activity == before.activity
    assert after.access_count == before.access_count
    assert after.last_accessed == before.last_accessed
    assert after.relevance_score == before.relevance_score


async def test_a_sighting_of_a_missing_node_never_creates_it(tmp_path) -> None:
    gm = await _graph(tmp_path)
    assert not await gm.mark_seen("app:ghost", T)
    assert not gm.has_node("app:ghost")


async def test_only_a_sighting_that_moves_something_dirties_the_graph(tmp_path) -> None:
    """A repeat sighting must not cost a save."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.mark_seen("app:foot", T)
    await gm.save()
    assert not gm.dirty

    await gm.mark_seen("app:foot", T)
    assert not gm.dirty

    await gm.mark_seen("app:foot", T + timedelta(minutes=5))
    assert gm.dirty


async def test_sightings_survive_a_save_and_load(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.mark_seen("app:foot", T)
    await gm.mark_seen("app:foot", T + timedelta(hours=2))
    await gm.save()

    reloaded = await _graph(tmp_path)
    node = reloaded.get_node("app:foot")
    assert node.first_seen_at == T and node.last_seen_at == T + timedelta(hours=2)


# =========================================== 2 · first_seen_at is earliest-wins


async def test_the_census_s_earlier_start_time_beats_a_focus_stamp(tmp_path) -> None:
    """Write-once would keep the focus stamp, because it got there first — and
    lose the process's real start time, three hours earlier."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:code", NodeType.APP, None)
    await gm.mark_seen("app:code", T)  # the focus event arrives first

    started = T - timedelta(hours=3)
    await gm.upsert_node(
        "app:code",
        NodeType.APP,
        {"first_seen_at": started.isoformat(), "last_seen_at": T.isoformat()},
    )

    assert gm.get_node("app:code").first_seen_at == started


async def test_a_later_first_seen_offer_is_still_refused(tmp_path) -> None:
    """The B13 guarantee is kept: nothing can push `first_seen_at` later."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:code", NodeType.APP, None)
    await gm.mark_seen("app:code", T)

    await gm.upsert_node(
        "app:code", NodeType.APP, {"first_seen_at": (T + timedelta(hours=1)).isoformat()}
    )

    assert gm.get_node("app:code").first_seen_at == T


# ================================ 3 · a focus is a sighting, gated per node

import pytest  # noqa: E402

from neuropaca.core.config import Config  # noqa: E402
from neuropaca.core.enums import EventType  # noqa: E402
from neuropaca.core.event_bus import EventBus  # noqa: E402
from neuropaca.core.models import Event  # noqa: E402
from neuropaca.diagnosis import correlator as corr_mod  # noqa: E402
from neuropaca.diagnosis.correlator import SignalCorrelator  # noqa: E402


def _switch(app_id: str, **extra) -> Event:
    return Event(
        event_type=EventType.APP_SWITCH,
        payload={"app_id": app_id, "title": "", "previous_app_id": None, **extra},
    )


async def _correlator(tmp_path, **cfg):
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    return SignalCorrelator(bus, Config(inference_backend="fake", **cfg), gm), gm, bus


def _count_marks(gm: GraphMemory) -> list[str]:
    calls: list[str] = []
    real = gm.mark_seen

    async def counting(node_id: str, when: datetime) -> bool:
        calls.append(node_id)
        return await real(node_id, when)

    gm.mark_seen = counting  # type: ignore[method-assign]
    return calls


def _app_nodes(gm: GraphMemory) -> list[str]:
    return [n for n in gm.node_ids if n.startswith("app:")]


async def test_a_light_focused_app_gets_its_sighting_times(tmp_path) -> None:
    """The V-10 symptom: an app under the census threshold, focused, used —
    and no first/last-seen, because only the census ever wrote them."""
    corr, gm, bus = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("foot"))

        (node_id,) = _app_nodes(gm)
        node = gm.get_node(node_id)
        assert node.first_seen_at is not None and node.last_seen_at is not None
        assert node.ram_mb is None  # seen, never measured — V-9 and V-10 agree
    finally:
        await bus.stop()


async def test_a_focus_storm_writes_each_sighting_once(tmp_path) -> None:
    """V-1 made a focus storm lock-free. Stamping every focus would undo it."""
    corr, gm, bus = await _correlator(tmp_path)
    try:
        calls = _count_marks(gm)
        for i in range(200):
            await corr.on_app_switch(_switch("foot" if i % 2 else "kitty"))
        assert len(calls) == 2  # one per node, not one per event
    finally:
        await bus.stop()


async def test_a_sighting_is_written_again_once_the_window_passes(tmp_path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(corr_mod, "_now", lambda: clock[0])
    corr, gm, bus = await _correlator(tmp_path)
    try:
        calls = _count_marks(gm)
        await corr.on_app_switch(_switch("foot"))
        clock[0] += corr_mod._SIGHTING_REFRESH_SECONDS / 2
        await corr.on_app_switch(_switch("foot"))
        assert len(calls) == 1  # inside the window: gated, no lock

        clock[0] += corr_mod._SIGHTING_REFRESH_SECONDS
        await corr.on_app_switch(_switch("foot"))
        assert len(calls) == 2
    finally:
        await bus.stop()


async def test_a_tab_is_a_sighting_of_its_browser_too(tmp_path) -> None:
    corr, gm, bus = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(
            _switch("brave-browser", webapp="github", webapp_domain="domain:engineering")
        )
        assert gm.get_node("webapp:github").last_seen_at is not None
        (browser,) = _app_nodes(gm)
        assert gm.get_node(browser).last_seen_at is not None
    finally:
        await bus.stop()


async def test_an_excluded_window_is_not_a_sighting(tmp_path) -> None:
    """Dialogs and portals are not activity (V-1); they are not stamped."""
    corr, gm, bus = await _correlator(tmp_path, focus_exclude_app_ids=["zenity"])
    try:
        calls = _count_marks(gm)
        await corr.on_app_switch(_switch("zenity"))
        assert calls == []
    finally:
        await bus.stop()


async def test_the_gate_s_memory_is_bounded(tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(corr_mod, "_now", lambda: clock[0])
    corr, _gm, bus = await _correlator(tmp_path)
    try:
        corr._sighted_at = {f"app:old{i}": 0.0 for i in range(corr_mod._CANON_CACHE_MAX + 50)}
        clock[0] = corr_mod._SIGHTING_REFRESH_SECONDS * 3  # every entry is stale
        await corr.on_app_switch(_switch("foot"))
        assert len(corr._sighted_at) <= corr_mod._CANON_CACHE_MAX
        assert not any(k.startswith("app:old") for k in corr._sighted_at)
    finally:
        await bus.stop()


@pytest.mark.parametrize("repeat", [1, 5])
async def test_sightings_never_move_the_score_inputs(tmp_path, repeat: int) -> None:
    """End to end through the correlator: the focus path's own upsert still
    touches (it always did); the sighting adds nothing on top."""
    corr, gm, bus = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("foot"))
        (node_id,) = _app_nodes(gm)
        before = gm.get_node(node_id)
        calls = _count_marks(gm)
        for _ in range(repeat):
            await gm.mark_seen(node_id, datetime.now(UTC) + timedelta(hours=1))
        after = gm.get_node(node_id)
        assert calls  # the sighting really ran
        assert (after.activity, after.access_count) == (before.activity, before.access_count)
    finally:
        await bus.stop()


# gen-ref: 70de458a
