"""The B0 spike's prompt + GBNF + validation-gate logic (no model needed).

`spikes/b0_bitnet/_common.py` is throwaway spike code, but its grammar assembly
and post-generation validation gate are the reference the real
`learning/prompts.py` + `BitNetRuntime` are built against in B4 — so the pure
logic is worth pinning now. `llama_cpp` is imported lazily inside `_common`, so
importing the module here needs no native dependency.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

_SPIKE_DIR = Path(__file__).resolve().parents[1] / "spikes" / "b0_bitnet"
sys.path.insert(0, str(_SPIKE_DIR))

import _common as sc  # noqa: E402  (path shim above)
import coherence_ablation as ablation  # noqa: E402
import gen_fixtures  # noqa: E402

try:
    from llama_cpp import LlamaGrammar  # type: ignore[import-not-found]
except ImportError:  # the `spike` extra isn't installed in the default env
    LlamaGrammar = None


def _node(alias: str, label: str = "bundler-watch") -> sc.ContextNode:
    return sc.ContextNode(
        alias=alias, label=label, node_type="APP", score=8.0, attrs={"cpu_avg": "93%"}
    )


def _fixture(
    *, nodes: tuple[sc.ContextNode, ...], expected: tuple[str, ...], weak: bool = False
) -> sc.Fixture:
    return sc.Fixture(
        fixture_id="t",
        signal="HIGH_LOAD",
        confidence=0.8,
        nodes=nodes,
        expected_citations=expected,
        weak=weak,
    )


# --------------------------------------------------------------------------- #
# prompt assembly
# --------------------------------------------------------------------------- #


def test_prompt_puts_the_signal_last_and_keeps_one_few_shot() -> None:
    fx = _fixture(nodes=(_node("n1"), _node("n2", "linter-daemon")), expected=("n1",))
    prompt = sc.build_prompt(fx)

    assert prompt.count("Example:") == 1
    # the live signal line comes after the facts block
    assert prompt.rindex("Signal: HIGH_LOAD (conf 0.80)") > prompt.rindex("[n2]")
    assert prompt.rstrip().endswith("Answer:")


# --------------------------------------------------------------------------- #
# GBNF grammar assembly
# --------------------------------------------------------------------------- #


def test_grammar_splices_exactly_the_prompt_aliases() -> None:
    gbnf = sc.build_grammar(["n1", "n2", "n3"])

    assert sc.ALIAS_MARKER not in gbnf
    assert 'alias       ::= "\\"n1\\"" | "\\"n2\\"" | "\\"n3\\""' in gbnf
    assert "root        ::=" in gbnf
    assert '"n4"' not in gbnf


@pytest.mark.skipif(LlamaGrammar is None, reason="needs the `spike` extra (llama-cpp-python)")
@pytest.mark.parametrize("aliases", [["n1"], ["n1", "n2", "n3"], ["n1", "n2", "n3", "n4", "n5"]])
def test_assembled_grammar_compiles_in_llama_cpp(aliases: list[str]) -> None:
    # Catches a broken GBNF template before the target-machine run.
    LlamaGrammar.from_string(sc.build_grammar(aliases), verbose=False)


@pytest.mark.parametrize("bad", [["nodeid"], ["n0"], ["N1"], ["n1", "file:/x"], [""]])
def test_grammar_rejects_anything_that_is_not_a_local_alias(bad: list[str]) -> None:
    with pytest.raises(ValueError, match="not a local alias"):
        sc.build_grammar(bad)


def test_grammar_rejects_duplicate_aliases() -> None:
    with pytest.raises(ValueError, match="duplicate aliases"):
        sc.build_grammar(["n1", "n1"])


# --------------------------------------------------------------------------- #
# the hard validation gate  (rules.md §4.1 item 6)
# --------------------------------------------------------------------------- #


def test_valid_grounded_answer_passes_every_check() -> None:
    fx = _fixture(
        nodes=(_node("n1", "bundler-watch"), _node("n2", "linter-daemon")), expected=("n1",)
    )
    raw = json.dumps(
        {
            "insight": "bundler-watch has pinned a core for 40 minutes.",
            "cited_nodes": ["n1"],
            "confidence": 0.8,
        }
    )
    res = sc.score_output(fx, raw, 1.0)

    assert res.parse_ok and res.citations_valid and res.grounded
    assert not res.abstained
    assert sc.citation_accuracy(fx, res) == 1.0


def test_a_citation_not_in_the_prompt_is_rejected() -> None:
    fx = _fixture(nodes=(_node("n1"),), expected=("n1",))
    raw = json.dumps({"insight": "bundler-watch is hot.", "cited_nodes": ["n2"], "confidence": 0.7})
    res = sc.score_output(fx, raw, 1.0)

    assert res.parse_ok
    assert not res.citations_valid
    assert not res.grounded


def test_text_that_never_names_a_cited_label_is_not_grounded() -> None:
    fx = _fixture(nodes=(_node("n1", "bundler-watch"),), expected=("n1",))
    raw = json.dumps(
        {"insight": "Something is using the CPU.", "cited_nodes": ["n1"], "confidence": 0.6}
    )
    res = sc.score_output(fx, raw, 1.0)

    assert res.citations_valid
    assert not res.grounded  # free text must substring-match a cited node label


def test_abstain_is_correct_only_when_the_fixture_wanted_it() -> None:
    weak = _fixture(nodes=(_node("n1", "stale-cache-dir"),), expected=(), weak=True)
    strong = _fixture(nodes=(_node("n1", "bundler-watch"),), expected=("n1",))
    raw = json.dumps({"insight": None, "cited_nodes": [], "confidence": 0.0})

    assert sc.score_output(weak, raw, 1.0).grounded is True
    assert sc.score_output(strong, raw, 1.0).grounded is False
    assert sc.citation_accuracy(weak, sc.score_output(weak, raw, 1.0)) is None


def test_unparseable_output_fails_closed() -> None:
    fx = _fixture(nodes=(_node("n1"),), expected=("n1",))
    res = sc.score_output(fx, "the model rambled without json", 1.0)

    assert not res.parse_ok
    assert not res.grounded
    assert sc.citation_accuracy(fx, res) == 0.0


def test_partial_citation_accuracy() -> None:
    fx = _fixture(nodes=(_node("n1"), _node("n2"), _node("n3")), expected=("n1", "n2"))
    raw = json.dumps(
        {"insight": "bundler-watch is the cause.", "cited_nodes": ["n1", "n3"], "confidence": 0.7}
    )
    res = sc.score_output(fx, raw, 1.0)
    assert sc.citation_accuracy(fx, res) == pytest.approx(0.5)


def test_completion_tokens_per_second() -> None:
    assert sc.Completion("x", 2.0, 40).tokens_per_second == pytest.approx(20.0)
    assert sc.Completion("x", 0.0, 40).tokens_per_second == 0.0  # no div-by-zero


# --------------------------------------------------------------------------- #
# ablation knee-picker
# --------------------------------------------------------------------------- #


def test_pick_knee_stops_where_the_curve_flattens() -> None:
    # big jump 1->3, then flat: knee is 3
    assert ablation._pick_knee({1: 0.40, 3: 0.82, 5: 0.83, 8: 0.84}) == 3
    # keeps climbing meaningfully: knee is the last K
    assert ablation._pick_knee({1: 0.30, 3: 0.50, 5: 0.70, 8: 0.90}) == 8
    # nothing measurable
    assert ablation._pick_knee({1: None, 3: None}) is None


# --------------------------------------------------------------------------- #
# fixture generator <-> loader round trip
# --------------------------------------------------------------------------- #


def test_generated_fixtures_load_back_and_cover_every_k(tmp_path: Path) -> None:
    out = tmp_path / "ablation.jsonl"
    rng = random.Random(42)
    lines = [
        json.dumps(gen_fixtures._make_case(rng, k, idx))
        for k in gen_fixtures.K_VALUES
        for idx in range(1, 6)
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fixtures = sc.load_fixtures(out)
    assert {f.k for f in fixtures} == set(gen_fixtures.K_VALUES)
    assert any(f.weak for f in fixtures)
    for f in fixtures:
        assert len(f.nodes) == f.k
        assert [n.alias for n in f.nodes] == [f"n{i + 1}" for i in range(f.k)]
        # a non-weak fixture cites only aliases it actually contains
        assert set(f.expected_citations) <= {n.alias for n in f.nodes}
