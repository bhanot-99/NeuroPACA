# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L4 · the `Insight` data model (Architecture.md §6, D-11).

An insight is **extractive**, not generated. The B0 spike proved BitNet b1.58
2B4T cannot write a grounded sentence over graph context (`problems.md` 1.13);
so L4 asks the model for exactly two enum-constrained fields — *which* cited node
is salient and *what category* the episode is. L6 likewise picks a question
*template key* and its subject/object nodes. Nothing here is free text from the
model.

B18: an insight's words are not stored on it either. `spec` says what it is
about (by node id) and `summary` is `labels.render(spec)` — the same renderer
the graph, the window and the terminal use. `label` is filled on store with the
graph's rendering (real names of the cited nodes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from neuropaca.core.enums import SignalType
from neuropaca.core.labels import LabelKind, LabelSpec, leaf_name, render

# The full closed set an insight's category may take. `routine` / `anomaly` /
# `distraction` are the D-11 L4 grammar's `insight_category` enum; `proactive`
# (D-13) is L6's — an idle-thought follow-up question, not a signal category, so
# it never appears in the L4 grammar, only on an `Insight` the DMN builds.
INSIGHT_CATEGORIES: tuple[str, ...] = ("routine", "anomaly", "distraction", "proactive")
L4_CATEGORIES: tuple[str, ...] = INSIGHT_CATEGORIES[:3]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Insight:
    """One extractive observation about a correlated signal (L4) or one open
    question about two live nodes (L6, `category == "proactive"`).

    `snapshot_count` is `len(signal.source_snapshots)` — the B4 exit criterion
    is "every stored insight traces to >= 1 snapshot and >= 1 node".
    """

    category: str
    cited_node_ids: tuple[str, ...]
    source_signal: SignalType
    confidence: float
    snapshot_count: int
    node_id: str = ""  # the `insight:<fp>` / `idle:<fp>` graph node id, filled on store
    # L6 (D-13): the `THOUGHT_TEMPLATES` key the model chose. Empty for L4.
    template: str = ""
    # B18: the graph's rendering of `spec`, filled on store. Empty before.
    label: str = ""
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.category not in INSIGHT_CATEGORIES:
            raise ValueError(f"unknown insight category: {self.category!r}")

    @property
    def spec(self) -> LabelSpec:
        """What this insight is about — its identity in the graph (B18)."""
        if self.category == "proactive":
            return LabelSpec(LabelKind.THOUGHT, self.cited_node_ids, self.template)
        return LabelSpec(
            LabelKind.INSIGHT,
            self.cited_node_ids,
            f"{self.category}/{self.source_signal}",
            self.confidence,
        )

    @property
    def summary(self) -> str:
        """The human-readable line: the stored rendering, or — before storage —
        the same template over the cited ids' leaf names."""
        return self.label or render(self.spec, lambda ref: leaf_name(ref, ""))

    def traces_to_evidence(self) -> bool:
        """Every stored insight must reach real evidence. An L4 insight needs a
        source snapshot and a cited node (B4 exit gate); an L6 proactive thought
        is grounded by construction — it cites live graph nodes, not a signal —
        so a cited node alone is enough (D-13)."""
        if not self.cited_node_ids:
            return False
        if self.category == "proactive":
            return True
        return self.snapshot_count >= 1


# gen-ref: 1e08e934
