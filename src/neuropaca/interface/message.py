"""L9 · the `Message` data model (Architecture.md §9, B8).

`conversation_history` is a list of these, held **in RAM only** — never written
to disk, the graph, or a log (rules.md §6, PRD §8.5). `InterfaceLayer` enforces
that; this dataclass just carries a turn.

The blueprint's `Message.role: str` / `related_node_ids: List[UUID]` are both
superseded: `role` is the `MessageRole` enum (rules.md §7) and every node
reference is a `str` (D-5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from neuropaca.core.enums import MessageRole


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of an L9 conversation. Immutable — history is append-only."""

    role: MessageRole
    content: str
    related_node_ids: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=_utcnow)
