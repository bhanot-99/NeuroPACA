# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B18 · one labeling system — labels are rendered, never stored.

Each test pins one guarantee from RESEARCH_DOSSIER.md §21.7:

- identity is the facts (fingerprint ignores `value`, thought refs unordered);
- a rendered name never contains a raw node id;
- the same fact written twice — concurrently, or across a restart — is one node;
- issue 1: a B17 rename heals every label that points at the renamed app;
- issue 2: L4 does not re-ask the model about a fact it already stored, even
  after a daemon restart (the in-memory Jaccard buffer did not survive one);
- issue 3: probes on different apps render differently; real collisions are
  disambiguated;
- issue 4: no label quotes another label (migration drops the ones that did,
  DMN never seeds on L8 probes, the pressure reason names a cause only).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from neuropaca.agents.supervisor import AgentSupervisor, _cause
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.labels import (
    LabelKind,
    LabelSpec,
    disambiguate,
    fact_id,
    fingerprint,
    render,
)
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.sensing.snapshot import MetricSnapshot

_SNAP = MetricSnapshot(collector_name="system", timestamp=datetime(2026, 1, 1, tzinfo=UTC), data={})


def _insight(ref: str, value: float | None = None) -> LabelSpec:
    return LabelSpec(LabelKind.INSIGHT, (ref,), "anomaly/idle", value)


async def _graph(tmp_path: Path, name: str = "g.json") -> GraphMemory:
    gm = GraphMemory(tmp_path / name)
    await gm.load()
    return gm


# ------------------------------------------------------------------ pure core
def test_fingerprint_is_the_facts_not_the_numbers() -> None:
    assert fingerprint(_insight("app:brave", 0.7)) == fingerprint(_insight("app:brave", 0.9))
    assert fingerprint(_insight("app:brave")) != fingerprint(_insight("app:code"))
    a_b = LabelSpec(LabelKind.THOUGHT, ("app:a", "app:b"), "how_does_x_affect_y")
    b_a = LabelSpec(LabelKind.THOUGHT, ("app:b", "app:a"), "how_does_x_affect_y")
    assert fact_id(a_b) == fact_id(b_a)  # one open question, whichever way round
    assert fact_id(a_b).startswith("idle:")


def test_a_rendered_name_never_contains_a_raw_id() -> None:
    names = {"app:brave": "Brave"}
    for spec in (
        _insight("app:brave", 0.82),
        LabelSpec(LabelKind.PROBE, ("app:brave",), "summary/L4.anomaly", 1.59),
        LabelSpec(LabelKind.PROBE, ("app:brave",), "source/learning"),
    ):
        for mode in ("short", "full"):
            text = render(spec, names.__getitem__, mode)  # type: ignore[arg-type]
            assert "app:" not in text and "Brave" in text, text


def test_disambiguate_only_touches_real_collisions() -> None:
    caps = {"a": "Brave · pressure 1.4", "b": "Brave · pressure 1.4", "c": "VS Code"}
    out = disambiguate(caps, {"a": "10:05", "b": "11:30"})
    assert out["c"] == "VS Code"
    assert len(set(out.values())) == 3
    assert out["a"].endswith("10:05")


