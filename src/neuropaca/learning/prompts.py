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

from neuropaca.core.context import build_aliased_context
from neuropaca.core.enums import SignalType
from neuropaca.core.models import GroundedAnswer, Node
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
    """The distilled facts block — the shared serialiser (`core/context.py`, A8),
    two-space indented as this prompt family has always rendered it."""
    return build_aliased_context(aliased)


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


# ============================================================================
# L9 · the interactive `$` / `$?` answer (B5, A2).
#
# `problems.md` 1.13: 2B4T cannot write a grounded sentence, so the interactive
# path routes to a larger Qwen2.5-3B Q4 model (D-12). It *is* asked for free
# text — one sentence — but still fully bounded: the `cited_nodes` field is an
# enum of this prompt's aliases, `null` abstains, and `parse_answer` is a hard
# gate (rules.md §4.1) that discards any answer whose sentence does not
# substantively name a cited node's label. No grounded answer -> L9 falls back
# to the extractive template, never a raw model string.
#
#   {"insight": "<one sentence>" | null,
#    "cited_nodes": ["n1", ...],           // >= 1, all from THIS prompt
#    "confidence": 0.0-1.0}
# ============================================================================

# A single logical line per rule (the llama.cpp GBNF parser ends a rule at a
# top-level newline; a multi-line body segfaults the sampler — see above).
#
# `ws` is a *single optional space*, not `[ \t\n]*` (B5 validation finding,
# 2026-09-01): a weak model, given free whitespace between tokens, fell into a
# space-emitting loop and burned its token budget before closing the `}`. The
# output shape is fully defined here — flexible whitespace buys nothing and costs
# coherence. The compact form matches the few-shot exactly.
_ANSWER_GRAMMAR_TEMPLATE = (
    'root ::= "{" ws "\\"insight\\":" ws (sentence | "null") ws "," ws '
    '"\\"cited_nodes\\":" ws "[" ws aliaslist ws "]" ws "," ws '
    '"\\"confidence\\":" ws number ws "}"\n'
    'sentence ::= "\\"" schar schar* "\\""\n'
    'schar ::= [^"\\\\] | "\\\\" ["\\\\nt/]\n'
    'aliaslist ::= alias (ws "," ws alias)*\n'
    "alias ::= __ALIASES__\n"
    'number ::= "1.0" | "1" | "0" | "0." [0-9] [0-9]?\n'
    'ws ::= " "?\n'
)

# Free text for the interactive path — one sentence, hard-capped so a drifting
# model cannot ramble (rules.md §4.1). ~80 tokens covers a full sentence + the
# JSON envelope.
ANSWER_MAX_TOKENS = 96

_ANSWER_FEW_SHOT = (
    "Facts:\n"
    "  [n1] webpack · APP · score 8.1\n"
    "  [n2] ~/src/app · FILE · score 7.4\n"
    "Question: what is using my CPU?\n"
    "Answer one sentence grounded in the facts, naming the fact(s) you used. "
    "If the facts do not support an answer, use null.\n"
    'Answer: {"insight": "webpack is the heaviest CPU consumer right now.", '
    '"cited_nodes": ["n1"], "confidence": 0.86}\n\n'
)


def build_answer_grammar(aliases: Sequence[str]) -> str:
    """Splice this prompt's alias enum into the `$?` skeleton. `aliases` must be
    exactly the aliases present in the prompt (`rules.md §4.1`)."""
    if not aliases:
        raise ValueError("at least one alias is required")
    for alias in aliases:
        if not _ALIAS_RE.match(alias):
            raise ValueError(f"not a local alias: {alias!r}")
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"duplicate aliases: {list(aliases)!r}")
    enum = " | ".join(f'"\\"{alias}\\""' for alias in aliases)
    return _ANSWER_GRAMMAR_TEMPLATE.replace("__ALIASES__", enum)


