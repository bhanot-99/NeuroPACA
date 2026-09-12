# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-8 · insight citation structure is flat (VISION.md §5).

In the live graph an `insight:` node and every `ephemeral:` probe about the same
episode pointed at the *same* `app:` node and at nothing else. `app:brave`
carried one insight and five probes, all siblings, none connected to each other.
So "what did the system look at when it concluded this?" had no answer in the
graph, and a probe was indistinguishable from noise.

The causal link was never missing — it was discarded: L4 stores its insight
*before* publishing it, so `insight.node_id` is real by the time the pressure
accumulator sees the event. V-8 carries it through pressure into L8, which wires
`probe -CAUSED_BY-> insight`.

Deliberately an edge and not part of `spec.refs`: refs are identity, so citing
the insight there would mint a new probe per insight and race the ephemeral cap.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.labels import KIND_PREFIX, LabelKind, LabelSpec
from neuropaca.core.models import Event
from neuropaca.drive.pressure import PressureAccumulator, PressureEntry
from neuropaca.learning.insight import Insight

EPHEMERAL = KIND_PREFIX[LabelKind.PROBE]


async def _graph(tmp_path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


async def _supervisor(tmp_path, gm, **cfg):
    from neuropaca.agents.supervisor import AgentSupervisor

    bus = EventBus.get_instance()
    await bus.start()
    sup = AgentSupervisor(bus, Config(inference_backend="fake", **cfg), gm, clock=FakeClock())
    await sup.initialize()
    await sup.start()
    return sup, bus


def _entry(node_id: str, *, evidence: tuple[str, ...] = (), sources=("diagnosis",)):
    now = datetime.now(UTC)
    return PressureEntry(
        node_id=node_id,
        pressure=1.4,
        reason="L4 anomaly (idle)",
        created_at=now,
        last_updated=now,
        sources=sources,
        evidence=evidence,
    )


# =========================================== 1 · pressure carries the cause by id


async def _accumulator(tmp_path):
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    acc = PressureAccumulator(bus, Config(inference_backend="fake"), gm, clock=FakeClock())
    return acc, bus


async def test_an_l4_insight_contributes_its_node_id_as_evidence(tmp_path) -> None:
    acc, bus = await _accumulator(tmp_path)
    try:
        insight = Insight(
            category="anomaly",
            cited_node_ids=("app:brave",),
            source_signal=SignalType.IDLE,
            confidence=0.9,
            snapshot_count=1,
            node_id="insight:deadbeef",
        )
        await acc.on_insight_event(
            Event(
                event_type=EventType.INSIGHT_GENERATED,
                source="learning",
                payload={"insight": insight},
            )
        )
        assert acc._snapshot("app:brave").evidence == ("insight:deadbeef",)
    finally:
        await bus.stop()


async def test_a_node_is_never_its_own_evidence(tmp_path) -> None:
    acc, bus = await _accumulator(tmp_path)
    try:
        acc.add_pressure("app:brave", 1.0, "L4 anomaly (idle)", evidence="app:brave")
        assert acc._snapshot("app:brave").evidence == ()
    finally:
        await bus.stop()


async def test_evidence_is_deduped_newest_last_and_bounded(tmp_path) -> None:
    from neuropaca.drive.pressure import _MAX_EVIDENCE

    acc, bus = await _accumulator(tmp_path)
    try:
        for i in range(_MAX_EVIDENCE + 3):
            acc.add_pressure("app:x", 0.1, "L4 anomaly (idle)", evidence=f"insight:{i}")
        acc.add_pressure("app:x", 0.1, "L4 anomaly (idle)", evidence="insight:3")

        evidence = acc._snapshot("app:x").evidence
        assert len(evidence) == _MAX_EVIDENCE  # bounded
        assert len(set(evidence)) == _MAX_EVIDENCE  # deduped
        assert evidence[-1] == "insight:3"  # a repeat moves to the back, does not grow
    finally:
        await bus.stop()


async def test_an_l3_signal_carries_no_evidence_because_it_has_no_node(tmp_path) -> None:
    """Only an L4 insight is a *node*; a bare L3 signal has nothing to cite."""
    acc, bus = await _accumulator(tmp_path)
    try:
        acc.add_pressure("app:brave", 1.0, "L3 distraction: tab churn")
        assert acc._snapshot("app:brave").evidence == ()
    finally:
        await bus.stop()


# =================================================== 2 · the citation edge itself


async def test_a_probe_cites_the_insight_that_caused_it(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "Brave"})
    insight, _ = await gm.upsert_fact(
        LabelSpec(LabelKind.INSIGHT, ("app:brave",), "anomaly/idle", 0.9)
    )
    sup, bus = await _supervisor(tmp_path, gm)
    try:
        await sup._grow_subcluster(_entry("app:brave", evidence=(insight.id,)))

        probes = [n for n in gm.node_ids if n.startswith(EPHEMERAL)]
        assert probes
        for probe in probes:
            targets = [
                (e.target_id, e.relation) for e in gm.get_edges(probe) if e.source_id == probe
            ]
            assert (insight.id, RelationType.CAUSED_BY) in targets
    finally:
        await sup.stop()
        await bus.stop()


