# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L8 · `AgentSupervisor` — bounded agents and structural plasticity
(Architecture.md §11b, B8, D-15/D-16).

It subscribes `PRESSURE_THRESHOLD_REACHED` — the *same* signal L7 consumes, read
for a different question. L7 asks "what single effect does this justify?"; L8
asks "does this justify a bounded investigation?" and answers it by growing a
small, temporary piece of graph around the node under pressure, then reaping it
later. That is the whole of B8: **structural plasticity, no inference**.

Why no inference (D-16). Pressure crosses precisely when the machine is busy, and
`rules.md §4` allows exactly one inference system-wide. An agent contending for
`_inference_lock` at that moment would starve L4 and the user's own `$?` query —
the two things that matter most under load. `Config.agent_inference_budget` is
declared and deliberately unspent, so a later phase cannot quietly add inference
without also declaring a budget for it.

**L8 holds no `SafetyGate`.** The gate, the single audit writer, and the single
`ConfirmationBroker` all live in L7. An agent that wants an *effect* publishes
`ACTION_PROPOSAL` — a description, never a live object — and L7 gates it and
answers on `ACTION_PROPOSAL_RESULT` (`propose_action` below). L8 therefore has no
privileged path to the filesystem, to a process, or past a confirmation, exactly
as D-15 required.

**Bounded by construction.** Three independent caps, none of them advisory:

- `max_concurrent_agents` — a spawn over the cap is **refused and logged, never
  queued**. An unbounded queue is how one load spike becomes a thundering herd.
- `agent_wall_clock_budget_seconds` — every agent body runs inside
  `asyncio.timeout`; an overrun is cancelled and reported, never fatal.
- `max_ephemeral_nodes` — checked *before* the graph is mutated, under this
  module's own lock so the count-then-create pair cannot interleave with another
  agent (`rules.md §3`: a code path that can add nodes without bound is wrong).

**How an ephemeral node is recognised — a deviation from D-16 worth knowing.**
D-16 called for `is_ephemeral` / `spawned_by` in the attributes dict, to avoid a
graph-schema bump. They cannot be the durable marker:
`GraphMemory._add_node_unsafe` builds its node data from the fixed `Node` field
set, and `_node_record` / `_deserialise` serialise only those fields, so an extra
key is dropped at creation and again on every `save()`. Selecting on it would
mean that after one daemon restart every ephemeral node looked permanent and
apoptosis reaped nothing — unbounded growth, silently. So the id prefix
(`ephemeral:`) is the marker, and the two attributes are no longer written at
all: nothing ever read them back, and setting them cost a second `_lock` cycle
and a `_dirty` flip per spawned node to store something `save()` throws away.

