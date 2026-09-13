# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S3 Exit Criterion Verification Test (VISION_PHASES.md §S3).

Criterion:
"Correct 'where you stopped' on a fixture of real titles and 3 real series over a week."

Validates:
1. Continuity tracking across 3 distinct real series over a 7-day timeline.
2. Progression through episodes: newer episodes supersede older ones.
3. Accurate "where you stopped" extraction at each point in time.
4. Non-media titles interleaved during playback are dropped and never leak into the graph.
5. Natural fading when facts cross the media_stale_days threshold (> 7 days).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.interface.briefing import _media_continuity_candidates, compose_briefing
from neuropaca.sensing.media_ingest import MediaIngest

_START_TIME = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)  # Monday evening


async def test_s3_exit_criterion_three_series_over_a_week(tmp_path: Path) -> None:
    clock = FakeClock(wall=_START_TIME)

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    cfg = Config(
        inference_backend="fake",
        media_tracking_enabled=True,
        media_stale_days=7,
        briefing_max_items=5,
    )
    ingest = MediaIngest(bus, cfg, gm, store, clock=clock)
    await ingest.initialize()

    # Dynamic mock player properties
    current_mpris: list[dict[str, Any]] = []
    current_props: dict[str, Any] = {"data": [{}]}

    async def mock_run_busctl(*args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
        if "list" in args:
            return 0, json.dumps(current_mpris)
        if "GetAll" in args:
            return 0, json.dumps(current_props)
        return -1, ""

    ingest._run_busctl = mock_run_busctl  # type: ignore[method-assign]

    try:
        # =====================================================================
        # DAY 1 (Monday 18:00): Series 1 - Example Anime S03E01
        # =====================================================================
        current_mpris = [{"name": "org.mpris.MediaPlayer2.brave.instance1", "pid": 101}]
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 720000000},  # 720s (12 min)
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
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

        facts = await ingest.poll_tick()
        assert facts == 1
        await store.flush()

        # Check Day 1 briefing candidates
        cands = await _media_continuity_candidates(gm, store, now=clock.now())
        assert len(cands) == 1
        assert cands[0].anchor == "series:example-anime"
        assert cands[0].text == "You were on episode 1 of season 3 of Example Anime."

        # User pauses playback after 20 minutes
        await clock.advance(1200.0)
        current_props["data"][0]["PlaybackStatus"]["data"] = "Paused"
        await ingest.poll_tick()
        await store.flush()

        # =====================================================================
        # DAY 2 (Tuesday 20:00, +26 hours): Series 1 advances to S03E02,
        # followed by Series 2 - Severance S01E01
        # =====================================================================
        await clock.advance(26 * 3600.0)

        # Example Anime S03E02
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 1200000000},  # 1200s (20 min)
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": ("Example Anime 3 Episode 2 Watch All Episodes at Hianime"),
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }
        await ingest.poll_tick()
        await store.flush()

        # Example Anime fact supersedes episode 1 with episode 2
        cands = await _media_continuity_candidates(gm, store, now=clock.now())
        assert len(cands) == 1
        assert cands[0].text == "You were on episode 2 of season 3 of Example Anime."

        # Now start Series 2: Severance Season 1 Episode 1
        await clock.advance(1800.0)
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 1500000000},  # 1500s (25 min)
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": "Severance S01E01 Good News About Hell",
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }
        await ingest.poll_tick()
        await store.flush()

        # Both series active now
        cands = await _media_continuity_candidates(gm, store, now=clock.now())
        cand_map = {c.anchor: c.text for c in cands}
        assert len(cand_map) == 2
        assert cand_map["series:example-anime"] == (
            "You were on episode 2 of season 3 of Example Anime."
        )
        assert cand_map["series:severance"] == "You were on episode 1 of season 1 of Severance."

        # =====================================================================
        # DAY 4 (Thursday 21:00, +48 hours): Series 3 - Breaking Bad S02E07
        # Interleaved with noise (GitHub tab playing audio notification)
        # =====================================================================
        await clock.advance(48 * 3600.0)

        # Noise tab
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 5000000},
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": (
                                    "GitHub - bhanot-99/NeuroPACA: A local cognitive architecture"
                                ),
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }
        facts = await ingest.poll_tick()
        assert facts == 0  # Noise dropped completely!
        assert ingest._titles_dropped >= 1

        # Series 3: Breaking Bad S02E07
        await clock.advance(300.0)
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 2100000000},  # 2100s (35 min)
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": "Breaking Bad Season 2 Episode 7 Negro y Azul",
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }
        await ingest.poll_tick()
        await store.flush()

        # 3 real series tracked
        cands = await _media_continuity_candidates(gm, store, now=clock.now())
        cand_map = {c.anchor: c.text for c in cands}
        assert len(cand_map) == 3
        assert cand_map["series:breaking-bad"] == (
            "You were on episode 7 of season 2 of Breaking Bad."
        )

        # =====================================================================
        # DAY 5 (Friday 22:00, +24 hours): Series 2 advances to S01E02
        # =====================================================================
        await clock.advance(24 * 3600.0)
        current_props = {
            "data": [
                {
                    "PlaybackStatus": {"type": "s", "data": "Playing"},
                    "Position": {"type": "x", "data": 1800000000},
                    "Metadata": {
                        "type": "a{sv}",
                        "data": {
                            "xesam:title": {
                                "type": "s",
                                "data": "Severance S01E02 Half Loop",
                            },
                            "xesam:artist": {"type": "as", "data": [""]},
                            "xesam:album": {"type": "s", "data": ""},
                        },
                    },
                }
            ]
        }
        await ingest.poll_tick()
        await store.flush()

        cands = await _media_continuity_candidates(gm, store, now=clock.now())
        cand_map = {c.anchor: c.text for c in cands}
        assert len(cand_map) == 3
        assert cand_map["series:severance"] == "You were on episode 2 of season 1 of Severance."

        # Verify compose_briefing produces grounded moment containing "where you stopped"
        # Seed focus history with Severance
        moment = await compose_briefing(
            gm,
            store,
            focus_history=[("series:severance", clock.now())],
            now=clock.now(),
            last_briefing_seq=0,
            config=cfg,
        )
        assert moment is not None
        assert "series:severance" in moment.evidence
        assert "You were on episode 2 of season 1 of Severance." in moment.text

        # =====================================================================
        # DAY 10 (Next Thursday, +5 days): Example Anime is now > 7 days old
        # It must naturally fade out (media_stale_days = 7).
        # Severance and Breaking Bad were active on Days 4 & 5 (within 5-6 days).
        # =====================================================================
        await clock.advance(5 * 24 * 3600.0)

        cands = await _media_continuity_candidates(gm, store, now=clock.now(), stale_days=7)
        active_anchors = {c.anchor for c in cands}
        # Example Anime (Day 2, 8 days ago) is stale and faded
        assert "series:example-anime" not in active_anchors
        # Breaking Bad (Day 4, 6 days ago) and Severance (Day 5, 5 days ago) remain active
        assert "series:breaking-bad" in active_anchors
        assert "series:severance" in active_anchors

    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


# gen-ref: 1676b7c1
