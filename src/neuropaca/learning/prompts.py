"""L4 · prompt + GBNF assembly for extractive insight classification (D-11).

`rules.md §7`: every prompt string lives here; an inline prompt elsewhere is a
defect. `rules.md §4.1`: one grammar per task, `cited_node_id` locked to the
aliases in *this* prompt, an abstain path, distilled input, greedy decode, a
hard post-generation validation gate.

The B0 spike (`problems.md` 1.13) killed free-text generation on 2B4T, so the
grammar here asks for exactly two enum fields:

    {"cited_node_id": "n2" | null, "insight_category": "routine"|"anomaly"|"distraction"}

- `cited_node_id` — the single most salient node, by local alias; `null` abstains
- `insight_category` — one of `INSIGHT_CATEGORIES`

Alias assembly and the grammar string are pure string work done **before**
`_inference_lock` is taken (`rules.md §4.1`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from neuropaca.core.enums import SignalType
from neuropaca.core.models import Node
from neuropaca.learning.insight import INSIGHT_CATEGORIES, Insight

_ALIAS_RE = re.compile(r"^n[1-9][0-9]*$")

# One logical line per rule — this llama.cpp GBNF parser ends a rule at a
# top-level newline; a multi-line body segfaults the sampler (B0 spike note).
_GRAMMAR_TEMPLATE = (
    'root ::= "{" ws "\\"cited_node_id\\":" ws (alias | "null") ws "," ws '
    '"\\"insight_category\\":" ws category ws "}"\n'
    "alias ::= __ALIASES__\n"
    'category ::= "\\"routine\\"" | "\\"anomaly\\"" | "\\"distraction\\""\n'
    "ws ::= [ \\t\\n]*\n"
)

_FEW_SHOT = (
    "Facts:\n"
    "  [n1] webpack · APP · score 8.1 · cpu_avg 96\n"
    "  [n2] ~/src/app · FILE · score 7.4\n"
    "Signal: HIGH_LOAD (conf 0.90)\n"
    "Pick the one fact most responsible for this signal and classify the episode "
    "(routine / anomaly / distraction). If no fact fits, cite null.\n"
    'Answer: {"cited_node_id": "n1", "insight_category": "anomaly"}\n\n'
)

# Extractive JSON is tiny — cap hard so a confused model cannot ramble past it.
INSIGHT_MAX_TOKENS = 48


def alias_nodes(nodes: Sequence[Node]) -> list[tuple[str, Node]]:
    """Assign `n1..nK` local aliases in the given order (no raw ids in the grammar)."""
    return [(f"n{i + 1}", node) for i, node in enumerate(nodes)]


def _context_block(aliased: Sequence[tuple[str, Node]]) -> str:
    lines = [
        f"  [{alias}] {node.label} · {node.node_type} · score {node.relevance_score:.1f}"
        for alias, node in aliased
    ]
    return "\n".join(lines)


def build_insight_grammar(aliases: Sequence[str]) -> str:
    """Splice the alias enum into the static skeleton. `aliases` must be exactly
    the aliases present in this prompt (`rules.md §4.1`)."""
    if not aliases:
        raise ValueError("at least one alias is required")
    for alias in aliases:
        if not _ALIAS_RE.match(alias):
            raise ValueError(f"not a local alias: {alias!r}")
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"duplicate aliases: {list(aliases)!r}")
    enum = " | ".join(f'"\\"{alias}\\""' for alias in aliases)
    return _GRAMMAR_TEMPLATE.replace("__ALIASES__", enum)


def build_insight_prompt(
    signal_type: SignalType, confidence: float, aliased: Sequence[tuple[str, Node]]
) -> str:
    """One synthetic few-shot, then this signal's distilled facts, signal last
    (`problems.md` 1.13)."""
    return (
        _FEW_SHOT
        + "Facts:\n"
        + _context_block(aliased)
        + "\n"
        + f"Signal: {signal_type} (conf {confidence:.2f})\n"
        + "Pick the one fact most responsible for this signal and classify the episode "
        + "(routine / anomaly / distraction). If no fact fits, cite null.\n"
        + "Answer: "
    )


def _first_json_object(raw: str) -> dict[str, object] | None:
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start : i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def parse_insight(
    raw: str,
    alias_to_id: dict[str, str],
    *,
    source_signal: SignalType,
    confidence: float,
    snapshot_count: int,
) -> Insight | None:
    """The hard validation gate (`rules.md §4.1` item 6). Returns `None` on an
    abstain (`cited_node_id: null`) or any malformed / out-of-vocab output —
    the caller discards it, never stores a guess."""
    obj = _first_json_object(raw)
    if obj is None:
        return None
    cited = obj.get("cited_node_id")
    category = obj.get("insight_category")
    if cited is None:  # explicit abstain
        return None
    if not isinstance(cited, str) or cited not in alias_to_id:
        return None
    if not isinstance(category, str) or category not in INSIGHT_CATEGORIES:
        return None
    return Insight(
        category=category,
        cited_node_ids=(alias_to_id[cited],),
        source_signal=source_signal,
        confidence=confidence,
        snapshot_count=snapshot_count,
    )
