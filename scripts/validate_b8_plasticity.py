#!/usr/bin/env python3
"""B8 · All five exit criteria — agents & structural plasticity (phases.md B8, D-16).

Runs the real `AgentSupervisor` and the real `ActionExecutor` against the real
`EventBus` and `GraphMemory` on the **real clock**, in a throwaway directory.

**Why synthetic injection rather than a soak.** B7 tried the soak route three
times and logged zero proposals every time: under `systemd --user` the Wayland
activity collector self-disables (no `$WAYLAND_DISPLAY`), so the only pressure
path a headless daemon can reach is `HighLoadPattern`, and a normal day does not
pin the CPU above 90 % for five minutes while touching a watched path. An empty
log cannot distinguish a correct-quiet pipeline from a broken one. So this
harness publishes `PRESSURE_THRESHOLD_REACHED` directly with a synthetic
`PressureEntry` — the same shape L5 publishes, at the same thresholds — which is
the B7 positive-control pattern applied from the outset rather than as a rescue.

What is real here: the supervisor, the executor, the single `SafetyGate`, the
audit log, the graph, and every cap and budget in `Config`. What is synthetic:
the pressure entry that starts each run, the backdating of `created_at` used to
reach a 14-day TTL in bounded time, and one deliberately slow sub-cluster used to
provoke the wall-clock budget. Each is called out at its use site.

    uv run python scripts/validate_b8_plasticity.py

Exit 0 = all five criteria pass. Exit 1 = any failure.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.action.executor import ActionExecutor
from neuropaca.agents.supervisor import EPHEMERAL_PREFIX, AgentSupervisor
from neuropaca.core import logging as np_logging
from neuropaca.core.clock import SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.drive.pressure import PressureEntry

_NODE = "app:webpack"
_CAP = 12  # deliberately small so a burst can be seen to hit it
_BURST = 60  # five times the cap
_TTL_DAYS = 14  # the production default — not shortened for the harness


def _entry(node_id: str = _NODE, *, sources: tuple[str, ...] = ("diagnosis", "learning")):
    """The shape L5 publishes on a high-tier crossing. Synthetic — see the module docstring."""
    now = datetime.now(UTC)
    return PressureEntry(
        node_id=node_id,
        pressure=4.2,
        reason="cpu pinned at 100% for 5 samples",
        created_at=now,
        last_updated=now,
        sources=sources,
    )


def _crossing(entry: PressureEntry) -> Event:
    return Event(
        event_type=EventType.PRESSURE_THRESHOLD_REACHED,
        source="drive",
        payload={"entry": entry, "tier": "high"},
    )


def _spy(sink: list[Event]):
    """`EventBus` subscribers are coroutines — this keeps the spies one-liners."""

    async def _handler(event: Event) -> None:
        sink.append(event)

    return _handler


async def _drain(bus: EventBus, agents: AgentSupervisor) -> None:
    await bus.join()
    running = [t for t in agents._agents.values() if not t.done()]
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    await bus.join()


async def _main() -> int:
    np_logging.configure("WARNING")
    tmp = Path(tempfile.mkdtemp(prefix="np-b8-plasticity-"))
    fails: list[str] = []

    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp / "graph.json"))
    await graph.load()
    await graph.add_node(_NODE, NodeType.APP, {"label": "webpack"})

    config = Config(
        inference_backend="fake",
        graph_db_path=str(tmp / "graph.json"),
        action_log_path=str(tmp / "actions.jsonl"),
        quarantine_path=str(tmp / "quarantine"),
        max_ephemeral_nodes=_CAP,
        agent_idle_ttl_days=_TTL_DAYS,
        max_concurrent_agents=1,
        agent_wall_clock_budget_seconds=1,
        # The gate must be live for criterion 3 — a dry run would prove routing
        # but not execution. Safe tier only, so no confirmation is bypassed.
        action_dry_run=False,
    )

    agents = AgentSupervisor(bus, config, graph, clock=SystemClock())
    await agents.initialize()
    await agents.start()
    action = ActionExecutor(bus, config, graph)
    await action.initialize()
    await action.start()

    spawned: list[Event] = []
    completed: list[Event] = []
    results: list[Event] = []
    bus.subscribe(EventType.AGENT_SPAWNED, _spy(spawned))
    bus.subscribe(EventType.AGENT_COMPLETED, _spy(completed))
    bus.subscribe(EventType.ACTION_PROPOSAL_RESULT, _spy(results))

    print(f"=== B8 · plasticity validation ({tmp}) ===\n")

    # --- 1. the hard cap on spawn_node -------------------------------------
    t0 = time.perf_counter()
    created = [
        await agents.spawn_node(f"probe {i}", trigger_node=_NODE, facet="probe")
        for i in range(_BURST)
    ]
    burst_ms = (time.perf_counter() - t0) * 1000
    granted = sum(1 for c in created if c is not None)
    live = agents._count_ephemeral()
    print(
        f"1. cap        {_BURST} spawns against a cap of {_CAP} -> {granted} granted, "
        f"{live} live ({burst_ms:.1f} ms)"
    )
    if granted != _CAP or live != _CAP:
        fails.append(f"cap breached: {granted} granted / {live} live, expected {_CAP}")

    # The cap must also hold when the burst is concurrent — the count-then-create
    # pair is what a naive implementation gets wrong.
    for node_id in agents._ephemeral_ids():
        await graph.delete_node(node_id)
    concurrent = await asyncio.gather(
        *(agents.spawn_node(f"race {i}", trigger_node=_NODE) for i in range(_BURST))
    )
    granted_concurrent = sum(1 for c in concurrent if c is not None)
    print(f"   concurrent {_BURST} simultaneous spawns -> {granted_concurrent} granted")
    if granted_concurrent != _CAP:
        fails.append(f"cap raced: {granted_concurrent} granted concurrently, expected {_CAP}")

    # --- 2. apoptosis -------------------------------------------------------
    ephemeral_before = agents._ephemeral_ids()
    edges_before = graph.edge_count
    nodes_before = graph.node_count
    # Synthetic: `created_at` is backdated past the *production* 14-day TTL so the
    # real constant is exercised without waiting two weeks. Half the nodes are
    # aged and half left fresh, so the sweep has to discriminate rather than
    # simply delete everything it owns.
    aged = ephemeral_before[: len(ephemeral_before) // 2]
    fresh = ephemeral_before[len(ephemeral_before) // 2 :]
    cutoff = datetime.now(UTC) - timedelta(days=_TTL_DAYS + 1)
    for node_id in aged:
        await graph.update_node(node_id, {"created_at": cutoff})

    reaped = await agents.apoptosis()

    survivors = agents._ephemeral_ids()
    dangling = [
        (e.source_id, e.target_id)
        for n in graph.node_ids
        for e in graph.get_edges(n)
        if graph.get_node(e.source_id) is None or graph.get_node(e.target_id) is None
    ]
    print(
        f"2. apoptosis  {len(aged)} aged past {_TTL_DAYS}d / {len(fresh)} fresh -> "
        f"{reaped} reaped, {len(survivors)} survive, "
        f"{graph.edge_count}/{edges_before} edges, {len(dangling)} dangling"
    )
    if reaped != len(aged):
        fails.append(f"apoptosis reaped {reaped}, expected {len(aged)}")
    if sorted(survivors) != sorted(fresh):
        fails.append("apoptosis reaped the wrong nodes — a fresh node was taken")
    if dangling:
        fails.append(f"apoptosis left {len(dangling)} dangling edge(s): {dangling[:3]}")
    if graph.get_node(_NODE) is None:
        fails.append("apoptosis deleted a non-ephemeral node")
    if graph.edge_count != edges_before - len(aged):
        fails.append(
            f"edge count is {graph.edge_count}, expected {edges_before - len(aged)} "
            "(one edge per reaped node)"
        )
    if graph.node_count != nodes_before - len(aged):
        fails.append(f"node count is {graph.node_count}, expected {nodes_before - len(aged)}")

    # A non-ephemeral node is refused outright — L8 reaps only what it grew.
    if await agents.kill_node(_NODE) is not False or graph.get_node(_NODE) is None:
        fails.append("kill_node did not refuse a non-ephemeral node")

    # The marker has to survive a restart, or apoptosis silently stops working.
    await graph.save()
    reloaded = GraphMemory(tmp / "graph.json")
    await reloaded.load()
    persisted = [n for n in reloaded.node_ids if n.startswith(EPHEMERAL_PREFIX)]
    print(f"   restart    {len(persisted)}/{len(survivors)} ephemeral nodes still identifiable")
    if sorted(persisted) != sorted(survivors):
        fails.append("the ephemeral marker did not survive a save/load — apoptosis would stall")

    # --- 3. ACTION_PROPOSAL routes to L7 ------------------------------------
    proposal_id = agents.propose_action(
        "memory_write",
        {
            "node_id": "concept:agent-finding",
            "node_type": "concept",
            "attributes": {"label": "webpack is the hot node"},
            "edges": [[_NODE, "related_to"]],
        },
        reason="the agent found something worth keeping",
        trigger="b8-validation",
    )
    await bus.join()
    if action._tasks:
        await asyncio.gather(*list(action._tasks), return_exceptions=True)
    await bus.join()

    executed = action.gate.counters["executed"]
    landed = graph.get_node("concept:agent-finding") is not None
    ok_result = next((r for r in results if r.payload["proposal_id"] == proposal_id), None)
    print(
        f"3. proposal   {proposal_id} -> gate executed={executed}, node written={landed}, "
        f"result={'ok' if ok_result and ok_result.payload['ok'] else 'MISSING'}"
    )
    if executed != 1:
        fails.append(f"the gate executed {executed} actions, expected exactly 1")
    if not landed:
        fails.append("the proposed memory_write did not reach the graph")
    if ok_result is None or not ok_result.payload["ok"]:
        fails.append("no successful ACTION_PROPOSAL_RESULT came back to L8")

    # An unrecognised action_type is refused, not raised, and still audited.
    results.clear()
    agents.propose_action("rm_rf", {"path": "/"}, reason="not in the registry", trigger="b8")
    await bus.join()
    refused = results[0].payload if results else {}
    print(
        f"   refusal    unknown action_type -> accepted={refused.get('accepted')}, "
        f"detail={refused.get('detail', '')[:48]!r}"
    )
    if refused.get("accepted") is not False:
        fails.append("an unknown action_type was not refused")
    if action.gate.counters["executed"] != 1:
        fails.append("a refused proposal still reached the gate")

    audit_lines = Path(config.action_log_path).read_text().strip().splitlines()
    print(f"   audit      {len(audit_lines)} lines (attempt+result for the run and the refusal)")
    if len(audit_lines) != 4:
        fails.append(f"audit has {len(audit_lines)} lines, expected 4")

    # --- 4. the wall-clock budget -------------------------------------------
    completed.clear()
    original = agents._grow_subcluster

    async def _slow(_entry):  # deliberately over the 1 s budget
        await asyncio.sleep(5)
        return 99

    agents._grow_subcluster = _slow  # type: ignore[method-assign]
    t0 = time.perf_counter()
    bus.publish(_crossing(_entry()))
    await _drain(bus, agents)
    budget_elapsed = time.perf_counter() - t0
    agents._grow_subcluster = original  # type: ignore[method-assign]

    outcome = completed[0].payload["payload"].outcome if completed else "MISSING"
    print(
        f"4. budget     agent abandoned after {budget_elapsed:.2f}s "
        f"(budget {config.agent_wall_clock_budget_seconds}s) -> outcome={outcome!r}, "
        f"health ok={agents.health().ok}"
    )
    if outcome != "timeout":
        fails.append(f"an over-budget agent reported {outcome!r}, expected 'timeout'")
    if budget_elapsed > 3:
        fails.append(f"the budget did not bite — {budget_elapsed:.2f}s for a 1 s ceiling")
    if not agents.health().ok:
        fails.append("an over-budget agent degraded the module — it should be reported, not fatal")

    # --- 5. the concurrency cap ---------------------------------------------
    spawned.clear()
    completed.clear()
    release = asyncio.Event()

    async def _blocked(_entry):
        await release.wait()
        return 1

    agents._grow_subcluster = _blocked  # type: ignore[method-assign]
    refused_before = agents._refused
    for _ in range(_BURST):
        bus.publish(_crossing(_entry()))
    await bus.join()
    concurrent_agents = len(agents._agents)
    refused_now = agents._refused - refused_before
    release.set()
    await _drain(bus, agents)
    agents._grow_subcluster = original  # type: ignore[method-assign]

    print(
        f"5. concurrency {_BURST} crossings against a cap of {config.max_concurrent_agents} -> "
        f"{len(spawned)} spawned, {refused_now} refused, {concurrent_agents} in flight"
    )
    if len(spawned) != config.max_concurrent_agents:
        fails.append(
            f"{len(spawned)} agents spawned against a cap of {config.max_concurrent_agents}"
        )
    if refused_now != _BURST - config.max_concurrent_agents:
        fails.append(f"{refused_now} refusals, expected {_BURST - config.max_concurrent_agents}")

    await action.stop()
    await agents.stop()
    await bus.stop()

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(
        f"=== RESULT (PASS) — cap held at {_CAP} under a {_BURST}-spawn burst (serial and "
        f"concurrent); apoptosis reaped {reaped} past {_TTL_DAYS}d with 0 dangling edges and "
        f"survived a reload; ACTION_PROPOSAL executed through L7's one gate and an unknown type "
        f"was refused; a 1 s budget bit at {budget_elapsed:.2f}s; {refused_now} over-cap agents "
        f"refused, none queued ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
