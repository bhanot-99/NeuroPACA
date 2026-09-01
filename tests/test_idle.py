"""B6 · Idle Cognition (L6) — DefaultModeNetwork, graph consolidation / pruning,
the proactive idle-thought grammar, and L9 surfacing (D-13).

No test loads a real model (`FakeInferenceBackend`, rules.md §8). No test sleeps
on a poll interval — the one wall-clock-budget test drives `asyncio.timeout`
directly with a zero budget.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("neuropaca.idle.dmn")

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.context import build_aliased_context, build_context_from_nodes, format_node_line
from neuropaca.core.enums import EventType, NodeType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import HUB_NODE_IDS, GraphMemory
from neuropaca.core.models import Event, Node
from neuropaca.idle.dmn import DefaultModeNetwork
from neuropaca.interface.layer import InterfaceLayer
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    PROACTIVE_TEMPLATES,
    build_proactive_grammar,
    parse_proactive,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _node(
    node_id: str, label: str, *, score: float = 1.0, node_type: NodeType = NodeType.APP
) -> Node:
    return Node(id=node_id, node_type=node_type, label=label, relevance_score=score)


# ============================================================ 1 · context (A8)


def test_shared_serialiser_is_one_format() -> None:
    n = _node("app:code", "Visual Studio Code", score=8.1)
    assert format_node_line("app:code", n) == "[app:code] Visual Studio Code · app · score 8.1"
    assert build_context_from_nodes([n]) == format_node_line("app:code", n)
    assert build_aliased_context([("n1", n)]) == "  [n1] Visual Studio Code · app · score 8.1"


def test_prompts_context_block_delegates_to_the_shared_serialiser() -> None:
    from neuropaca.learning.prompts import _context_block

    n = _node("file:/x", "/src/api.py", score=7.0, node_type=NodeType.FILE)
    assert _context_block([("n1", n)]) == build_aliased_context([("n1", n)])


# ==================================================== 2 · proactive grammar/parse


def test_proactive_grammar_splices_exactly_this_prompt_aliases() -> None:
    g = build_proactive_grammar(["n1", "n2", "n3"])
    assert 'alias ::= "\\"n1\\"" | "\\"n2\\"" | "\\"n3\\""' in g
    assert "how_does_x_affect_y" in g and "why_is_x_active" in g
    assert "null" in g  # the single-subject templates
    assert "n4" not in g
    assert 'ws ::= " "?' in g  # tight whitespace (B5 finding, carried)


def test_proactive_grammar_rejects_non_aliases() -> None:
    with pytest.raises(ValueError, match="alias"):
        build_proactive_grammar(["file:/x"])
    with pytest.raises(ValueError, match="at least one"):
        build_proactive_grammar([])


def test_parse_proactive_renders_a_relational_question() -> None:
    a2i = {"n1": "app:esbuild", "n2": "file:/api"}
    a2l = {"n1": "esbuild-service", "n2": "~/src/api"}
    ins = parse_proactive(
        '{"subject": "n1", "object": "n2", "query_template": "how_does_x_affect_y"}', a2i, a2l
    )
    assert ins is not None
    assert ins.category == "proactive"
    assert ins.detail == "How does esbuild-service affect ~/src/api?"
    assert ins.cited_node_ids == ("app:esbuild", "file:/api")
    assert ins.summary == ins.detail  # summary is the question, not a template
    assert ins.traces_to_evidence()  # grounded by construction (D-13)
    assert ins.confidence >= 0.75  # clears the L9 surfacing gate


def test_parse_proactive_single_subject_template_drops_a_spurious_object() -> None:
    a2i = {"n1": "app:code", "n2": "app:brave"}
    a2l = {"n1": "VS Code", "n2": "Brave"}
    ins = parse_proactive(
        '{"subject": "n1", "object": "n2", "query_template": "what_changed_in_x"}', a2i, a2l
    )
    assert ins is not None
    assert ins.detail == "What changed in VS Code recently?"
    assert ins.cited_node_ids == ("app:code",)


@pytest.mark.parametrize(
    "raw",
    [
        # bad alias / bad template / template needs an object / object == subject / not json
        '{"subject": "n9", "object": null, "query_template": "what_changed_in_x"}',
        '{"subject": "n1", "object": null, "query_template": "made_up"}',
        '{"subject": "n1", "object": null, "query_template": "how_does_x_affect_y"}',
        '{"subject": "n1", "object": "n1", "query_template": "how_does_x_affect_y"}',
        "not json at all",
    ],
)
def test_parse_proactive_hard_gate_discards_bad_output(raw: str) -> None:
    a2i = {"n1": "app:code"}
    a2l = {"n1": "VS Code"}
    assert parse_proactive(raw, a2i, a2l) is None


def test_every_template_renders_without_a_keyerror() -> None:
    for tmpl in PROACTIVE_TEMPLATES.values():
        assert tmpl.format(x="X", y="Y")


# ============================================ 2 · graph consolidation / pruning


async def _graph(tmp_path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    return gm


async def test_consolidate_shrinks_a_duplicate_heavy_fixture(tmp_path) -> None:
    gm = await _graph(tmp_path)
    # 40 exact duplicates of one label + 10 unique — the B6 exit fixture.
    for i in range(40):
        await gm.add_node(f"dup{i}", NodeType.APP, {"label": "Slack", "access_count": 1})
    for i in range(10):
        await gm.add_node(f"u{i}", NodeType.FILE, {"label": f"/src/{i}.py"})
    before = gm.node_count

    merged = await gm.consolidate()

    assert merged == 39  # 40 -> 1
    assert gm.node_count == before - 39
    survivors = [n for n in (gm.get_node(f"dup{i}") for i in range(40)) if n is not None]
    assert len(survivors) == 1
    assert survivors[0].access_count == 40  # summed


async def test_merge_math_keeps_oldest_created_at_and_rewires_edges(tmp_path) -> None:
    gm = await _graph(tmp_path)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 8, 1, tzinfo=UTC)
    await gm.add_node("a", NodeType.APP, {"label": "Zoom", "created_at": old, "relevance_score": 2})
    await gm.add_node("b", NodeType.APP, {"label": "zoom", "created_at": new, "relevance_score": 4})
    await gm.add_node("peer", NodeType.FILE, {"label": "/notes"})
    await gm.add_edge("b", "peer", RelationType.RELATED_TO, weight=0.5)

    merged = await gm.consolidate()

    assert merged == 1
    survivor = gm.get_node("a")  # older created_at wins
    assert survivor is not None
    assert survivor.created_at == old
    assert survivor.relevance_score == 3.0  # (2.0 + 4.0) / 2
    assert gm.get_node("b") is None
    assert {e.target_id for e in gm.get_edges("a")} == {"peer"}  # b's edge rewired to a


async def test_consolidate_never_touches_the_routing_hubs(tmp_path) -> None:
    gm = await _graph(tmp_path)
    # A node whose label collides with a hub's, same type — must NOT merge the hub.
    await gm.add_node("mine", NodeType.CONCEPT, {"label": "YOU"})
    merged = await gm.consolidate()
    assert merged == 0
    assert gm.get_node("YOU") is not None
    assert gm.get_node("mine") is not None


async def test_link_orphan_nodes_attaches_to_you_and_terminates(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("lonely", NodeType.FILE, {"label": "/tmp/x"})
    await gm.add_node("connected", NodeType.APP, {"label": "code"})
    await gm.add_edge("connected", "domain:engineering", RelationType.PART_OF)

    linked = await gm.link_orphan_nodes()

    assert linked == 1
    assert {e.target_id for e in gm.get_edges("lonely")} == {"YOU"}
    assert await gm.link_orphan_nodes() == 0  # idempotent — no orphans left


async def test_prune_stale_nodes_drops_expired_and_zero_score_but_spares_fresh(tmp_path) -> None:
    gm = await _graph(tmp_path)
    ttl = timedelta(hours=48)
    stale = datetime.now(UTC) - timedelta(hours=72)

    await gm.add_node("fresh", NodeType.FILE, {"label": "/new", "relevance_score": 0.0})
    await gm.add_node(
        "decayed",
        NodeType.FILE,
        {"label": "/old", "relevance_score": 0.0, "created_at": stale, "last_accessed": stale},
    )
    await gm.add_node(
        "expired_thought",
        NodeType.IDLE_THOUGHT,
        {
            "label": "How does X affect Y?",
            "relevance_score": 9.0,
            "created_at": stale,
            "last_accessed": stale,
        },
    )

    pruned = await gm.prune_stale_nodes(ttl)

    assert pruned == 2
    assert gm.get_node("fresh") is not None  # scoring may not have run — spared
    assert gm.get_node("decayed") is None  # old + score 0
    assert gm.get_node("expired_thought") is None  # past the 48 h idle-thought TTL
    for hub in HUB_NODE_IDS:
        assert gm.get_node(hub) is not None


# ============================================================ 3 · the DMN cycle


async def _dmn(tmp_path, clock=None, **cfg):
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    runtime = BitNetRuntime.get_instance()
    dmn = DefaultModeNetwork(
        bus, Config(inference_backend="fake", **cfg), gm, runtime, clock=clock or FakeClock()
    )
    await dmn.initialize()
    await dmn.start()
    return dmn, bus, gm


async def test_idle_detected_runs_one_cycle_and_publishes_a_thought(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path)
    for i in range(4):
        await gm.add_node(
            f"app:{i}", NodeType.APP, {"label": f"service {i}", "relevance_score": float(9 - i)}
        )
    seen: list[Event] = []
    bus.subscribe(EventType.INSIGHT_GENERATED, _collect(seen))

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await dmn._idle_task
    await bus.join()

    assert dmn._cycles == 1
    assert dmn._thoughts >= 1
    thought_nodes = [nid for nid in gm.node_ids if nid.startswith("idle:")]
    assert len(thought_nodes) == dmn._thoughts
    node = gm.get_node(thought_nodes[0])
    assert node is not None and node.node_type is NodeType.IDLE_THOUGHT
    assert seen and isinstance(seen[0].payload["insight"], Insight)
    assert seen[0].payload["insight"].category == "proactive"
    await bus.stop()


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def test_cycle_respects_the_inference_budget(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path, dmn_max_inferences_per_cycle=2, dmn_top_k=5)
    for i in range(5):
        await gm.add_node(
            f"app:{i}", NodeType.APP, {"label": f"svc{i}", "relevance_score": float(9 - i)}
        )

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await dmn._idle_task
    await bus.join()

    assert dmn._thoughts == 2  # capped, not 5
    assert len([nid for nid in gm.node_ids if nid.startswith("idle:")]) == 2
    await bus.stop()


async def test_activity_cancels_the_cycle_within_one_tick_without_corruption(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path)
    # A duplicate-heavy graph so consolidate() is mid-flight (it yields between merges).
    for i in range(60):
        await gm.add_node(f"d{i}", NodeType.APP, {"label": "dup"})

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await asyncio.sleep(0)  # let the cycle start
    await dmn.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
    assert dmn._cancels == 1

    results = await asyncio.gather(dmn._idle_task, return_exceptions=True)
    assert (
        dmn._idle_task.cancelled()
        or isinstance(results[0], asyncio.CancelledError)
        or dmn._idle_task.done()
    )
    # graph consistent: hubs intact, no node half-merged into nothing
    for hub in HUB_NODE_IDS:
        assert gm.get_node(hub) is not None
    remaining = [nid for nid in gm.node_ids if nid.startswith("d")]
    assert all(gm.get_node(nid) is not None for nid in remaining)
    await bus.stop()


async def test_cycle_abandons_cleanly_when_it_blows_the_wall_clock_budget(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path)
    for i in range(3):
        await gm.add_node(f"app:{i}", NodeType.APP, {"label": f"s{i}", "relevance_score": 1.0})
    # Force an already-expired budget: asyncio.timeout(0) fires at the first await.
    object.__setattr__(dmn.config, "dmn_cycle_wall_clock_seconds", 0)

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await asyncio.gather(dmn._idle_task, return_exceptions=True)
    await bus.join()

    assert dmn._timeouts == 1
    assert dmn._errors == 0  # a budget overrun is not an error
    assert dmn._cycles == 1
    await bus.stop()


async def test_dmn_does_nothing_when_fewer_than_two_seed_nodes(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path)
    await gm.add_node("app:only", NodeType.APP, {"label": "solo", "relevance_score": 5.0})

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await dmn._idle_task
    await bus.join()

    assert dmn._cycles == 1
    assert dmn._thoughts == 0
    assert not [nid for nid in gm.node_ids if nid.startswith("idle:")]
    await bus.stop()


async def test_stop_cancels_an_in_flight_cycle(tmp_path) -> None:
    dmn, bus, gm = await _dmn(tmp_path)
    for i in range(60):
        await gm.add_node(f"d{i}", NodeType.APP, {"label": "dup"})
    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await asyncio.sleep(0)

    await dmn.stop()  # must not hang, must not raise

    assert dmn._idle_task is None
    await bus.stop()


# ============================================================ 4 · L9 surfacing


async def _interface(tmp_path):
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    layer = InterfaceLayer(
        bus,
        Config(inference_backend="fake"),
        gm,
        BitNetRuntime.get_instance(),
        clock=FakeClock(wall=_NOW),
        socket_path=str(tmp_path / "np.sock"),
    )
    await layer.initialize()
    await layer.start()
    return layer, bus, gm


def _thought(node_id: str, text: str) -> Insight:
    return Insight(
        category="proactive",
        cited_node_ids=("app:esbuild",),
        source_signal=SignalType.IDLE,
        confidence=0.8,
        snapshot_count=0,
        node_id=node_id,
        detail=text,
    )


async def test_proactive_thought_surfaces_once_and_survives_restart(tmp_path) -> None:
    layer, bus, gm = await _interface(tmp_path)
    text = "How does esbuild affect api?"
    await gm.add_node("idle:abc123", NodeType.IDLE_THOUGHT, {"label": text})
    thought = _thought("idle:abc123", text)

    await layer.on_insight_generated(
        Event(event_type=EventType.INSIGHT_GENERATED, payload={"insight": thought})
    )
    await bus.join()

    drained = await layer._route({"op": "insights"})
    assert drained["ok"]
    assert drained["insights"][0]["text"] == "How does esbuild affect api?"
    assert drained["insights"][0]["category"] == "proactive"

    node = gm.get_node("idle:abc123")
    assert node is not None and node.surfaced_at is not None  # stamped, schema v2
    assert node.node_type is NodeType.IDLE_THOUGHT  # upsert kept the real type

    await layer.stop()

    # a fresh InterfaceLayer over the same graph must treat it as already seen
    layer2, bus2, _gm = await _interface(tmp_path)
    assert "idle:abc123" in layer2._surfaced_ids
    await layer2.on_insight_generated(
        Event(event_type=EventType.INSIGHT_GENERATED, payload={"insight": thought})
    )
    again = await layer2._route({"op": "insights"})
    assert again["insights"] == []  # surface-once held across the restart
    await layer2.stop()
    await bus2.stop()
