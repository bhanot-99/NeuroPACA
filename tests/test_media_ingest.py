# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for MediaIngest (S3 · Media).

Presents MPRIS players in every state:
1. Clean video pattern match (extracts show, season, episode; records fact, span, graph node).
2. No match (non-media / noisy web titles dropped, never stored raw in the graph).
3. Music with clean structured metadata (xesam:artist/album trusted as-is).
4. Pause / resume span boundaries (Playing opens span, Paused/Stopped closes it).
5. Track change closing one span and opening another.
6. Missing busctl binary (clean self-disable via shutil.which).
7. Read-only D-Bus verification (never calls any mutating method).
8. Forget series (cleanses facts, spans, graph nodes, and in-memory caches).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.sensing.media_ingest import MediaIngest

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=_NOW)


async def _setup_ingest(
    tmp_path: Path, clock: FakeClock
) -> tuple[MediaIngest, GraphMemory, EpisodeStore, EventBus]:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    cfg = Config(inference_backend="fake", media_tracking_enabled=True)
    ingest = MediaIngest(bus, cfg, gm, store, clock=clock)
    await ingest.initialize()
    return ingest, gm, store, bus


async def test_media_ingest_clean_video_pattern_match(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Video title matching allowlist regex produces node and EpisodeKind.MEDIA_POSITION_FACT."""
    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        # Mock MPRIS busctl response
        mpris_list = [{"name": "org.mpris.MediaPlayer2.brave.instance1234", "pid": 1234}]
        player_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 120000000},  # 120.0 s
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "mpris:length": {"type": "x", "data": 1440000000},
                            "xesam:title": {
                                "type": "s",
                                "data": ("Example Anime 3 Episode 1 Watch All Episodes at Hianime"),
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(player_props)
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        facts = await ingest.poll_tick()
        assert facts == 1
        await store.flush()

        entity_id = "series:example-anime"
        assert gm.has_node(entity_id)
        node = gm.get_node(entity_id)
        assert node is not None
        assert node.node_type == NodeType.SERIES
        assert node.label == "Example Anime"

        # Check edge to domain:media
        assert any(
            e.target_id == "domain:media" and e.relation == RelationType.PART_OF
            for e in gm.get_edges(entity_id)
        )

        # Check superseding fact in store
        open_facts = await store.at(fake_clock.now())
        media_facts = [f for f in open_facts if f.kind == str(EpisodeKind.MEDIA_POSITION_FACT)]
        assert len(media_facts) == 1
        fact = media_facts[0]
        assert fact.subject == entity_id
        assert fact.attrs["show"] == "Example Anime"
        assert fact.attrs["season"] == 3
        assert fact.attrs["episode"] == 1
        assert pytest.approx(fact.attrs["position_seconds"], rel=1e-2) == 120.0
        assert fact.attrs["summary_text"] == "You were on episode 1 of season 3 of Example Anime."

        # Verify fact churn suppression and V-10 non-inflation on subsequent tick
        initial_access_count = node.access_count
        await fake_clock.advance(30.0)
        # Player advances position by 30 seconds
        player_props["data"][0]["Position"]["data"] = 150000000  # 150.0 s
        facts_second_tick = await ingest.poll_tick()
        assert facts_second_tick == 0  # No fact churn!
        node_after = gm.get_node(entity_id)
        assert node_after is not None
        # V-10: mark_seen only moves last_seen_at; it does NOT touch access_count
        assert node_after.access_count == initial_access_count

        # Check open span
        assert "org.mpris.MediaPlayer2.brave.instance1234" in ingest._open_spans
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_no_pattern_match_dropped_never_stored(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """A non-media browser title is dropped completely without storing facts or graph nodes."""
    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.brave.instance5555", "pid": 5555}]
        player_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 30000000},
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {"type": "s", "data": "GitHub - user/repo: A git repo"},
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(player_props)
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        facts = await ingest.poll_tick()
        assert facts == 0
        await store.flush()

        # Verify nothing was added to GraphMemory or EpisodeStore
        series_nodes = [n for n in gm.node_ids if n.startswith("series:")]
        assert len(series_nodes) == 0

        open_facts = await store.at(fake_clock.now())
        media_facts = [f for f in open_facts if f.kind == str(EpisodeKind.MEDIA_POSITION_FACT)]
        assert len(media_facts) == 0
        assert ingest._titles_dropped == 1
        assert len(ingest._open_spans) == 0
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_music_trusted_metadata(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Music players with xesam:artist or album are trusted as-is without regex pattern need."""
    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.spotify", "pid": 5678}]
        player_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 120000000},  # 120 s
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "mpris:length": {"type": "x", "data": 354000000},
                            "xesam:title": {"type": "s", "data": "Bohemian Rhapsody"},
                            "xesam:artist": {"type": "as", "data": ["Queen"]},
                            "xesam:album": {"type": "s", "data": "A Night at the Opera"},
                        },
                    },
                }
            ]
        }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(player_props)
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        facts = await ingest.poll_tick()
        assert facts == 1
        await store.flush()

        entity_id = "series:a-night-at-the-opera"
        assert gm.has_node(entity_id)
        node = gm.get_node(entity_id)
        assert node is not None
        assert node.label == "A Night at the Opera"

        open_facts = await store.at(fake_clock.now())
        fact = next(f for f in open_facts if f.subject == entity_id)
        assert fact.attrs["artist"] == "Queen"
        assert fact.attrs["album"] == "A Night at the Opera"
        assert fact.attrs["title"] == "Bohemian Rhapsody"
        assert fact.attrs["summary_text"] == "You were listening to A Night at the Opera by Queen."
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_pause_resume_span_boundaries(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Playing opens a span; Paused or Stopped closes it; resuming opens a new span."""
    ingest, _gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.vlc", "pid": 3333}]
        status = "Playing"

        def _get_props() -> dict[str, Any]:
            return {
                "data": [
                    {
                        "PlaybackStatus": {"type": "s", "data": status},
                        "Position": {"type": "x", "data": 60000000},
                        "Metadata": {
                            "type": "a{sv}",
                            "data": {
                                "xesam:title": {"type": "s", "data": "Severance Episode 9"},
                                "xesam:artist": {"type": "as", "data": [""]},
                                "xesam:album": {"type": "s", "data": ""},
                            },
                        },
                    }
                ]
            }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(_get_props())
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        # 1. Tick 1: Playing
        await ingest.poll_tick()
        assert "org.mpris.MediaPlayer2.vlc" in ingest._open_spans

        # 2. Advance clock by 300s, switch status to Paused
        await fake_clock.advance(300.0)
        status = "Paused"
        await ingest.poll_tick()
        assert "org.mpris.MediaPlayer2.vlc" not in ingest._open_spans
        await store.flush()

        # Check span was closed and recorded
        history = await store.for_entity("series:severance")
        spans = [h for h in history if h.kind == str(EpisodeKind.MEDIA_SPAN)]
        assert len(spans) == 1
        assert spans[0].t_start is not None and spans[0].t_end is not None
        assert (spans[0].t_end - spans[0].t_start).total_seconds() == pytest.approx(300.0)

        # 3. Advance clock by 60s, resume Playing
        await fake_clock.advance(60.0)
        status = "Playing"
        await ingest.poll_tick()
        assert "org.mpris.MediaPlayer2.vlc" in ingest._open_spans

        # 4. Stop ingest closes the in-flight span
        await fake_clock.advance(150.0)
        await ingest.stop()
        await store.flush()

        history = await store.for_entity("series:severance")
        spans = [h for h in history if h.kind == str(EpisodeKind.MEDIA_SPAN)]
        assert len(spans) == 2
        assert spans[1].t_start is not None and spans[1].t_end is not None
        assert (spans[1].t_end - spans[1].t_start).total_seconds() == pytest.approx(150.0)
    finally:
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_track_change_closes_old_and_opens_new_span(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Switching media closes the previous episode span and starts a new one."""
    ingest, _gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.brave.instance99", "pid": 99}]
        current_title = "Breaking Bad S02E07"

        def _get_props() -> dict[str, Any]:
            return {
                "data": [
                    {
                        "PlaybackStatus": {"type": "s", "data": "Playing"},
                        "Position": {"type": "x", "data": 10000000},
                        "Metadata": {
                            "type": "a{sv}",
                            "data": {
                                "xesam:title": {"type": "s", "data": current_title},
                                "xesam:artist": {"type": "as", "data": [""]},
                                "xesam:album": {"type": "s", "data": ""},
                            },
                        },
                    }
                ]
            }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(_get_props())
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        # Tick 1: episode 7
        await ingest.poll_tick()
        assert (
            ingest._open_spans["org.mpris.MediaPlayer2.brave.instance99"][0]
            == "series:breaking-bad"
        )

        # Advance 1200s, user advances to Episode 8
        await fake_clock.advance(1200.0)
        current_title = "Breaking Bad S02E08"
        await ingest.poll_tick()
        await store.flush()

        # Track changed on same series or new track
        assert "org.mpris.MediaPlayer2.brave.instance99" in ingest._open_spans

        # Stop closes active span
        await fake_clock.advance(500.0)
        await ingest.stop()
        await store.flush()

        history = await store.for_entity("series:breaking-bad")
        facts = [h for h in history if h.kind == str(EpisodeKind.MEDIA_POSITION_FACT)]
        # Verified both facts were asserted (superseding)
        assert len(facts) == 2
        assert facts[0].attrs["episode"] == 7
        assert facts[1].attrs["episode"] == 8
    finally:
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_missing_busctl_self_disables(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When busctl is missing from PATH, MediaIngest disables itself safely."""
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "ep.sqlite")
    await store.start()

    cfg = Config(inference_backend="fake", media_tracking_enabled=True)
    ingest = MediaIngest(bus, cfg, gm, store, clock=fake_clock)

    try:
        await ingest.initialize()
        assert ingest._available is False
        assert ingest.health().detail == "disabled (busctl missing)"
        assert (await ingest.poll_tick()) == 0
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_busctl_calls_strictly_read_only(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """The collector invokes ONLY read-only D-Bus operations (list, GetAll). Never mutations."""
    ingest, _gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        recorded_calls: list[list[str]] = []

        async def inspect_calls(*args: str) -> tuple[int, str]:
            recorded_calls.append(list(args))
            if "list" in args:
                return 0, json.dumps([{"name": "org.mpris.MediaPlayer2.brave", "pid": 1000}])
            return 0, json.dumps({"data": [{}]})

        ingest._run_busctl = inspect_calls  # type: ignore[method-assign,assignment]

        await ingest.poll_tick()

        assert len(recorded_calls) >= 2
        for call_args in recorded_calls:
            # Must be either list or Properties.GetAll
            is_list = "list" in call_args
            is_get_all = "GetAll" in call_args and "org.freedesktop.DBus.Properties" in call_args
            assert is_list or is_get_all, f"Disallowed mutating call: {call_args}"

            # Ensure zero media control calls
            for mutating_verb in ("Play", "Pause", "Stop", "Next", "Previous", "Seek", "Set"):
                assert mutating_verb not in call_args, f"Mutating verb {mutating_verb} called!"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_ingest_forget_cleanses_facts_spans_and_nodes(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """forget(series) purges facts, spans, and graph nodes completely."""
    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock)
    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.vlc", "pid": 4444}]
        player_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 50000000},
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {"type": "s", "data": "Dark Season 3 Episode 8"},
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }

        async def mock_run_busctl(*args: str) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(player_props)
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign,assignment]

        await ingest.poll_tick()
        await store.flush()

        entity_id = "series:dark"
        assert gm.has_node(entity_id)

        # Call forget
        forgotten_count = await ingest.forget("dark")
        await store.flush()

        assert forgotten_count >= 1
        assert not gm.has_node(entity_id)

        # Verify no open facts remain in store
        open_facts = await store.at(fake_clock.now())
        assert len([f for f in open_facts if f.subject == entity_id]) == 0
        assert "org.mpris.MediaPlayer2.vlc" not in ingest._open_spans
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