# ------------------------------------------------------------ the write path
async def test_the_same_fact_across_a_restart_is_one_node(tmp_path: Path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave"})
    first, created = await gm.upsert_fact(_insight("app:brave", 0.8))
    assert created
    await gm.save()

    restarted = await _graph(tmp_path)
    again, created_again = await restarted.upsert_fact(_insight("app:brave", 0.9))
    assert not created_again
    assert again.id == first.id
    assert again.access_count == 1  # "seen 2 times"
    assert again.spec is not None and again.spec.value == 0.9  # latest value wins
    assert sum(1 for n in restarted.node_ids if n.startswith("insight:")) == 1


async def test_fifty_concurrent_identical_upserts_make_one_node(tmp_path: Path) -> None:
    gm = await _graph(tmp_path)
    results = await asyncio.gather(*(gm.upsert_fact(_insight("app:x")) for _ in range(50)))
    assert sum(1 for _node, created in results if created) == 1
    node = gm.find_fact(_insight("app:x"))
    assert node is not None and node.access_count == 49


# ------------------------------------------------------------------- issue 1
async def test_a_rename_heals_labels_and_merges_the_facts_it_unites(tmp_path: Path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave"})
    await gm.add_node("app:brave-browser", NodeType.APP, {"label": "brave-browser"})
    await gm.upsert_fact(_insight("app:brave-browser"))
    await gm.upsert_fact(_insight("app:brave"))
    assert sum(1 for n in gm.node_ids if n.startswith("insight:")) == 2

    canon = {"brave-browser": "brave"}
    await gm.canonicalise_app_nodes(lambda b: canon.get(b, b), lambda _b: False)

    facts = [n for n in gm.node_ids if n.startswith("insight:")]
    assert facts == [fact_id(_insight("app:brave"))]
    node = gm.get_node(facts[0])
    assert node is not None
    assert node.label == "Anomaly on Brave (idle)"
    assert node.access_count == 0 + 0  # merge sums counts (two fresh facts)


# ----------------------------------------------- migration (schema v4 -> v5)
def _legacy_graph(path: Path) -> None:
    """The shapes actually found in data/graph.json on 2026-09-10."""
    now = "2026-09-10T10:00:00+00:00"

    def node(nid: str, ntype: str, label: str) -> dict:
        return {
            "id": nid,
            "node_type": ntype,
            "label": label,
            "created_at": now,
            "last_accessed": now,
            "access_count": 1,
            "relevance_score": 1.0,
            "priority": 0,
        }

    nodes = [
        node("app:brave", "app", "brave"),
        node("app:cosmic-term", "app", "cosmic-term"),
        node("insight:aaa", "insight", "anomaly: idle implicates app:brave"),
        node("insight:bbb", "insight", "anomaly: idle implicates app:brave"),  # issue 2
        node("insight:ccc", "insight", "anomaly: idle implicates app:brave-browser"),  # issue 1
        node(
            "ephemeral:summary:1",
            "concept",
            "pressure 1.43 on app:brave: L4 anomaly: anomaly: idle implicates app:brave",
        ),
        node(
            "ephemeral:summary:2",
            "concept",
            "pressure 1.59 on app:brave: L4 anomaly: anomaly: idle implicates app:brave",
        ),
        node("ephemeral:source-learning:1", "concept", "learning corroborated app:brave"),
        node("idle:t1", "idle_thought", "How does brave affect Cosmic Term?"),
        node("idle:t2", "idle_thought", "How does Cosmic Term affect brave?"),
        node(  # issue 4: a thought quoting two other labels
            "idle:t3",
            "idle_thought",
            "How does learning corroborated app:brave affect pressure 1.30 on app:brave: "
            "L3 distraction: 6 app switches in 2 min (3 distinct)?",
        ),
    ]
    edges = [
        {"source": "insight:aaa", "target": "app:brave", "relation": "related_to", "weight": 0.0},
        {"source": "idle:t3", "target": "app:brave", "relation": "related_to", "weight": 0.0},
    ]
    path.write_text(json.dumps({"schema_version": 4, "nodes": nodes, "edges": edges}))


async def test_v4_graph_migrates_to_facts(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    _legacy_graph(path)
    gm = GraphMemory(path)
    await gm.load()

    assert (tmp_path / "graph.json.pre-b18-backup").exists()
    ids = gm.node_ids
    assert not any(n in ids for n in ("idle:t1", "idle:t2", "idle:t3")), "ids re-derived"
    thoughts = [n for n in ids if n.startswith("idle:")]
    assert len(thoughts) == 1, "reversed pair merged, label-quoting thought dropped"
    summaries = [
        n
        for n in ids
        if (node := gm.get_node(n)) and node.spec and node.spec.facet.startswith("summary/")
    ]
    assert len(summaries) == 1, "pressure 1.43 and 1.59 on Brave are one probe"

    # the pre-B17 name is healed by the identity pass the orchestrator runs next
    canon = {"brave-browser": "brave"}
    await gm.canonicalise_app_nodes(lambda b: canon.get(b, b), lambda _b: False)
    insights = [n for n in gm.node_ids if n.startswith("insight:")]
    assert len(insights) == 1, "3 legacy insights about one fact -> 1"

    for nid in gm.node_ids:
        node = gm.get_node(nid)
        assert node is not None
        if node.spec is not None:
            assert "app:" not in node.label, node.label
            assert "corroborated app:" not in gm.display_name(nid)

    await gm.save()
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 8  # saved by the current (V-9) build
    reloaded = GraphMemory(path)
    await reloaded.load()
    assert sorted(reloaded.node_ids) == sorted(gm.node_ids), "v5 round-trips unchanged"


async def test_an_old_graph_with_nothing_to_migrate_leaves_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    now = "2026-09-10T10:00:00+00:00"
    app = {
        "id": "app:brave",
        "node_type": "app",
        "label": "brave",
        "created_at": now,
        "last_accessed": now,
        "access_count": 0,
        "relevance_score": 0.0,
        "priority": 0,
    }
    path.write_text(json.dumps({"schema_version": 4, "nodes": [app], "edges": []}))
    gm = GraphMemory(path)
    await gm.load()
    assert gm.get_node("app:brave") is not None
    assert not (tmp_path / "graph.json.pre-b18-backup").exists()
    assert not gm.dirty


# ------------------------------------------------------------------- issue 2
class _CountingRuntime:
    is_loaded = True
    is_busy = False
    backend_unavailable = False

    def __init__(self) -> None:
        self.calls = 0

    async def load_model_async(self) -> bool:
        return True

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        self.calls += 1
        return '{"cited_node_id": "n1", "insight_category": "anomaly"}'


def _signal() -> Event:
    sig = Signal(
        signal_type=SignalType.IDLE,
        confidence=0.9,
        related_node_ids=("app:brave",),
        source_snapshots=(_SNAP,),
        reason="idle",
    )
    return Event(
        event_type=EventType.SIGNAL_CORRELATED, source="diagnosis", payload={"signal": sig}
    )


async def test_l4_never_re_asks_about_a_known_fact_even_after_restart(tmp_path: Path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave"})
    runtime = _CountingRuntime()
    config = Config(inference_backend="fake")

    first = BitNetPlasticity(bus, config, gm, runtime)  # type: ignore[arg-type]
    await first.on_signal_event(_signal())
    assert first._generated == 1 and runtime.calls == 1
    await gm.save()

    # "restart": a fresh module over a freshly loaded graph
    reloaded = await _graph(tmp_path)
    second = BitNetPlasticity(bus, config, reloaded, runtime)  # type: ignore[arg-type]
    await second.on_signal_event(_signal())
    assert second._drops["repeat"] == 1
    assert runtime.calls == 1, "the model was not asked again"
    assert sum(1 for n in reloaded.node_ids if n.startswith("insight:")) == 1
    await bus.join()
    await bus.stop()


# ------------------------------------------------------------ issue 3 and 4
async def test_probe_repeats_reinforce_one_node_per_facet(tmp_path: Path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave"})
    agents = AgentSupervisor(
        bus,
        Config(
            inference_backend="fake",
            action_log_path=str(tmp_path / "a.jsonl"),
            quarantine_path=str(tmp_path / "q"),
        ),
        gm,
        clock=FakeClock(wall=datetime.now(UTC)),
    )
    a = await agents.spawn_node("summary/L4.anomaly", trigger_node="app:brave", value=1.43)
    b = await agents.spawn_node("summary/L4.anomaly", trigger_node="app:brave", value=1.59)
    c = await agents.spawn_node("source/learning", trigger_node="app:brave")
    assert a == b and a != c
    assert agents._nodes_created == 2
    assert gm.display_name(a or "", "short") == "Brave · pressure 1.6"
    assert gm.display_name(c or "", "short") == "Brave · via learning"
    await bus.stop()


def test_a_pressure_cause_is_a_closed_facet_not_free_text() -> None:
    assert _cause("L4 anomaly (idle)") == "L4.anomaly"
    assert _cause("L3 distraction: 6 app switches in 2 min (3 distinct)") == "L3.distraction"
    assert _cause("something else") == "other"


async def test_dmn_seeds_never_include_l8_probes(tmp_path: Path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave", "relevance_score": 2.0})
    probe, _ = await gm.upsert_fact(LabelSpec(LabelKind.PROBE, ("app:brave",), "source/learning"))
    await gm.update_node(probe.id, {"relevance_score": 9.0})
    top = gm.top_nodes_by_score(5, exclude_prefixes=("ephemeral:",))
    assert [n.id for n in top] == ["app:brave"]


# gen-ref: b18-labels-tests
