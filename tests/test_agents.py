"""L8 · Agents & structural plasticity (B8, D-16, Architecture.md §11b).

The five B8 exit criteria are tested here as invariants rather than as tuned
numbers, the same way B7's were:

- the ephemeral cap holds *structurally* — it is checked under a lock before the
  mutation, so a burst larger than the cap cannot race past it;
- apoptosis reaps on age and on ephemerality, and on nothing else;
- an `ACTION_PROPOSAL` reaches L7's single `SafetyGate` and comes back, and a
  proposal L7 does not recognise is refused rather than raised;
- an over-budget agent is cancelled and still closes its own record;
- a spawn over `max_concurrent_agents` is refused, never queued.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from neuropaca.action.executor import ActionExecutor
from neuropaca.agents.payloads import AgentCompletedPayload, AgentSpawnedPayload
from neuropaca.agents.supervisor import EPHEMERAL_PREFIX, AgentSupervisor
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.errors import ConfigError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.drive.pressure import PressureEntry


@pytest.fixture(autouse=True)
async def _stop_bus():
    """Every test here drives the singleton bus. Stop its dispatch task before
    conftest's singleton wipe runs, or the reset touches a closed loop — the
    same `_teardown` discipline `test_drive.py` applies by hand."""
    yield
    bus = EventBus.get_instance()
    if bus.is_running:
        await bus.stop()


def _config(tmp_path, **over) -> Config:
    over.setdefault("action_log_path", str(tmp_path / "actions.jsonl"))
    over.setdefault("quarantine_path", str(tmp_path / "quarantine"))
    return Config(inference_backend="fake", **over)


def _entry(node_id: str = "app:code", *, pressure: float = 4.0, sources=("diagnosis",)):
    now = datetime.now(UTC)
    return PressureEntry(
        node_id=node_id,
        pressure=pressure,
        reason="cpu pinned while editing",
        created_at=now,
        last_updated=now,
        sources=tuple(sources),
    )


async def _supervisor(tmp_path, *, clock=None, **over):
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    await graph.add_node("app:code", NodeType.APP, {"label": "code"})
    agents = AgentSupervisor(
        bus, _config(tmp_path, **over), graph, clock=clock or FakeClock(wall=datetime.now(UTC))
    )
    await agents.initialize()
    await agents.start()
    return agents, bus, graph


async def _executor(tmp_path, bus, graph, **over) -> ActionExecutor:
    action = ActionExecutor(bus, _config(tmp_path, **over), graph)
    await action.initialize()
    await action.start()
    return action


def _collect(bus: EventBus, event_type: EventType) -> list[Event]:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe(event_type, handler)
    return seen


async def _settle(bus: EventBus, *tasks) -> None:
    await bus.join()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await bus.join()


# ------------------------------------------------------------------ criterion 1


async def test_spawn_node_never_exceeds_the_ephemeral_cap(tmp_path) -> None:
    agents, _bus, _graph = await _supervisor(tmp_path, max_ephemeral_nodes=5)

    created = [
        await agents.spawn_node(f"n{i}", trigger_node="app:code", facet="probe") for i in range(20)
    ]

    assert sum(1 for c in created if c is not None) == 5
    assert created[5] is None, "the 6th spawn is refused, not raised"
    assert agents._count_ephemeral() == 5
    await agents.stop()


async def test_the_cap_holds_under_a_concurrent_burst(tmp_path) -> None:
    """The check-then-create pair sits inside `_spawn_lock`, so twenty coroutines
    racing at once cannot each see room below the cap and each then take it."""
    agents, _bus, _graph = await _supervisor(tmp_path, max_ephemeral_nodes=7)

    results = await asyncio.gather(
        *(agents.spawn_node(f"n{i}", trigger_node="app:code") for i in range(20))
    )

    assert sum(1 for r in results if r is not None) == 7
    assert agents._count_ephemeral() == 7
    await agents.stop()


async def test_a_spawned_node_is_a_concept_edged_to_its_trigger(tmp_path) -> None:
    """No new NodeType and no schema bump (D-16): a CONCEPT marked by id prefix."""
    agents, _bus, graph = await _supervisor(tmp_path)

    node_id = await agents.spawn_node("why is code hot", trigger_node="app:code", facet="summary")

    assert node_id is not None
    assert node_id.startswith(EPHEMERAL_PREFIX)
    node = graph.get_node(node_id)
    assert node is not None
    assert node.node_type is NodeType.CONCEPT
    assert any(
        e.target_id == "app:code" and e.relation is RelationType.RELATED_TO
        for e in graph.get_edges(node_id)
    )
    await agents.stop()


async def test_the_ephemeral_marker_survives_a_save_and_reload(tmp_path) -> None:
    """The reason the id prefix is the marker and the attributes are not: node
    ids persist, ad-hoc attributes do not. Without this, a restart would make
    every ephemeral node look permanent and apoptosis would reap nothing."""
    agents, _bus, graph = await _supervisor(tmp_path)
    node_id = await agents.spawn_node("survivor", trigger_node="app:code")
    assert node_id
    await graph.save()

    reloaded = GraphMemory(tmp_path / "g.json")
    await reloaded.load()

    assert [n for n in reloaded.node_ids if n.startswith(EPHEMERAL_PREFIX)] == [node_id]
    assert reloaded.get_node(node_id) is not None
    await agents.stop()


# ------------------------------------------------------------------ criterion 2


async def test_apoptosis_reaps_past_the_ttl_and_spares_everything_else(tmp_path) -> None:
    clock = FakeClock(wall=datetime.now(UTC))
    agents, _bus, graph = await _supervisor(tmp_path, clock=clock, agent_idle_ttl_days=14)

    old_id = await agents.spawn_node("old", trigger_node="app:code")
    fresh_id = await agents.spawn_node("fresh", trigger_node="app:code")
    assert old_id and fresh_id
    await graph.update_node(old_id, {"created_at": clock.now() - timedelta(days=15)})

    reaped = await agents.apoptosis()

    assert reaped == 1
    assert graph.get_node(old_id) is None
    assert graph.get_node(fresh_id) is not None, "a node inside its TTL is left alone"
    assert graph.get_node("app:code") is not None, "a non-ephemeral node is never touched"
    await agents.stop()


async def test_apoptosis_leaves_no_dangling_edges(tmp_path) -> None:
    clock = FakeClock(wall=datetime.now(UTC))
    agents, _bus, graph = await _supervisor(tmp_path, clock=clock)

    node_id = await agents.spawn_node("doomed", trigger_node="app:code")
    assert node_id
    await graph.update_node(node_id, {"created_at": clock.now() - timedelta(days=30)})
    assert graph.get_edges("app:code"), "precondition: the trigger carries the agent's edge"
    edges_before = graph.edge_count

    await agents.apoptosis()

    assert graph.get_node(node_id) is None
    assert graph.edge_count == edges_before - 1
    for edge in graph.get_edges("app:code"):
        assert node_id not in (edge.source_id, edge.target_id)
    # Every surviving edge still has both endpoints in the graph.
    for survivor in graph.node_ids:
        for edge in graph.get_edges(survivor):
            assert graph.get_node(edge.source_id) is not None
            assert graph.get_node(edge.target_id) is not None
    await agents.stop()


async def test_kill_node_refuses_a_non_ephemeral_node(tmp_path) -> None:
    """L8 reaps what it grew and nothing else — enforced, not merely documented."""
    agents, _bus, graph = await _supervisor(tmp_path)

    assert await agents.kill_node("app:code") is False
    assert graph.get_node("app:code") is not None
    await agents.stop()


async def test_apoptosis_runs_on_boot(tmp_path) -> None:
    """An ephemeral node outlives the process that made it, so a daemon that was
    down past a TTL still reaps on the way up."""
    clock = FakeClock(wall=datetime.now(UTC))
    agents, _bus, graph = await _supervisor(tmp_path, clock=clock)
    node_id = await agents.spawn_node("stale", trigger_node="app:code")
    assert node_id
    await graph.update_node(node_id, {"created_at": clock.now() - timedelta(days=99)})
    await agents.stop()

    restarted = AgentSupervisor(EventBus.get_instance(), _config(tmp_path), graph, clock=clock)
    await restarted.initialize()
    await restarted.start()

    assert graph.get_node(node_id) is None
    await restarted.stop()


# ------------------------------------------------------------------ criterion 3


async def test_an_action_proposal_routes_through_l7s_single_gate(tmp_path) -> None:
    agents, bus, graph = await _supervisor(tmp_path)
    action = await _executor(tmp_path, bus, graph, action_dry_run=False)
    results = _collect(bus, EventType.ACTION_PROPOSAL_RESULT)

    proposal_id = agents.propose_action(
        "memory_write",
        {
            "node_id": "concept:agent-finding",
            "node_type": "concept",
            "attributes": {"label": "agent finding"},
            "edges": [["app:code", "related_to"]],
        },
        reason="the agent found something worth keeping",
        trigger="test",
    )
    await bus.join()
    await _settle(bus, *action._tasks)

    assert len(results) == 1
    payload = results[0].payload
    assert payload["proposal_id"] == proposal_id
    assert payload["accepted"] is True
    assert payload["ok"] is True
    # The effect landed through L7's gate, not through L8.
    assert graph.get_node("concept:agent-finding") is not None
    assert action.gate.counters["executed"] == 1
    await action.stop()
    await agents.stop()


async def test_an_unknown_action_type_is_refused_and_audited(tmp_path) -> None:
    agents, bus, graph = await _supervisor(tmp_path)
    action = await _executor(tmp_path, bus, graph)
    results = _collect(bus, EventType.ACTION_PROPOSAL_RESULT)

    agents.propose_action("rm_rf", {"path": "/"}, reason="not in the registry", trigger="test")
    await _settle(bus)

    assert len(results) == 1
    assert results[0].payload["accepted"] is False
    assert "not proposable" in results[0].payload["detail"]
    assert action.gate.counters["executed"] == 0
    # Refusals are audited too — the trail has no silent gaps (rules.md §5.6).
    assert len(action.audit.path.read_text().strip().splitlines()) == 2
    await action.stop()
    await agents.stop()


async def test_a_malformed_proposal_is_refused_not_raised(tmp_path) -> None:
    agents, bus, graph = await _supervisor(tmp_path)
    action = await _executor(tmp_path, bus, graph)
    results = _collect(bus, EventType.ACTION_PROPOSAL_RESULT)

    agents.propose_action("memory_write", {}, reason="missing every kwarg", trigger="test")
    await _settle(bus)

    assert results and results[0].payload["accepted"] is False
    assert "malformed" in results[0].payload["detail"]
    assert action.health().ok is False or action._errors == 1
    await action.stop()
    await agents.stop()


async def test_a_dangerous_proposal_still_obeys_the_tier_gate(tmp_path) -> None:
    """L8 gets no privileged path: a proposal faces the same tier gate every
    other action does (rules.md §5.2, D-15). Default tiers are ["safe"]."""
    agents, bus, graph = await _supervisor(tmp_path)
    action = await _executor(tmp_path, bus, graph)
    results = _collect(bus, EventType.ACTION_PROPOSAL_RESULT)

    agents.propose_action(
        "run_command", {"argv": ["/bin/true"]}, reason="agent wants a command", trigger="test"
    )
    await bus.join()
    await _settle(bus, *action._tasks)

    assert results and results[0].payload["ok"] is False
    assert action.gate.counters["executed"] == 0
    await action.stop()
    await agents.stop()


# ------------------------------------------------------------------ criterion 4


async def test_an_agent_over_its_wall_clock_budget_is_cancelled_cleanly(tmp_path) -> None:
    agents, bus, graph = await _supervisor(tmp_path, agent_wall_clock_budget_seconds=1)
    completed = _collect(bus, EventType.AGENT_COMPLETED)

    async def _slow(_entry):
        await asyncio.sleep(5)
        return 99

    agents._grow_subcluster = _slow  # type: ignore[method-assign]

    bus.publish(
        Event(
            event_type=EventType.PRESSURE_THRESHOLD_REACHED,
            source="drive",
            payload={"entry": _entry(), "tier": "high"},
        )
    )
    await bus.join()
    await _settle(bus, *list(agents._agents.values()))

    assert len(completed) == 1
    payload = completed[0].payload["payload"]
    assert isinstance(payload, AgentCompletedPayload)
    assert payload.outcome == "timeout"
    assert agents.health().ok, "an over-budget agent is reported, not fatal"
    assert graph.get_node("app:code") is not None, "the graph is left consistent"
    await agents.stop()


# ------------------------------------------------------------------ criterion 5


async def test_a_spawn_over_the_concurrency_cap_is_refused_never_queued(tmp_path) -> None:
    agents, bus, _graph = await _supervisor(
        tmp_path, max_concurrent_agents=1, agent_wall_clock_budget_seconds=5
    )
    spawned = _collect(bus, EventType.AGENT_SPAWNED)
    release = asyncio.Event()

    async def _blocked(_entry):
        await release.wait()
        return 1

    agents._grow_subcluster = _blocked  # type: ignore[method-assign]

    for _ in range(5):
        bus.publish(
            Event(
                event_type=EventType.PRESSURE_THRESHOLD_REACHED,
                source="drive",
                payload={"entry": _entry(), "tier": "high"},
            )
        )
    await bus.join()

    assert len(spawned) == 1, "only the first crossed the cap"
    assert agents._refused == 4
    assert len(agents._agents) == 1

    release.set()
    await _settle(bus, *list(agents._agents.values()))

    # The four refused agents are gone, not queued behind the first.
    assert len(spawned) == 1
    assert agents._agents == {}
    await agents.stop()


# -------------------------------------------------------------------- wiring


async def test_an_agent_grows_a_subcluster_from_a_real_pressure_crossing(tmp_path) -> None:
    agents, bus, _graph = await _supervisor(tmp_path)
    spawned = _collect(bus, EventType.AGENT_SPAWNED)
    completed = _collect(bus, EventType.AGENT_COMPLETED)

    bus.publish(
        Event(
            event_type=EventType.PRESSURE_THRESHOLD_REACHED,
            source="drive",
            payload={"entry": _entry(sources=("diagnosis", "learning")), "tier": "high"},
        )
    )
    await bus.join()
    await _settle(bus, *list(agents._agents.values()))

    assert isinstance(spawned[0].payload["payload"], AgentSpawnedPayload)
    assert spawned[0].payload["payload"].trigger_node == "app:code"
    done = completed[0].payload["payload"]
    assert done.outcome == "ok"
    assert done.nodes_spawned == 3, "one summary node plus one per corroborating source"
    assert agents._count_ephemeral() == 3
    await agents.stop()


async def test_an_agent_reports_capped_when_the_graph_is_full(tmp_path) -> None:
    agents, bus, _graph = await _supervisor(tmp_path, max_ephemeral_nodes=1)
    completed = _collect(bus, EventType.AGENT_COMPLETED)
    assert await agents.spawn_node("filler", trigger_node="app:code") is not None

    bus.publish(
        Event(
            event_type=EventType.PRESSURE_THRESHOLD_REACHED,
            source="drive",
            payload={"entry": _entry(), "tier": "high"},
        )
    )
    await bus.join()
    await _settle(bus, *list(agents._agents.values()))

    assert completed[0].payload["payload"].outcome == "capped"
    assert completed[0].payload["payload"].nodes_spawned == 0
    assert agents._count_ephemeral() == 1
    await agents.stop()


async def test_agents_disabled_is_a_real_kill_switch(tmp_path) -> None:
    agents, bus, _graph = await _supervisor(tmp_path, agents_enabled=False)
    spawned = _collect(bus, EventType.AGENT_SPAWNED)

    bus.publish(
        Event(
            event_type=EventType.PRESSURE_THRESHOLD_REACHED,
            source="drive",
            payload={"entry": _entry(), "tier": "high"},
        )
    )
    await _settle(bus)

    assert spawned == []
    assert agents.health().detail == "disabled"
    await agents.stop()


async def test_stop_cancels_a_running_agent_and_closes_its_record(tmp_path) -> None:
    agents, bus, _graph = await _supervisor(tmp_path, agent_wall_clock_budget_seconds=30)
    completed = _collect(bus, EventType.AGENT_COMPLETED)

    async def _blocked(_entry):
        await asyncio.Event().wait()
        return 0

    agents._grow_subcluster = _blocked  # type: ignore[method-assign]

    bus.publish(
        Event(
            event_type=EventType.PRESSURE_THRESHOLD_REACHED,
            source="drive",
            payload={"entry": _entry(), "tier": "high"},
        )
    )
    await bus.join()
    assert len(agents._agents) == 1

    await agents.stop()
    await bus.join()

    assert agents._agents == {}
    assert completed and completed[0].payload["payload"].outcome == "cancelled"


async def test_health_is_the_only_agent_surface(tmp_path) -> None:
    """D-16: no L9 view, no new op — agent state lives in `neuropaca health`."""
    agents, _bus, _graph = await _supervisor(tmp_path)

    health = agents.health()

    assert health.name == "agents"
    assert health.ok
    assert "0/2 running" in health.detail
    assert "spawned" in health.detail and "reaped" in health.detail
    await agents.stop()


@pytest.mark.parametrize(
    "bad",
    [
        {"agent_wall_clock_budget_seconds": 0},
        {"agent_idle_ttl_days": 0},
        {"max_ephemeral_nodes": 0},
        {"agent_inference_budget": -1},
    ],
)
def test_config_refuses_an_unusable_agent_budget(bad) -> None:
    with pytest.raises(ConfigError):
        Config(inference_backend="fake", **bad)
