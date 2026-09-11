# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-7 · idle-thought text is not stored (VISION.md §5).

B18 made labels rendered rather than stored, so a rename heals every label at
once — a property worth keeping. But it left no room for two things: a record of
what a thought *actually asked*, in the words used at the time, and any free-text
payload a closed four-template vocabulary cannot express (the briefing will need
one).

`LabelSpec.text` is that room. It is deliberately NOT part of the fingerprint,
so no node id changes and a v6 graph loads untouched; NOT what the label renders
from, so B18's rename-healing is intact; and NOT rewritten when the same
question is asked again, because it is a historical record.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.core.labels import (
    TEXT_MAX,
    LabelKind,
    LabelSpec,
    fact_id,
    fingerprint,
    render,
)

_SPEC = LabelSpec(LabelKind.THOUGHT, ("app:code", "app:brave"), "how_does_x_affect_y")


async def _graph(tmp_path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


# ================================================ 1 · identity is left alone


def test_text_is_not_part_of_a_fact_s_identity() -> None:
    """The whole reason V-7 needs no migration: every id in an existing graph is
    exactly what it was."""
    assert fingerprint(_SPEC) == fingerprint(replace(_SPEC, text="How does X affect Y?"))
    assert fact_id(_SPEC) == fact_id(replace(_SPEC, text="anything at all"))


def test_the_same_question_in_different_words_is_still_one_question() -> None:
    a = replace(_SPEC, text="How does VS Code affect Brave?")
    b = replace(_SPEC, text="What is the effect of VS Code on Brave?")
    assert fact_id(a) == fact_id(b)


def test_the_label_still_renders_from_facet_and_refs(tmp_path) -> None:
    """B18's rule, unchanged: text on the spec must not leak into the label."""
    spec = replace(_SPEC, text="a completely different sentence")
    assert render(spec, lambda ref: ref.removeprefix("app:")) == "How does code affect brave?"


# ====================================================== 2 · storing and reading


def test_a_spec_round_trips_its_text() -> None:
    spec = replace(_SPEC, text="How does VS Code affect Brave?")
    assert LabelSpec.from_record(spec.to_record()) == spec


def test_a_spec_without_text_writes_no_key() -> None:
    """One null per record across thousands of generated nodes is pure weight."""
    assert "text" not in _SPEC.to_record()
    assert LabelSpec.from_record(_SPEC.to_record()).text is None


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_blank_text_normalises_to_none(raw: str) -> None:
    assert replace(_SPEC, text=raw).text is None


def test_whitespace_is_collapsed() -> None:
    assert replace(_SPEC, text="  How  does\n X\taffect Y? ").text == "How does X affect Y?"


def test_text_is_clipped_so_one_node_cannot_bloat_the_graph_file() -> None:
    spec = replace(_SPEC, text="x" * (TEXT_MAX * 4))
    assert len(spec.text) == TEXT_MAX


async def test_text_survives_a_save_and_load(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for ref in _SPEC.refs:
        await gm.add_node(ref, NodeType.APP, {"label": ref.removeprefix("app:")})
    node, created = await gm.upsert_fact(replace(_SPEC, text="How does code affect brave?"))
    assert created
    await gm.save()

    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await reloaded.load()

    assert reloaded.get_node(node.id).spec.text == "How does code affect brave?"


# ============================================= 3 · it is a record, not a cache


async def test_re_asking_keeps_the_words_of_the_first_asking(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for ref in _SPEC.refs:
        await gm.add_node(ref, NodeType.APP, {"label": ref.removeprefix("app:")})
    node, created = await gm.upsert_fact(replace(_SPEC, text="asked on Monday"))
    assert created

    again, created_again = await gm.upsert_fact(replace(_SPEC, text="asked on Friday"))

    assert not created_again and again.id == node.id
    assert gm.get_node(node.id).spec.text == "asked on Monday"


async def test_a_later_value_still_wins_while_the_text_stands(tmp_path) -> None:
    """`value` is the live number (pressure, confidence); `text` is history.
    They must not be confused for each other."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"label": "Brave"})
    probe = LabelSpec(LabelKind.PROBE, ("app:brave",), "summary/L2.load", 1.43, "first look")
    node, _ = await gm.upsert_fact(probe)
    await gm.upsert_fact(replace(probe, value=1.59, text="second look"))

    stored = gm.get_node(node.id).spec
    assert stored.value == 1.59
    assert stored.text == "first look"


async def test_a_rename_heals_the_label_but_not_the_record(tmp_path) -> None:
    """The point of keeping both: the label tracks what things are called now,
    the text records what was actually asked then."""
    gm = await _graph(tmp_path)
    spec = LabelSpec(LabelKind.THOUGHT, ("app:alpha", "app:beta"), "how_does_x_affect_y")
    await gm.add_node("app:alpha", NodeType.APP, {"label": "Alpha"})
    await gm.add_node("app:beta", NodeType.APP, {"label": "Beta"})
    node, _ = await gm.upsert_fact(replace(spec, text="How does Alpha affect Beta?"))
    assert gm.get_node(node.id).label == "How does Alpha affect Beta?"

    await gm.update_node("app:alpha", {"label": "Alpha Editor"})
    await gm.canonicalise_app_nodes(lambda bare: bare, lambda bare: False)  # re-renders

    healed = gm.get_node(node.id)
    assert healed.label == "How does Alpha Editor affect Beta?"
    assert healed.spec.text == "How does Alpha affect Beta?"


# ========================================================== 4 · compatibility


def test_the_schema_version_says_a_v7_file_is_a_v7_file() -> None:
    assert graph_schema_version() == 7


async def test_a_v6_graph_loads_with_no_text(tmp_path) -> None:
    """The field is additive: an existing graph loads unchanged, ids included."""
    path = tmp_path / "g.json"
    now = "2026-09-01T12:00:00+00:00"
    record = {
        "id": fact_id(_SPEC),
        "node_type": "idle_thought",
        "label": "How does code affect brave?",
        "created_at": now,
        "last_accessed": now,
        "access_count": 1,
        "relevance_score": 0.0,
        "priority": 0,
        "spec": {"kind": "thought", "refs": list(_SPEC.refs), "facet": _SPEC.facet, "value": None},
    }
    path.write_text(json.dumps({"schema_version": 6, "nodes": [record], "edges": []}))

    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(path))
    await gm.load()

    node = gm.get_node(fact_id(_SPEC))
    assert node is not None and node.spec.text is None
    await gm.save()
    assert json.loads(path.read_text())["schema_version"] == 7


def test_a_malformed_text_field_does_not_break_the_tolerant_reader() -> None:
    spec = LabelSpec.from_record(
        {"kind": "thought", "refs": ["app:a", "app:b"], "facet": "x", "text": 12345}
    )
    assert spec is not None and spec.text == "12345"


# ================================================== 5 · the DMN actually stores it


async def test_a_stored_idle_thought_carries_the_question_as_asked(tmp_path) -> None:
    import random

    from neuropaca.core.bitnet_runtime import BitNetRuntime
    from neuropaca.core.clock import FakeClock
    from neuropaca.core.config import Config
    from neuropaca.core.event_bus import EventBus
    from neuropaca.idle.dmn import DefaultModeNetwork

    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    dmn = DefaultModeNetwork(
        bus,
        Config(inference_backend="fake"),
        gm,
        BitNetRuntime.get_instance(),
        clock=FakeClock(),
        rng=random.Random(0),
    )
    await dmn.initialize()
    await dmn.start()
    try:
        for i in range(6):
            await gm.add_node(
                f"app:{i}", NodeType.APP, {"label": f"service {i}", "relevance_score": 9.0 - i}
            )
            await gm.add_edge(f"app:{i}", "domain:engineering", RelationType.PART_OF)

        assert await dmn._imagination() >= 1

        thoughts = [gm.get_node(n) for n in gm.node_ids if n.startswith("idle:")]
        assert thoughts
        for thought in thoughts:
            assert thought.spec.text, f"{thought.id} stored no question text"
            # the words of the moment == the label at the moment of asking
            assert thought.spec.text == thought.label
            assert thought.spec.text.endswith("?")
    finally:
        await dmn.stop()
        await bus.stop()
