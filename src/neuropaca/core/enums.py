# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

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
    APP_SWITCH = auto()  # B2.5b/B14 — focused app or web-app changed; payload
    # {app_id, webapp, webapp_domain, previous_app_id, previous_webapp}. The raw
    # window title is NOT in the payload — only the allowlisted `webapp` label.
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
    # B7 · Drive & Action (L5 + L7, D-14). The confirmation handshake. The daemon
    # is headless — `neuropacad` has no TTY — so rules.md §5.2's "terminal
    # confirmation at execution time" is satisfied over the bus: L7 pauses the
    # action, publishes a REQUEST, and L9 relays it to the human's terminal; the
    # human's answer comes back as a RESPONSE carrying the same `request_id`.
    # No response inside `action_confirmation_timeout_seconds` = refusal.
    ACTION_CONFIRMATION_REQUEST = auto()
    ACTION_CONFIRMATION_RESPONSE = auto()
    # B8 · Agents (L8, D-16). The L7/L8 decoupling. L8 owns no `SafetyGate` —
    # duplicating it would mean two `ConfirmationBroker`s racing to consume the
    # human's one answer and two writers on one audit file. Instead L8 publishes
    # a *description* of the effect it wants (`action_type` + `kwargs`, never a
    # live `BaseAction`), L7 instantiates it from its own registry, runs it
    # through the single gate, and answers with the result. Same shape as the
    # B5 health bridge; `rules.md §0` ("no module imports another module") holds.
    ACTION_PROPOSAL = auto()
    ACTION_PROPOSAL_RESULT = auto()
    # A0 · Interface (F1, VISION_PHASES.md). The `Moment` seam — every typed,
    # grounded, evidence-carrying thing the machine proposes to say. Introduced
    # by `MomentComposer` (published straight through to delivery in A0);
    # gated by A3 once the guardian exists (deliver / hold / drop); fed to S5's
    # ranker via the feedback event. `MOMENT_DELIVERED` / `MOMENT_FEEDBACK` are
    # defined now (the closed set) though only A3/F2 populate them.
    MOMENT_PROPOSED = auto()
    MOMENT_DELIVERED = auto()
    MOMENT_FEEDBACK = auto()
    # S0 · Interface (VISION_PHASES.md). `neuropaca briefing`'s on-demand path:
    # the same request/report shape as `SYSTEM_HEALTH_REQUEST`/`_REPORT` — L9
    # cannot import `BriefingComposer` (rules.md §0), so it asks over the bus
    # and `BriefingComposer` answers with whatever it composes right now.
    BRIEFING_REQUEST = auto()
    BRIEFING_REPORT = auto()
    # A1 · Idle Cognition (VISION_PHASES.md). The tray's "thinking" state needs
    # to know a DMN cycle is running, and L9 cannot import `idle/dmn.py`
    # (rules.md §0) — so the DMN publishes its own cycle boundary, the same
    # request/report-adjacent pattern as the health bridge, but fire-and-forget
    # since nothing needs to *answer* it. `_ENDED` always fires, success,
    # timeout, or cancellation alike (a `finally`, not a bare `except`).
    DMN_CYCLE_STARTED = auto()
    DMN_CYCLE_ENDED = auto()
    # A2 · the mirror (VISION_PHASES.md §3.8). `neuropaca mirror`'s on-demand
    # path — the same request/report shape as `BRIEFING_REQUEST`/`_REPORT`,
    # for the same reason: L9 cannot import `MirrorComposer` (rules.md §0).
    MIRROR_REQUEST = auto()
    MIRROR_REPORT = auto()


class PresenceState(StrEnum):
    """A1 · the tray's state machine (VISION_PHASES.md). Precedence, highest
    first: `THINKING` > `NOTICED` > `FOCUSED` > `IDLE` > `AWAKE` —
    `core/presence.py`'s `compute_presence_state` is the one place that order
    is encoded; nothing else may re-derive it. Restored after the terminal/L9
    removal for `core/presence_tracker.py` — the same pure function, now fed
    by a small always-on module instead of `InterfaceLayer`."""

    THINKING = auto()
    NOTICED = auto()
    FOCUSED = auto()
    IDLE = auto()
    AWAKE = auto()


class EpisodeKind(StrEnum):
    """The kind of row in the S0 `EpisodeStore` (VISION_PHASES.md, §3.3).

    A row is either a **span** (`t_start`/`t_end` set, `t_valid`/`t_invalid`
    null) or a **fact** (`t_valid`/`t_invalid` set, `t_start`/`t_end` null) —
    `core/episodes.py`'s `record_span` / `assert_fact` write one or the other,
    never both. `TOPIC_FACT` / `PROJECT_STATE_FACT` are written from A2 / S2
    respectively; defined now so the closed set never needs a schema bump when
    those writers land (the same reasoning as `EventType.MOMENT_DELIVERED`)."""

    FOCUS_SPAN = auto()
    IDLE_SPAN = auto()
    INSIGHT = auto()
    MOMENT_DELIVERED = auto()
    MOMENT_FEEDBACK = auto()
    TOPIC_FACT = auto()
    PROJECT_STATE_FACT = auto()
    # A3 · one fact per (moment kind, context bucket) arm, replaced on every
    # update (`assert_fact` closes the old one) so the guardian's Beta(a, b)
    # posteriors survive a daemon restart. `object` is a small JSON blob
    # ({"a", "b", "n", "updated_at"}); `subject` is `guardian:<kind>:<bucket>`.
    GUARDIAN_POSTERIOR = auto()


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
    # B6 · L6 (D-13). A cached "idle thought" — an extractive follow-up question
    # the DMN generated during idle, grounded in real nodes. `idle:<uuid>`; edged
    # `RELATED_TO` its cited nodes; surfaced once by L9; pruned after the 48 h TTL.
    IDLE_THOUGHT = auto()
    # B14 · a focused browser tab identified against the webapp allowlist.
    # `webapp:<slug>` (e.g. `webapp:gmail`); `PART_OF` its browser `app:` node
    # and `PART_OF` its routing domain; `access_count` is the focus count.
    # Enum add => graph schema v4 (forward-incompatible with a v3 reader).
    WEBAPP = auto()


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
    # B13-B4 (D-19(e)). A memory-heavy app group first crossed the census
    # threshold — "a big app appeared" (a VM, Blender, Docker came up). Distinct
    # from HIGH_LOAD: a transient CPU episode vs a working-set change, different
    # confidence math, different node attribution. Enum add => schema v3 bump.
    WORKING_SET_CHANGE = auto()


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


# gen-ref: aaa10008
