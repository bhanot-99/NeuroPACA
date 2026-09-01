"""The closed enumerations shared across every layer (Architecture.md §3.6).

`rules.md §7`: a string literal where an enum belongs is a defect. Every event
kind, node kind, edge relation, signal kind, and interface channel is one of
these members and nothing else.

All five are `StrEnum` so a member *is* its wire string — `NodeType.FILE ==
"file"` — which means `json.dumps` serialises the graph without a custom encoder
and `NodeType("file")` round-trips it back on load. `rules.md §9`: changing a
member requires human approval and a schema-version bump.
"""

from __future__ import annotations

from enum import StrEnum, auto


class EventType(StrEnum):
    """Every message that can travel on the `EventBus` (Architecture.md §13)."""

    METRIC_COLLECTED = auto()
    SIGNAL_CORRELATED = auto()
    PATTERN_DETECTED = auto()
    ACTION_TRIGGERED = auto()
    MEMORY_UPDATED = auto()
    PRESSURE_THRESHOLD_REACHED = auto()
    IDLE_DETECTED = auto()
    ACTIVITY_DETECTED = auto()
    APP_SWITCH = auto()  # B2.5b — focused app_id changed; payload {app_id, title, previous_app_id}
    INSIGHT_GENERATED = auto()
    USER_MESSAGE = auto()
    AGENT_SPAWNED = auto()
    AGENT_COMPLETED = auto()
    SYSTEM_ERROR = auto()
    # B5 · Interface (L9, A6). The health bridge: L9 publishes a REQUEST, L10 —
    # which alone holds every module's health() — answers with a REPORT. Keeps
    # the orchestrator out of the module import graph (rules.md §0).
    SYSTEM_HEALTH_REQUEST = auto()
    SYSTEM_HEALTH_REPORT = auto()


class NodeType(StrEnum):
    """The kind of thing a graph node represents (Architecture.md §3.6)."""

    TASK = auto()
    PERSON = auto()
    CONCEPT = auto()
    FILE = auto()
    EVENT_LOG = auto()
    METRIC = auto()
    INSIGHT = auto()
    APP = auto()
    SESSION = auto()
    GOAL = auto()


class RelationType(StrEnum):
    """The kind of a directed edge. In the `MultiDiGraph` this is the edge key,
    so a pair of nodes can carry several relations at once (Architecture.md §3.2)."""

    RELATED_TO = auto()
    CAUSED_BY = auto()
    PART_OF = auto()
    DEPENDS_ON = auto()
    CREATED = auto()
    MODIFIED = auto()
    FOLLOWED_BY = auto()
    CONTRADICTS = auto()


class SignalType(StrEnum):
    """A correlated behavioural signal (produced in L3, defined here so L1 payloads
    can name it). Only the first four have patterns before B3 (Architecture.md §5)."""

    FOCUS_SESSION = auto()
    DISTRACTION = auto()
    HIGH_LOAD = auto()
    IDLE = auto()
    FILE_ACTIVITY = auto()
    APP_SWITCH = auto()
    USER_RETURN = auto()


class InterfaceChannel(StrEnum):
    """Where an L9 `Message` is delivered (Architecture.md §9)."""

    CLI = auto()
    WEB_SOCKET = auto()
    NOTIFICATION_ONLY = auto()


class MessageRole(StrEnum):
    """Who authored an L9 `Message` (Architecture.md §9, B8). The blueprint's
    `Message.role: str` is replaced by this closed set — a bare string here is a
    defect like anywhere else (rules.md §7). `conversation_history` is RAM-only
    (rules.md §6), so these values never reach disk."""

    USER = auto()
    ASSISTANT = auto()
    SYSTEM = auto()