def build_answer_prompt(
    question: str,
    aliased: Sequence[tuple[str, Node]],
    *,
    live_snapshot: str | None = None,
) -> str:
    """One synthetic few-shot, then the distilled graph facts, an optional live
    system snapshot line (the `$?` diagnose path only), then the question last
    (`problems.md` 1.13)."""
    snap = f"Live: {live_snapshot}\n" if live_snapshot else ""
    return (
        _ANSWER_FEW_SHOT
        + "Facts:\n"
        + _context_block(aliased)
        + "\n"
        + snap
        + f"Question: {question.strip()}\n"
        + "Answer one sentence grounded in the facts, naming the fact(s) you used. "
        + "If the facts do not support an answer, use null.\n"
        + "Answer: "
    )


def parse_answer(
    raw: str,
    alias_to_id: dict[str, str],
    alias_to_label: dict[str, str],
) -> GroundedAnswer | None:
    """The hard validation gate for `$?` (`rules.md §4.1` item 6):

    1. parses against the schema (first JSON object);
    2. `insight` is a non-empty string (``null`` -> abstain -> `None`);
    3. `cited_nodes` is non-empty and every alias was in the prompt;
    4. `confidence` is a real number in [0, 1];
    5. **grounding** — the sentence contains a case-insensitive substring of at
       least one cited node's label. Citation without grounding is discarded.
    """
    obj = _first_json_object(raw)
    if obj is None:
        return None
    insight = obj.get("insight")
    cited = obj.get("cited_nodes")
    confidence = obj.get("confidence")

    if insight is None:  # explicit abstain
        return None
    if not isinstance(insight, str) or not insight.strip():
        return None
    if not isinstance(cited, list) or not cited:
        return None
    if any(not isinstance(a, str) or a not in alias_to_id for a in cited):
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0.0 <= float(confidence) <= 1.0:
        return None

    text_l = insight.lower()
    grounded = any(
        alias_to_label.get(a, "\x00").lower() in text_l
        or any(tok in text_l for tok in alias_to_label.get(a, "").lower().split() if len(tok) >= 4)
        for a in cited
    )
    if not grounded:
        return None

    return GroundedAnswer(
        text=insight.strip(),
        cited_node_ids=tuple(dict.fromkeys(alias_to_id[a] for a in cited)),
        confidence=float(confidence),
    )


# ============================================================================
# L6 · the proactive idle thought (B6, D-13).
#
# `problems.md` 1.13 is open for L6: the 2B4T loop model cannot write a grounded
# sentence, and the interactive Qwen model is not on the always-on loop. So the
# DMN's "imagination" is **strictly extractive** — the model selects a subject
# node, optionally an object node, and one `query_template` from a closed enum.
# The human-readable question is assembled from a Python template, never
# generated. Same discipline as D-11: the schema *is* the task.
#
#   {"subject": "n1",
#    "object": "n2" | null,               // both from THIS prompt
#    "query_template": "how_does_x_affect_y" | ...}
# ============================================================================

# Rendered question per template. `{x}` is the subject label, `{y}` the object's.
PROACTIVE_TEMPLATES: dict[str, str] = {
    "how_does_x_affect_y": "How does {x} affect {y}?",
    "what_connects_x_and_y": "What connects {x} and {y}?",
    "what_changed_in_x": "What changed in {x} recently?",
    "why_is_x_active": "Why has {x} been active so often?",
}
# Templates that are meaningless without a distinct object node.
_PROACTIVE_NEEDS_OBJECT: frozenset[str] = frozenset(
    {"how_does_x_affect_y", "what_connects_x_and_y"}
)
# An extractive recombination of two real nodes — high, fixed. L9 surfaces an
# insight at confidence >= 0.75 (B5); a proactive thought clears that by design.
_PROACTIVE_CONFIDENCE = 0.8
# Two enum fields + one alias — nothing to ramble into.
PROACTIVE_MAX_TOKENS = 48

