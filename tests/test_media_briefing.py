# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Media continuity briefing candidate and formatting tests (S3 · Media continuity).

Tests:
1. Clause composition & continuity formatting (format_media_continuity)
2. Grounding guards & candidate generation (_media_continuity_candidates)
3. Stale suppression & invalidation filtering
4. build_candidates & compose_briefing integration
5. End-to-end integration with MediaIngest sensing module
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.media_format import format_media_continuity, series_slug
from neuropaca.interface.briefing import (
    _media_continuity_candidates,
    build_candidates,
    compose_briefing,
)
from neuropaca.sensing.media_ingest import MediaIngest

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=_NOW)


async def _graph(tmp_path: Path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    return gm


async def _store(tmp_path: Path) -> EpisodeStore:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    return store


# ---------------------------------------------------------------- formatting


def test_format_media_continuity_video_season_and_episode() -> None:
    text = format_media_continuity(
        media_type="video",
        show="Example Anime",
        season=3,
        episode=1,
    )
    assert text == "You were on episode 1 of season 3 of Example Anime."


def test_format_media_continuity_video_episode_only() -> None:
    text = format_media_continuity(
        media_type="video",
        show="Severance",
        season=None,
        episode=9,
    )
    assert text == "You were on episode 9 of Severance."


def test_format_media_continuity_music_with_album() -> None:
    text = format_media_continuity(
        media_type="music",
        artist="Queen",
        album="A Night at the Opera",
        title="Bohemian Rhapsody",
    )
    assert text == "You were listening to A Night at the Opera by Queen."


def test_format_media_continuity_music_without_album() -> None:
    text = format_media_continuity(
        media_type="music",
        artist="Radiohead",
        album=None,
        title="Karma Police",
    )
    assert text == "You were listening to Karma Police by Radiohead."


def test_format_media_continuity_empty_fallback() -> None:
    text = format_media_continuity(media_type="video")
    assert text == ""


def test_series_slug() -> None:
    assert series_slug("Breaking Bad") == "breaking-bad"
    assert series_slug("  Example's   Show 3  ") == "examples-show-3"


# ---------------------------------------------------------------- candidate generation


async def test_media_continuity_candidates_grounding_guard(tmp_path: Path) -> None:
    """A media fact whose entity is NOT in GraphMemory is dropped by the grounding guard."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        # Assert fact in store, but do NOT add node to gm
        store.assert_fact(
            EpisodeKind.MEDIA_POSITION_FACT,
            "series:severance",
            "9",
            valid_from=_NOW,
            source="media_ingest",
            attrs={
                "media_type": "video",
                "show": "Severance",
                "episode": 9,
                "summary_text": "You were on episode 9 of Severance.",
            },
        )
        await store.flush()

        candidates = await _media_continuity_candidates(gm, store, now=_NOW)
        assert len(candidates) == 0

        # Now ground the node in gm
        await gm.add_node("series:severance", NodeType.SERIES, attributes={"label": "Severance"})
        candidates = await _media_continuity_candidates(gm, store, now=_NOW)
        assert len(candidates) == 1
        assert candidates[0].anchor == "series:severance"
        assert candidates[0].text == "You were on episode 9 of Severance."
        assert candidates[0].evidence == ("series:severance",)
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_continuity_candidates_stale_suppression(tmp_path: Path) -> None:
    """Facts older than stale_days are suppressed."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        await gm.add_node("series:dark", NodeType.SERIES, attributes={"label": "Dark"})

        # 10 days old fact (default stale_days = 7)
        old_time = _NOW - timedelta(days=10)
        store.assert_fact(
            EpisodeKind.MEDIA_POSITION_FACT,
            "series:dark",
            "8",
            valid_from=old_time,
            source="media_ingest",
            attrs={
                "media_type": "video",
                "show": "Dark",
                "season": 3,
                "episode": 8,
                "summary_text": "You were on episode 8 of season 3 of Dark.",
            },
        )
        await store.flush()

        candidates = await _media_continuity_candidates(gm, store, now=_NOW, stale_days=7)
        assert len(candidates) == 0

        # With 14 stale_days, it appears
        candidates = await _media_continuity_candidates(gm, store, now=_NOW, stale_days=14)
        assert len(candidates) == 1
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_continuity_candidates_in_build_candidates(tmp_path: Path) -> None:
    """build_candidates incorporates media continuity candidates."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        await gm.add_node("series:queen", NodeType.SERIES, attributes={"label": "Queen"})
        store.assert_fact(
            EpisodeKind.MEDIA_POSITION_FACT,
            "series:queen",
            "A Night at the Opera",
            valid_from=_NOW,
            source="media_ingest",
            attrs={
                "media_type": "music",
                "artist": "Queen",
                "album": "A Night at the Opera",
                "summary_text": "You were listening to A Night at the Opera by Queen.",
            },
        )
        await store.flush()

        candidates = await build_candidates(
            gm, store, now=_NOW, last_briefing_seq=0, config=Config(inference_backend="fake")
        )
        media_items = [c for c in candidates if c.anchor == "series:queen"]
        assert len(media_items) == 1
        assert media_items[0].text == "You were listening to A Night at the Opera by Queen."
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_compose_briefing_with_media_candidate(tmp_path: Path) -> None:
    """compose_briefing selects media continuity candidate when focused and relevant."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        entity_id = "series:breaking-bad"
        await gm.add_node(
            entity_id,
            NodeType.SERIES,
            attributes={"label": "Breaking Bad", "relevance_score": 9.0},
        )
        await gm.add_edge(entity_id, "domain:media", relation=RelationType.PART_OF)

        store.assert_fact(
            EpisodeKind.MEDIA_POSITION_FACT,
            entity_id,
            "7",
            valid_from=_NOW,
            source="media_ingest",
            attrs={
                "media_type": "video",
                "show": "Breaking Bad",
                "season": 2,
                "episode": 7,
                "summary_text": "You were on episode 7 of season 2 of Breaking Bad.",
            },
        )
        await store.flush()

        moment = await compose_briefing(
            gm,
            store,
            focus_history=[(entity_id, _NOW)],
            now=_NOW,
            last_briefing_seq=0,
            config=Config(inference_backend="fake"),
        )

        assert moment is not None
        assert moment.kind == "briefing"
        assert entity_id in moment.evidence
        assert "You were on episode 7 of season 2 of Breaking Bad." in moment.text
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_media_briefing_end_to_end_with_sensor(tmp_path: Path, fake_clock: FakeClock) -> None:
    """End-to-end integration: MediaIngest polls MPRIS, store asserts fact, briefing formats it."""
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    cfg = Config(inference_backend="fake", media_tracking_enabled=True)
    ingest = MediaIngest(bus, cfg, gm, store, clock=fake_clock)
    await ingest.initialize()

    try:
        mpris_list = [{"name": "org.mpris.MediaPlayer2.brave.instance42", "pid": 42}]
        player_props: dict[str, Any] = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 120000000},
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": "Severance Season 1 Episode 9 The We We Are",
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }

        async def mock_run_busctl(*args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
            if "list" in args:
                return 0, json.dumps(mpris_list)
            if "GetAll" in args:
                return 0, json.dumps(player_props)
            return -1, ""

        ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign]

        facts = await ingest.poll_tick()
        assert facts == 1
        await store.flush()

        entity_id = "series:severance"
        assert gm.has_node(entity_id)

        # Now compose briefing with this entity in focus history
        moment = await compose_briefing(
            gm,
            store,
            focus_history=[(entity_id, fake_clock.now())],
            now=fake_clock.now(),
            last_briefing_seq=0,
            config=cfg,
        )

        assert moment is not None
        assert entity_id in moment.evidence
        assert "You were on episode 9 of season 1 of Severance." in moment.text
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