async def test_the_citations_hang_off_the_insight(tmp_path) -> None:
    """The shape V-8 asks for: ask the insight what backs it."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "Brave"})
    insight, _ = await gm.upsert_fact(
        LabelSpec(LabelKind.INSIGHT, ("app:brave",), "anomaly/idle", 0.9)
    )
    sup, bus = await _supervisor(tmp_path, gm)
    try:
        await sup._grow_subcluster(
            _entry("app:brave", evidence=(insight.id,), sources=("diagnosis", "learning"))
        )

        citations = gm.citations_of(insight.id)
        assert len(citations) >= 2
        assert all(c.id.startswith(EPHEMERAL) for c in citations)
    finally:
        await sup.stop()
        await bus.stop()


async def test_an_uncited_insight_has_no_citations(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    insight, _ = await gm.upsert_fact(LabelSpec(LabelKind.INSIGHT, ("app:brave",), "anomaly/idle"))
    assert gm.citations_of(insight.id) == []
    assert gm.citations_of("app:brave") == []
    assert gm.citations_of("nope") == []


async def test_a_citation_of_a_pruned_cause_is_skipped_not_dangling(tmp_path) -> None:
    """A citation of something gone is worse than no citation — and a dangling
    edge would resurrect the node as a bare networkx placeholder."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    sup, bus = await _supervisor(tmp_path, gm)
    try:
        await sup._grow_subcluster(_entry("app:brave", evidence=("insight:already-pruned",)))

        assert not gm.has_node("insight:already-pruned")
        for probe in [n for n in gm.node_ids if n.startswith(EPHEMERAL)]:
            assert all(e.relation is not RelationType.CAUSED_BY for e in gm.get_edges(probe))
    finally:
        await sup.stop()
        await bus.stop()


# ================================== 3 · identity and score are deliberately untouched


async def test_citing_does_not_mint_a_new_probe_per_insight(tmp_path) -> None:
    """The reason the citation is an edge and not a ref: refs are identity, so
    citing there would race the ephemeral cap with near-duplicate probes."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    causes = []
    for i in range(5):
        node, _ = await gm.upsert_fact(
            LabelSpec(LabelKind.INSIGHT, ("app:brave",), f"anomaly/sig{i}")
        )
        causes.append(node.id)
    sup, bus = await _supervisor(tmp_path, gm)
    try:
        for cause in causes:
            await sup._grow_subcluster(_entry("app:brave", evidence=(cause,)))

        probes = [n for n in gm.node_ids if n.startswith(EPHEMERAL)]
        assert len(probes) == 2  # one summary + one source facet, refreshed 5x
    finally:
        await sup.stop()
        await bus.stop()


async def test_a_citation_edge_lends_no_relevance(tmp_path) -> None:
    """V-2's rule: the system's own notes neither earn nor lend relevance. A
    citation is bookkeeping between two generated nodes, so it must score 0."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    insight, _ = await gm.upsert_fact(LabelSpec(LabelKind.INSIGHT, ("app:brave",), "anomaly/idle"))
    probe, _ = await gm.upsert_fact(
        LabelSpec(LabelKind.PROBE, ("app:brave",), "summary/L4.anomaly")
    )

    before = gm._edge_profile_unsafe(insight.id)[0]
    await gm.add_edge(probe.id, insight.id, RelationType.CAUSED_BY, 0.0)
    after = gm._edge_profile_unsafe(insight.id)[0]

    assert before == after == pytest.approx(0.0)


async def test_citing_survives_a_save_and_load(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    insight, _ = await gm.upsert_fact(LabelSpec(LabelKind.INSIGHT, ("app:brave",), "anomaly/idle"))
    sup, bus = await _supervisor(tmp_path, gm)
    try:
        await sup._grow_subcluster(_entry("app:brave", evidence=(insight.id,)))
        await gm.save()
    finally:
        await sup.stop()
        await bus.stop()

    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await reloaded.load()
    assert reloaded.citations_of(insight.id)


# gen-ref: 759dcb05