_PROACTIVE_GRAMMAR_TEMPLATE = (
    'root ::= "{" ws "\\"subject\\":" ws alias ws "," ws '
    '"\\"object\\":" ws (alias | "null") ws "," ws '
    '"\\"query_template\\":" ws template ws "}"\n'
    "alias ::= __ALIASES__\n"
    'template ::= "\\"how_does_x_affect_y\\"" | "\\"what_connects_x_and_y\\"" | '
    '"\\"what_changed_in_x\\"" | "\\"why_is_x_active\\""\n'
    'ws ::= " "?\n'
)

_PROACTIVE_FEW_SHOT = (
    "Facts:\n"
    "  [n1] esbuild-service · app · score 8.0\n"
    "  [n2] ~/src/api · file · score 7.1\n"
    "Pick a subject fact, optionally an object fact, and the question template "
    "to explore next.\n"
    'Answer: {"subject": "n1", "object": "n2", "query_template": "how_does_x_affect_y"}\n\n'
)


def build_proactive_grammar(aliases: Sequence[str]) -> str:
    """Splice this prompt's alias enum into the proactive skeleton. `aliases`
    must be exactly the aliases present in the prompt (`rules.md §4.1`)."""
    if not aliases:
        raise ValueError("at least one alias is required")
    for alias in aliases:
        if not _ALIAS_RE.match(alias):
            raise ValueError(f"not a local alias: {alias!r}")
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"duplicate aliases: {list(aliases)!r}")
    enum = " | ".join(f'"\\"{alias}\\""' for alias in aliases)
    return _PROACTIVE_GRAMMAR_TEMPLATE.replace("__ALIASES__", enum)


def build_proactive_prompt(aliased: Sequence[tuple[str, Node]]) -> str:
    """One synthetic few-shot, then the distilled top-K graph facts. No question
    to answer — the model only selects (`problems.md` 1.13, D-13)."""
    return (
        _PROACTIVE_FEW_SHOT
        + "Facts:\n"
        + _context_block(aliased)
        + "\n"
        + "Pick a subject fact, optionally an object fact, and the question template "
        + "to explore next.\n"
        + "Answer: "
    )


def parse_proactive(
    raw: str,
    alias_to_id: dict[str, str],
    alias_to_label: dict[str, str],
) -> Insight | None:
    """The hard validation gate for an idle thought (`rules.md §4.1` item 6):

    1. parses against the schema (first JSON object);
    2. `subject` is an alias that was in the prompt;
    3. `query_template` is one of `PROACTIVE_TEMPLATES`;
    4. `object` is `null` or an alias that was in the prompt;
    5. a relational template (`_PROACTIVE_NEEDS_OBJECT`) requires a distinct
       object; a single-subject template ignores any object the model supplied.

    Returns a `proactive` `Insight` whose `detail` is the rendered question, or
    `None` — the caller discards `None`, never stores a guess.
    """
    obj = _first_json_object(raw)
    if obj is None:
        return None
    subject = obj.get("subject")
    objct = obj.get("object")
    template = obj.get("query_template")

    if not isinstance(subject, str) or subject not in alias_to_id:
        return None
    if not isinstance(template, str) or template not in PROACTIVE_TEMPLATES:
        return None
    if objct is not None and (not isinstance(objct, str) or objct not in alias_to_id):
        return None

    needs_object = template in _PROACTIVE_NEEDS_OBJECT
    if needs_object:
        if objct is None or objct == subject:
            return None
    else:
        objct = None  # a single-subject question — drop a spurious object

    x_label = alias_to_label.get(subject, subject)
    cited = [alias_to_id[subject]]
    if objct is not None:
        y_label = alias_to_label.get(objct, objct)
        cited.append(alias_to_id[objct])
    else:
        y_label = ""
    question = PROACTIVE_TEMPLATES[template].format(x=x_label, y=y_label)

    return Insight(
        category="proactive",
        cited_node_ids=tuple(dict.fromkeys(cited)),
        source_signal=SignalType.IDLE,
        confidence=_PROACTIVE_CONFIDENCE,
        snapshot_count=0,
        detail=question,
    )