So the load-bearing marker is the **node id prefix** `ephemeral:`, which persists
because ids do, and which follows the convention already used for exactly this
purpose (`idle:<uuid>` for B6's 48 h thought cache, `insight:`, `action:`).
Together with the persisted `created_at`, apoptosis is fully restart-durable with
**no schema change** — D-16's actual goal. The attributes remain as in-process
introspection and are documented as such at the call site.

Apoptosis is L8's own job, not `prune_stale_nodes()` (D-16). That call is a
whole-graph sweep on one global TTL and L6 already drives it at 48 h; a second
caller at 14 d would have two layers fighting over one knob. L8 selects its own
nodes and calls `GraphMemory.delete_node()` — still the public API, still one
lock cycle per mutation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from neuropaca.agents.payloads import AgentCompletedPayload, AgentSpawnedPayload
from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.labels import KIND_PREFIX, LabelKind, LabelSpec
from neuropaca.core.models import Event, system_error_event
from neuropaca.drive.pressure import PressureEntry

_log = logging.getLogger(__name__)

#: The durable ephemerality marker. Node ids are persisted; ad-hoc node
#: attributes are not (see the module docstring). Same convention as B6's
#: `idle:` — both now come from the one table in `core/labels.py` (B18).
EPHEMERAL_PREFIX = KIND_PREFIX[LabelKind.PROBE]

_CAUSE_RE = re.compile(r"^(L\d+)\s+(\w+)")


def _cause(reason: str) -> str:
    """`"L4 anomaly (idle)"` -> `"L4.anomaly"`: the layer and cause of the
    pressure, as a closed-vocabulary facet — never the free reason text."""
    m = _CAUSE_RE.match(reason)
    return f"{m[1]}.{m[2]}" if m else "other"


#: Whatever a sub-cluster describes, it is a handful of nodes. This is the
#: per-agent ceiling; `max_ephemeral_nodes` is the graph-wide one.
_SUBCLUSTER_MAX = 4


class AgentSupervisor(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("agents", event_bus, config)
        self._graph = graph_memory
        self._clock: Clock = clock or SystemClock()
        self._agents: dict[str, asyncio.Task[None]] = {}
        # Guards the count-then-create pair in `spawn_node` so two concurrent
        # agents cannot both read a count below the cap and both then create.
        self._spawn_lock = asyncio.Lock()
        self._spawned = 0
        self._completed = 0
        self._refused = 0
        self._reaped = 0
        self._nodes_created = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, self.on_pressure_threshold)

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        # Sweep on boot: ephemeral nodes outlive the process that made them, so
        # a daemon that was down past a TTL still reaps on the way up.
        reaped = await self.apoptosis()
        if reaped:
            _log.info("L8 reaped %d ephemeral node(s) at start", reaped)

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.PRESSURE_THRESHOLD_REACHED, self.on_pressure_threshold)
        for task in list(self._agents.values()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._agents.clear()

    def health(self) -> ModuleHealth:
        """D-16: this is the *only* surface for agent state — no L9 view, the same
        call B7 made for pressure. Cached counters only, no lock, no await."""
        if not self.config.agents_enabled:
            return ModuleHealth(name=self.name, ok=True, detail="disabled")
        detail = (
            f"{len(self._agents)}/{self.config.max_concurrent_agents} running · "
            f"{self._spawned} spawned · {self._completed} completed · "
            f"{self._refused} refused · {self._nodes_created} nodes · "
            f"{self._reaped} reaped"
        )
        return ModuleHealth(
            name=self.name,
            ok=self._errors == 0,
            detail=detail if self._errors == 0 else f"{detail} · {self._errors} errors",
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_pressure_threshold(self, event: Event) -> None:
        """A node got hot. Decide whether to investigate it, and refuse cleanly
        when the concurrency cap says no."""
        try:
            if not self.config.agents_enabled or not self.is_running:
                return
            entry = event.payload.get("entry")
            if not isinstance(entry, PressureEntry):
                return

            if len(self._agents) >= self.config.max_concurrent_agents:
                # Refused, not queued (D-16). The pressure that justified this
                # agent decays on its own; a backlog would not.
                self._refused += 1
                _log.info(
                    "L8 refused an agent for %s — %d already running (cap %d)",
                    entry.node_id,
                    len(self._agents),
                    self.config.max_concurrent_agents,
                )
                return

            agent_id = f"agent:{uuid4().hex[:12]}"
            self._spawned += 1
            self._last_at = event.timestamp
            self.event_bus.publish(
                Event(
                    event_type=EventType.AGENT_SPAWNED,
                    source="agents",
                    payload={
                        "payload": AgentSpawnedPayload(
                            agent_id=agent_id, trigger_node=entry.node_id
                        )
                    },
                )
            )
            task = asyncio.create_task(self._run_agent(agent_id, entry))
            self._agents[agent_id] = task
            # A plain closure, not a default-arg lambda: `agent_id` is a fresh
            # local per handler call rather than a loop variable, so there is no
            # late-binding hazard to defend against — and the default argument
            # was defeating type inference on the callback for no benefit.
            task.add_done_callback(lambda _task: self._agents.pop(agent_id, None))
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._fail("on_pressure_threshold", exc)

    # ------------------------------------------------------------ agent body
    async def _run_agent(self, agent_id: str, entry: PressureEntry) -> None:
        """One bounded investigation: reap what has expired, then grow a small
        diagnostic sub-cluster around the node under pressure.

        No inference (D-16), so the only budget that binds is wall-clock. Every
        await point is cancellation-safe: `GraphMemory` takes its lock once per
        atomic mutation, so a cancellation lands between two nodes, never inside
        one (`rules.md §1`, §3).
        """
        created = 0
        outcome = "ok"
        try:
            async with asyncio.timeout(self.config.agent_wall_clock_budget_seconds):
                await self.apoptosis()
                created = await self._grow_subcluster(entry)
                if created == 0:
                    outcome = "capped"
        except asyncio.CancelledError:
            # Re-raised after the completion event so an agent cancelled by
            # `stop()` still closes its own record (rules.md §1).
            self._completed += 1
            self._publish_completed(agent_id, created, "cancelled")
            raise
        except TimeoutError:
            outcome = "timeout"
            _log.warning(
                "L8 agent %s exceeded its %ss budget — abandoned after %d node(s)",
                agent_id,
                self.config.agent_wall_clock_budget_seconds,
                created,
            )
        except Exception as exc:
            outcome = "error"
            self._fail(f"agent {agent_id}", exc)

        self._completed += 1
        self._publish_completed(agent_id, created, outcome)

    async def _grow_subcluster(self, entry: PressureEntry) -> int:
        """The investigation itself, expressed as graph structure rather than as
        text: one ephemeral node per corroborating pressure source, plus one
        summary node, each edged `RELATED_TO` the node under pressure.

        Every fact written here is one L5 already established. Nothing is
        inferred, so nothing needs grounding — which is why this is the shape of
        agent B8 ships (D-16).
        """
        facets: list[tuple[str, float | None]] = [
            (f"summary/{_cause(entry.reason)}", entry.pressure)
        ]
        facets += [(f"source/{s}", None) for s in entry.sources]

        created = 0
        for facet, value in facets[:_SUBCLUSTER_MAX]:
            node_id = await self.spawn_node(
                facet, trigger_node=entry.node_id, value=value, evidence=entry.evidence
            )
            if node_id is None:
                break  # graph-wide cap reached — stop, do not spin
            created += 1
        return created

    # --------------------------------------------------- structural plasticity
    async def spawn_node(
        self,
        facet: str,
        *,
        trigger_node: str,
        value: float | None = None,
        evidence: Sequence[str] = (),
    ) -> str | None:
        """Grow (or reinforce) one probe about `trigger_node`. Returns its id, or
        `None` if it would be new and the graph-wide cap is already reached.

        B18: a probe is a fact — `LabelSpec(PROBE, (trigger,), facet, value)` —
        written through `upsert_fact`, so pressure on Brave twice refreshes the
        one "Brave · pressure" probe instead of growing a second.

        V-8: `evidence` are the nodes that caused the pressure (an L4 `insight:`
        node). Each becomes a `CAUSED_BY` edge **from** the probe, so an
        insight's citations hang off the insight instead of every probe and
        every insight pointing flatly at the same app. It is deliberately an
        edge and not part of `spec.refs`: refs are identity, so citing the
        insight there would mint a fresh probe per insight and race the
        ephemeral cap, where the whole point of B18's fingerprint is that
        "Brave · pressure" is *one* probe that gets refreshed.

        The cap is checked **before** the mutation and under `_spawn_lock`, so two
        concurrent agents cannot both see room and both take it. A reinforcement
        needs no room. Returning `None` rather than raising is deliberate: hitting
        the cap is a normal operating state, not an error.
        """
        spec = LabelSpec(LabelKind.PROBE, (trigger_node,), facet, value)
        async with self._spawn_lock:
            if (
                self._graph.find_fact(spec) is None
                and self._count_ephemeral() >= self.config.max_ephemeral_nodes
            ):
                _log.info(
                    "L8 ephemeral cap reached (%d) — not spawning %r",
                    self.config.max_ephemeral_nodes,
                    facet,
                )
                return None
            node, created = await self._graph.upsert_fact(spec)
            if created:
                self._nodes_created += 1
        await self._cite(node.id, evidence)
        return node.id

    async def _cite(self, probe_id: str, evidence: Sequence[str]) -> None:
        """V-8 · wire `probe -CAUSED_BY-> cause` for every cause still in the
        graph. Outside `_spawn_lock` (it is the cap's lock, not the graph's) and
        skipped silently for a cause that has since been pruned — a citation of
        something gone is worse than no citation. Weight 0.0, and both endpoints
        are generated nodes, so `_edge_profile_unsafe` counts it as provenance:
        the system's own bookkeeping neither earns nor lends relevance (V-2)."""
        for cause in evidence:
            if cause != probe_id and self._graph.has_node(cause):
                await self._graph.add_edge(probe_id, cause, RelationType.CAUSED_BY, 0.0)

    async def kill_node(self, node_id: str) -> bool:
        """Delete one ephemeral node. Refuses anything that is not ephemeral —
        L8's mandate is to reap what it grew, and nothing else."""
        if not node_id.startswith(EPHEMERAL_PREFIX):
            _log.warning("L8 refused to kill non-ephemeral node %s", node_id)
            return False
        if self._graph.get_node(node_id) is None:
            return False
        # `delete_node` removes the node's incident edges with it (networkx), so
        # apoptosis cannot leave a dangling edge behind.
        await self._graph.delete_node(node_id)
        self._reaped += 1
        return True

    async def apoptosis(self) -> int:
        """Reap every ephemeral node untouched for `agent_idle_ttl_days` — keyed
        on `last_accessed` (B18), so a probe that keeps being reinforced lives on
        — and, V-3b, every probe whose subject is gone: a probe is a note *about*
        the nodes in its `spec.refs`, and once none of them exists it describes
        nothing. Left alone it would lose its only edge, be linked to `YOU` as
        an orphan, and linger to its TTL. The 14 d TTL (D-15/D-16) is unchanged
        for probes whose subject still exists; reaping stays L8's own job.

        One lock cycle per deletion with a yield between, exactly like the DMN's
        graph jobs — a cancellation lands between two deletions, never inside one
        (`rules.md §3`).
        """
        cutoff = self._clock.now() - timedelta(days=self.config.agent_idle_ttl_days)
        reaped = 0
        for node_id in self._ephemeral_ids():
            node = self._graph.get_node(node_id)
            if node is None:
                continue
            subject_gone = node.spec is not None and not any(
                self._graph.has_node(ref) for ref in node.spec.refs
            )
            if node.last_accessed > cutoff and not subject_gone:
                continue
            if await self.kill_node(node_id):
                reaped += 1
            await asyncio.sleep(0)
        return reaped

    def _ephemeral_ids(self) -> list[str]:
        """A snapshot of the ephemeral ids, taken synchronously so the list cannot
        shift under an await (`node_ids` is already a copy)."""
        return [n for n in self._graph.node_ids if n.startswith(EPHEMERAL_PREFIX)]

    def _count_ephemeral(self) -> int:
        return sum(1 for n in self._graph.node_ids if n.startswith(EPHEMERAL_PREFIX))

    # ------------------------------------------------------- action proposals
    def propose_action(
        self,
        action_type: str,
        kwargs: dict[str, Any],
        *,
        reason: str,
        trigger: str,
    ) -> str:
        """Ask L7 for an effect. Returns the `proposal_id` to match the result on.

        This is the *only* way L8 causes anything outside the graph. It publishes a
        description; L7 owns the class registry, the gate, the audit log and the
        confirmation broker (D-16). Nothing here can bypass any of them.
        """
        proposal_id = uuid4().hex[:12]
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="agents",
                payload={
                    "proposal_id": proposal_id,
                    "action_type": action_type,
                    "kwargs": kwargs,
                    "reason": reason,
                    "trigger": trigger,
                },
            )
        )
        return proposal_id

    # ---------------------------------------------------------------- helpers
    def _publish_completed(self, agent_id: str, created: int, outcome: str) -> None:
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_COMPLETED,
                source="agents",
                payload={
                    "payload": AgentCompletedPayload(
                        agent_id=agent_id, nodes_spawned=created, outcome=outcome
                    )
                },
            )
        )

    def _fail(self, where: str, exc: Exception) -> None:
        self._errors += 1
        _log.exception("agents %s failed", where)
        self.event_bus.publish(
            system_error_event(module="agents", exception=str(exc), severity="handler")
        )


# gen-ref: 6de02b39
