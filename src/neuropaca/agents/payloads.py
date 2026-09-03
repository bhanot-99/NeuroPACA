"""L8 event payloads (Architecture.md §13, B8, D-16).

`rules.md §2`: payloads are typed dataclasses, not ad-hoc dicts. Both are frozen
for the same reason `Event` is — a subscriber must not be able to mutate what
another subscriber will also see.

Neither event has a subscriber. That is deliberate (D-16): agent state is
surfaced through `neuropaca health` only, the same call B7 made for pressure
rather than adding a view nobody asked for. The events exist so the stream is
there when something wants it, and so the agent record does not depend on the
wording of a log line.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSpawnedPayload:
    """One agent started. `trigger_node` is the graph node whose pressure
    justified it — the thread back from an agent to the behaviour that caused it."""

    agent_id: str
    trigger_node: str


@dataclass(frozen=True, slots=True)
class AgentCompletedPayload:
    """One agent finished, for any reason.

    `outcome` is a short tag — `"ok"`, `"timeout"`, `"cancelled"`, `"error"`,
    `"capped"` — and `nodes_spawned` is how many ephemeral nodes it actually
    added. Zero is a normal result, not a failure: it is what the ephemeral cap
    being full looks like.
    """

    agent_id: str
    nodes_spawned: int
    outcome: str
