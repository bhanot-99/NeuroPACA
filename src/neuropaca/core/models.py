"""The core dataclasses that move between layers (Architecture.md §3.5).

`rules.md §2`: event payloads are typed dataclasses, not ad-hoc dicts — `Event`
is the envelope, and each `EventType` has its own payload dataclass defined by
the layer that publishes it (from B2 on).

Identity (D-5): every node id and every field that *references* a node is `str`
(`file:/abs/path`, `app:code`, `domain:engineering`, `YOU`). Only `Event.id` is a
`UUID` — it identifies an event, never a node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from neuropaca.core.enums import EventType, NodeType, RelationType


def _utcnow() -> datetime:
    """Timezone-aware now. Never a naive datetime — those compare and serialise
    ambiguously (rules.md §1 spirit: no silent surprises)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Event:
    """One message on the `EventBus`. Immutable: a subscriber must not be able to
    mutate an event another subscriber will also see (Architecture.md §3.1)."""

    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    priority: int = 0
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class Node:
    """A graph node. Mutable — `last_accessed`, `access_count`, and
    `relevance_score` change over the node's life (Architecture.md §3.2, §3.5).

    `relevance_score` is a 0-10 composite recomputed on a schedule, never
    per-event (rules.md §3). `bridge_value` (a node's distinct `domain:*` reach)
    is live from B2.5b (D-10); it was fixed at 0.0 through B1-B3 (D-6).
    """

    id: str
    node_type: NodeType
    label: str
    created_at: datetime = field(default_factory=_utcnow)
    last_accessed: datetime = field(default_factory=_utcnow)
    access_count: int = 0
    relevance_score: float = 0.0
    priority: int = 0
    # B5 · set by L9 the first time an INSIGHT node is surfaced to the user
    # (surface-once). None on every other node type. Persisted (schema v2).
    surfaced_at: datetime | None = None


@dataclass(slots=True)
class Edge:
    """A directed, typed, weighted edge. `(source_id, target_id, relation)` is the
    identity — the `MultiDiGraph` keys parallel edges by `relation` (D-5).

    `weight` grows by Hebbian co-occurrence (`+= ~0.01`) from L4 (Architecture.md
    §6); in B1 it is only ever set explicitly by a caller.
    """

    source_id: str
    target_id: str
    relation: RelationType
    weight: float = 0.0
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """A parsed, validated `$?` response (B5, A2). The interactive model fills a
    GBNF schema (`learning/prompts.py`); `parse_answer` is the hard gate — a
    stored answer's `text` substantively references at least one cited node's
    label (rules.md §4.1). `null` insight / a failed gate yields `None`, and L9
    falls back to the extractive template.

    Moves from `learning.prompts.parse_answer` to `InterfaceLayer` in RAM only —
    never persisted (`conversation_history` is RAM-only, rules.md §6)."""

    text: str
    cited_node_ids: tuple[str, ...]
    confidence: float


def system_error_event(
    *, module: str, exception: str, severity: str, source: str = "eventbus"
) -> Event:
    """Build the `SYSTEM_ERROR` event every layer publishes on a caught failure
    (rules.md §2). Kept here so the payload shape is defined in exactly one place.
    """
    return Event(
        event_type=EventType.SYSTEM_ERROR,
        source=source,
        priority=10,
        payload={"module": module, "exception": exception, "severity": severity},
    )
