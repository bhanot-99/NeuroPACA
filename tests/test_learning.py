"""B4 · Learning (L4) — Insight, extractive prompts, BitNetPlasticity (D-11).

The model is `FakeInferenceBackend` throughout (rules.md §8): with the insight
grammar in play it returns the deterministic extractive JSON
`{"cited_node_id": "n1", "insight_category": "anomaly"}`. No real model, no clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import LlamaCppBackend
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.insight import INSIGHT_CATEGORIES, Insight
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.learning.prompts import (
    alias_nodes,
    build_insight_grammar,
    build_insight_prompt,
    parse_insight,
)
from neuropaca.sensing.snapshot import MetricSnapshot

_SNAP = MetricSnapshot(collector_name="system", timestamp=datetime(2026, 1, 1, tzinfo=UTC), data={})


# --------------------------------------------------------------------- Insight


def test_insight_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="category"):
        Insight("nonsense", ("file:/a",), SignalType.HIGH_LOAD, 0.8, 1)


def test_insight_summary_is_a_template_not_model_text() -> None:
    ins = Insight("anomaly", ("app:webpack",), SignalType.HIGH_LOAD, 0.9, 2)
    assert ins.summary == "anomaly: high_load implicates app:webpack"
    assert ins.traces_to_evidence() is True
    assert Insight("routine", (), SignalType.IDLE, 0.9, 0).traces_to_evidence() is False


# ---------------------------------------------------------------- prompts/GBNF


def test_grammar_splices_exactly_the_prompt_aliases() -> None:
    g = build_insight_grammar(["n1", "n2", "n3"])
    assert 'alias ::= "\\"n1\\"" | "\\"n2\\"" | "\\"n3\\""' in g
    assert "routine" in g and "anomaly" in g and "distraction" in g
    assert "null" in g  # the abstain path (rules.md §4.1)
    assert "n4" not in g  # only this prompt's aliases


def test_grammar_rejects_raw_ids_and_dupes() -> None:
    with pytest.raises(ValueError):
        build_insight_grammar(["file:/a.py"])
    with pytest.raises(ValueError):
        build_insight_grammar(["n1", "n1"])
    with pytest.raises(ValueError):
        build_insight_grammar([])


async def test_prompt_puts_the_signal_last(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    await gm.upsert_node("app:x", NodeType.APP, {"label": "x"})
    node = gm.get_node("app:x")
    assert node is not None
    prompt = build_insight_prompt(SignalType.HIGH_LOAD, 0.82, alias_nodes([node]))
    signal_line = f"Signal: {SignalType.HIGH_LOAD} (conf 0.82)"
    assert prompt.index("[n1] x") < prompt.rindex(signal_line)  # facts before the signal
    assert prompt.rstrip().endswith("Answer:")


def test_parse_accepts_valid_and_discards_everything_else() -> None:
    a2i = {"n1": "file:/a", "n2": "file:/b"}
    kw = {"source_signal": SignalType.HIGH_LOAD, "confidence": 0.8, "snapshot_count": 1}

    good = parse_insight('{"cited_node_id": "n2", "insight_category": "anomaly"}', a2i, **kw)
    assert good is not None
    assert good.cited_node_ids == ("file:/b",)
    assert good.category == "anomaly"

    def rejected(raw: str) -> bool:
        return parse_insight(raw, a2i, **kw) is None

    assert rejected('{"cited_node_id": null, "insight_category": "routine"}')  # abstain
    assert rejected('{"cited_node_id": "n9", "insight_category": "anomaly"}')  # unknown alias
    assert rejected('{"cited_node_id": "n1", "insight_category": "weird"}')  # bad category
    assert rejected("not json at all")
    assert rejected('{"cited_node_id": "n1"}')  # missing key


# --------------------------------------------------------------- BitNetPlasticity


def _collect(sink: list[Event]) -> Callable[[Event], object]:
    async def handler(event: Event) -> None:
        sink.append(event)

    return handler


def _signal(*, conf: float = 0.85, nodes: tuple[str, ...] = ("file:/a.py", "file:/b.py")) -> Signal:
    return Signal(
        signal_type=SignalType.HIGH_LOAD,
        confidence=conf,
        related_node_ids=nodes,
        source_snapshots=(_SNAP,),
        reason="test",
    )


def _event(signal: Signal) -> Event:
    return Event(
        event_type=EventType.SIGNAL_CORRELATED, source="diagnosis", payload={"signal": signal}
    )


async def _wired(tmp_path, **cfg: object) -> tuple[BitNetPlasticity, EventBus, GraphMemory]:
    from neuropaca.core.bitnet_runtime import BitNetRuntime

    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    for nid in ("file:/a.py", "file:/b.py"):
        await gm.upsert_node(nid, NodeType.FILE, {"label": nid.rsplit("/", 1)[-1]})
    await gm.add_edge("file:/a.py", "file:/b.py", RelationType.RELATED_TO)  # for the Hebbian test
    module = BitNetPlasticity(
        bus, Config(inference_backend="fake", **cfg), gm, BitNetRuntime.get_instance()
    )
    await module.initialize()
    await module.start()
    return module, bus, gm


async def test_signal_becomes_an_insight_node_and_event(tmp_path) -> None:
    module, bus, gm = await _wired(tmp_path)
    seen: list[Event] = []
    bus.subscribe(EventType.INSIGHT_GENERATED, _collect(seen))

    await module.on_signal_event(_event(_signal()))
    await bus.join()

    assert module._generated == 1
    insight = seen[0].payload["insight"]
    assert isinstance(insight, Insight)
    assert insight.category in INSIGHT_CATEGORIES
    assert insight.cited_node_ids == ("file:/a.py",)  # n1 = first related node
    assert insight.node_id.startswith("insight:")

    stored = gm.get_node(insight.node_id)
    assert stored is not None and stored.node_type is NodeType.INSIGHT
    targets = {e.target_id for e in gm.get_edges(insight.node_id)}
    assert "file:/a.py" in targets
    assert insight.traces_to_evidence()

    await module.stop()
    await bus.stop()


async def test_low_confidence_is_gated(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    await module.on_signal_event(_event(_signal(conf=0.55)))
    await bus.join()
    assert module._generated == 0
    assert module._dropped == 1
    await module.stop()
    await bus.stop()


async def test_signal_with_no_nodes_is_gated(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    await module.on_signal_event(_event(_signal(nodes=())))
    await bus.join()
    assert module._generated == 0 and module._dropped == 1
    await module.stop()
    await bus.stop()


async def test_is_busy_is_gated(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    module._runtime._busy = True
    await module.on_signal_event(_event(_signal()))
    await bus.join()
    assert module._generated == 0 and module._dropped == 1
    module._runtime._busy = False
    await module.stop()
    await bus.stop()


async def test_repeated_signal_is_dropped_by_jaccard_novelty(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    for _ in range(3):
        await module.on_signal_event(_event(_signal()))  # identical node set -> Jaccard 1.0
        await bus.join()
    assert module._generated == 1
    assert module._dropped == 2
    await module.stop()
    await bus.stop()


async def test_hebbian_bump_on_co_occurring_edge(tmp_path) -> None:
    module, bus, gm = await _wired(tmp_path)
    before = next(e for e in gm.get_edges("file:/a.py") if e.target_id == "file:/b.py").weight
    await module.on_signal_event(_event(_signal()))
    await bus.join()
    # cited = file:/a.py ; other related = file:/b.py ; the a<->b edge exists
    after = next(e for e in gm.get_edges("file:/a.py") if e.target_id == "file:/b.py").weight
    assert after == pytest.approx(before + 0.01)
    await module.stop()
    await bus.stop()


async def test_handler_never_raises_publishes_system_error(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    errors: list[Event] = []
    bus.subscribe(EventType.SYSTEM_ERROR, _collect(errors))

    async def boom(_signal: Signal) -> None:
        raise RuntimeError("kaboom")

    module._handle = boom  # type: ignore[method-assign]
    await module.on_signal_event(_event(_signal()))
    await bus.join()
    assert module._errors == 1
    assert errors and errors[0].payload["module"] == "learning"
    await module.stop()
    await bus.stop()


async def test_gating_drops_at_least_half_under_a_storm(tmp_path) -> None:
    module, bus, _gm = await _wired(tmp_path)
    total = 40
    for i in range(total):
        sig = _signal(conf=0.4) if i % 2 else _signal(conf=0.9)
        await module.on_signal_event(_event(sig))
    await bus.join()
    assert module._dropped / total >= 0.5
    assert module._generated <= 1  # novelty collapses the high-conf repeats
    await module.stop()
    await bus.stop()


# ------------------------------------------------------------ LlamaCppBackend


def test_llama_backend_degrades_gracefully_without_the_wheel() -> None:
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("llama-cpp-python is installed — this test covers the absent case")

    backend = LlamaCppBackend("/no/such/model.gguf", n_threads=2, n_ctx=512)
    backend.load()
    assert backend.is_loaded is False
    assert backend.unavailable_reason is not None
    out = backend.infer("prompt", 48, 0.0, 'root ::= "x"')
    assert '"cited_node_id": null' in out  # the graceful abstain
