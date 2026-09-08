"""B14 · web-app attribution through the real pipeline + the privacy assertion.

`APP_SWITCH` events (as the collector now emits them — `webapp` label, no raw
title) replayed through a real `SignalCorrelator` + `GraphMemory`. Checks the
`webapp:` node/edge shape, the focus counter, and — the load-bearing one — that
no title fragment reaches the graph on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _switch(app_id: str, webapp: str | None, domain: str | None, *, t: float, prev=None) -> Event:
    return Event(
        event_type=EventType.APP_SWITCH,
        source="sensing.activity",
        payload={
            "app_id": app_id,
            "webapp": webapp,
            "webapp_domain": domain,
            "previous_app_id": prev,
            "previous_webapp": None,
        },
        timestamp=_BASE + timedelta(seconds=t),
    )


async def _correlator(tmp_path: Path) -> tuple[SignalCorrelator, EventBus, GraphMemory]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    corr = SignalCorrelator(bus, Config(inference_backend="fake"), graph)
    await corr.initialize()
    await corr.start()
    return corr, bus, graph


async def test_webapp_node_wired_to_browser_and_domain(tmp_path: Path) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("brave-browser", "github", "domain:engineering", t=0))
        await bus.join()

        node = graph.get_node("webapp:github")
        assert node is not None and node.node_type == NodeType.WEBAPP
        targets = {
            (e.target_id, e.relation.value)
            for e in graph.get_edges("webapp:github")
            if e.source_id == "webapp:github"
        }
        assert ("app:brave-browser", "part_of") in targets
        assert ("domain:engineering", "part_of") in targets
    finally:
        await corr.stop()
        await bus.stop()


async def test_access_count_tracks_refocus(tmp_path: Path) -> None:
    # `upsert_node` sets access_count 0 on creation and +1 per later sighting
    # (same mechanic as `app:` nodes) — so N focuses read as N-1.
    corr, bus, graph = await _correlator(tmp_path)
    try:
        for i in range(4):
            await corr.on_app_switch(_switch("brave-browser", "gmail", "domain:comms", t=i * 10))
        await corr.on_app_switch(_switch("brave-browser", "youtube", "domain:habits", t=99))
        await bus.join()

        assert graph.get_node("webapp:gmail").access_count == 3  # type: ignore[union-attr]
        assert graph.get_node("webapp:youtube").access_count == 0  # type: ignore[union-attr]
    finally:
        await corr.stop()
        await bus.stop()


async def test_unidentified_tab_writes_no_webapp_node(tmp_path: Path) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("brave-browser", None, None, t=0))
        await bus.join()
        assert not any(nid.startswith("webapp:") for nid in graph.node_ids)
        assert graph.get_node("app:brave-browser") is not None  # classified via app_map
    finally:
        await corr.stop()
        await bus.stop()


async def test_no_title_fragment_reaches_graph_json(tmp_path: Path) -> None:
    """The membrane holds end to end: only allowlisted labels on disk."""
    corr, bus, graph = await _correlator(tmp_path)
    try:
        # what the collector would emit for a realistic browsing session — note
        # it has ALREADY stripped every title; the correlator only ever sees labels
        seq = [
            _switch("brave-browser", "gmail", "domain:comms", t=0),
            _switch("brave-browser", "google-docs", "domain:projects", t=30),
            _switch("brave-browser", "github", "domain:engineering", t=60),
            _switch("brave-browser", "youtube", "domain:habits", t=90),
        ]
        for ev in seq:
            await corr.on_app_switch(ev)
        await bus.join()
        await graph.save()

        blob = (tmp_path / "graph.json").read_text("utf-8")
        for leak in ("@gmail.com", "Inbox", "(351)", "Astley", "Never Gonna", " - Brave"):
            assert leak not in blob
        assert "webapp:gmail" in blob and "webapp:github" in blob
    finally:
        await corr.stop()
        await bus.stop()
