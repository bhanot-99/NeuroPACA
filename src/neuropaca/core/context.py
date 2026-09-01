"""The one serialiser for graph context handed to the model (rules.md §4.1, D-13).

Before B6 this line format lived in three places — `BitNetRuntime`,
`learning/prompts.py._context_block`, and `InterfaceLayer._nodes_within_budget` —
each a copy. L4, L6, and L9 now all distil through here, so a change to how a
node reads to the model happens in exactly one spot (A8).

Two entry points, same line shape:
- `build_context_from_nodes(nodes)` — keyed by real node id (`BitNetRuntime`);
- `build_aliased_context(aliased)` — keyed by this prompt's `n1..nK` local alias
  (rules.md §4.1: raw ids never enter a GBNF enum).
"""

from __future__ import annotations

from collections.abc import Sequence

from neuropaca.core.models import Node


def format_node_line(ref: str, node: Node, *, indent: str = "") -> str:
    """One terse fact line: ``[ref] label · node_type · score N.N``.

    `ref` is whatever the caller keys the node by — its id, or a local alias.
    Distillation to top-K happens in the caller, never here.
    """
    return f"{indent}[{ref}] {node.label} · {node.node_type} · score {node.relevance_score:.1f}"


def build_context_from_nodes(nodes: Sequence[Node], *, indent: str = "") -> str:
    """One line per node, keyed by node id."""
    return "\n".join(format_node_line(node.id, node, indent=indent) for node in nodes)


def build_aliased_context(aliased: Sequence[tuple[str, Node]], *, indent: str = "  ") -> str:
    """One line per `(alias, node)` pair, keyed by the local alias."""
    return "\n".join(format_node_line(alias, node, indent=indent) for alias, node in aliased)
