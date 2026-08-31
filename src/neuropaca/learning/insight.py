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

# The full closed set the grammar's `insight_category` enum allows (D-11).
INSIGHT_CATEGORIES: tuple[str, ...] = ("routine", "anomaly", "distraction")


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
    node_id: str = ""  # the `insight:<uuid>` graph node id, filled on store
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.category not in INSIGHT_CATEGORIES:
            raise ValueError(f"unknown insight category: {self.category!r}")

    @property
    def summary(self) -> str:
        """The template-built human line — no model text (D-11)."""
        cited = self.cited_node_ids[0] if self.cited_node_ids else "?"
        return f"{self.category}: {self.source_signal} implicates {cited}"

    def traces_to_evidence(self) -> bool:
        """B4 exit gate — a stored insight must reach a snapshot and a node."""
        return self.snapshot_count >= 1 and len(self.cited_node_ids) >= 1
