"""B1 · NeuroPACAOrchestrator — daemon lifecycle and the B1 exit criteria
(Architecture.md §10, phases.md B1).

Skips until `orchestration/orchestrator.py` exists; it lands with these tests in
one commit (rules.md §8).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("neuropaca.orchestration.orchestrator")

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.models import Event
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "graph.json"),
        action_log_path=str(tmp_path / "actions.jsonl"),
        graph_save_interval_seconds=3600,  # keep the scheduler out of the way
    )


async def test_initialize_then_start_reaches_running_idle(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()
    assert orch.is_running is True

    # An idle loop with no modules is still a live loop: it stays healthy across
    # several iterations and keeps a graph.
    for _ in range(5):
        await asyncio.sleep(0)
    assert orch.health_check().ok is True
    assert orch.graph_memory.node_count == 11  # the seeded hubs

    await orch.stop()
    assert orch.is_running is False


async def test_clean_shutdown_saves_the_graph_exactly_once(config: Config, monkeypatch) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()

    saves = 0
    real_save = orch.graph_memory.save

    async def counting_save() -> None:
        nonlocal saves
        saves += 1
        await real_save()

    monkeypatch.setattr(orch.graph_memory, "save", counting_save)

    await orch.request_shutdown()  # what the SIGTERM handler calls

    assert saves == 1
    assert orch.is_running is False


async def test_sigterm_handler_ends_a_running_daemon(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    run_task = asyncio.create_task(orch.run())
    await asyncio.sleep(0)
    assert orch.is_running is True

    orch.request_shutdown_nowait()  # sync entry point a real signal handler uses
    await asyncio.wait_for(run_task, timeout=2.0)
    assert orch.is_running is False


async def test_shutdown_drains_queued_events_before_stopping(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()

    seen: list[Event] = []

    async def slow_handler(ev: Event) -> None:
        await asyncio.sleep(0)
        seen.append(ev)

    orch.event_bus.subscribe(EventType.USER_MESSAGE, slow_handler)
    for _ in range(10):
        orch.event_bus.publish(Event(event_type=EventType.USER_MESSAGE, source="test"))

    await orch.request_shutdown()
    assert len(seen) == 10  # queue drained, not discarded


async def test_health_check_never_raises_at_any_lifecycle_stage(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    assert orch.health_check().ok in (True, False)  # before initialize
    await orch.initialize()
    orch.health_check()
    await orch.start()
    orch.health_check()
    await orch.stop()
    assert orch.health_check().ok is False  # stopped daemon is not healthy


async def test_stop_is_idempotent(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()
    await orch.stop()
    await orch.stop()  # must not raise
    assert orch.is_running is False
