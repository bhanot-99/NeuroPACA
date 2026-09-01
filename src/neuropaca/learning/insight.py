"""L4 · the `Insight` data model (Architecture.md §6, D-11).

An insight is **extractive**, not generated. The B0 spike proved BitNet b1.58
2B4T cannot write a grounded sentence over graph context (`problems.md` 1.13);
so L4 asks the model for exactly two enum-constrained fields — *which* cited node
is salient and *what category* the episode is — and builds the human-readable
line from a template. Nothing here is free text from the model.

`INSIGHT_GENERATED` carries an `Insight`; `BitNetPlasticity` also writes an
`INSIGHT` graph node (`insight:<uuid>`) edged `RELATED_TO` to every cited node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from neuropaca.core.enums import SignalType

# The full closed set an insight's category may take. `routine` / `anomaly` /
# `distraction` are the D-11 L4 grammar's `insight_category` enum; `proactive`
# (D-13) is L6's — an idle-thought follow-up question, not a signal category, so
# it never appears in the L4 grammar, only on an `Insight` the DMN builds.
INSIGHT_CATEGORIES: tuple[str, ...] = ("routine", "anomaly", "distraction", "proactive")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Insight:
    """One extractive observation about a correlated signal.

    `cited_node_ids` is a tuple for forward-compatibility, but the D-11 grammar
    emits a single `cited_node_id`, so it currently holds exactly one id.
    `snapshot_count` is `len(signal.source_snapshots)` — the B4 exit criterion
    is "every stored insight traces to >= 1 snapshot and >= 1 node".
    """

    category: str
    cited_node_ids: tuple[str, ...]
    source_signal: SignalType
    confidence: float
    snapshot_count: int
    node_id: str = ""  # the `insight:<uuid>` / `idle:<uuid>` graph node id, filled on store
    # B6 (D-13): the rendered idle-thought question, e.g. "How does X affect Y?".
    # Empty for L4 insights, whose `summary` stays a category template.
    detail: str = ""
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.category not in INSIGHT_CATEGORIES:
            raise ValueError(f"unknown insight category: {self.category!r}")

    @property
    def summary(self) -> str:
        """The human-readable line. For an L6 proactive thought this is the
        extractively-assembled question (`detail`); for an L4 insight it is a
        category template — no model free text in either case (D-11, D-13)."""
        if self.detail:
            return self.detail
        cited = self.cited_node_ids[0] if self.cited_node_ids else "?"
        return f"{self.category}: {self.source_signal} implicates {cited}"

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
