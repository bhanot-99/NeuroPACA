"""B1 exit criteria · `BaseModule` lifecycle conformance (Architecture.md §3.7, §10).

A minimal `NullModule` proves the orchestrator drives `initialize -> start ->
stop` in order and that `health()` is a synchronous, non-raising, instant call.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth, SystemHealth
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator


class NullModule(BaseModule):
    """Does nothing but record the order of its lifecycle calls."""

    def __init__(self, name: str, event_bus: EventBus, config: Config) -> None:
        super().__init__(name, event_bus, config)
        self.calls: list[str] = []

    async def initialize(self) -> None:
        self.calls.append("initialize")

    async def start(self) -> None:
        self.calls.append("start")
        self.is_running = True

    async def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False

    def health(self) -> ModuleHealth:
        return ModuleHealth(name=self.name, ok=True, detail="null")


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "graph.json"),
        action_log_path=str(tmp_path / "actions.jsonl"),
        graph_save_interval_seconds=3600,
    )


def test_base_module_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseModule("x", EventBus.get_instance(), Config(inference_backend="fake"))  # type: ignore[abstract]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    class Partial(BaseModule):
        async def initialize(self) -> None: ...
        async def start(self) -> None: ...

        # missing stop() and health()

    with pytest.raises(TypeError):
        Partial("p", EventBus.get_instance(), Config(inference_backend="fake"))  # type: ignore[abstract]


async def test_orchestrator_drives_lifecycle_in_order(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    module = NullModule("null", EventBus.get_instance(), config)
    orch.register_module(module)

    await orch.initialize()
    assert module.calls == ["initialize"]

    await orch.start()
    assert module.calls == ["initialize", "start"]
    assert module.is_running is True

    await orch.stop()
    assert module.calls == ["initialize", "start", "stop"]
    assert module.is_running is False


async def test_register_module_rejected_after_initialize(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    with pytest.raises(RuntimeError, match="before initialize"):
        orch.register_module(NullModule("late", EventBus.get_instance(), config))


async def test_health_is_instant_synchronous_and_non_raising(config: Config) -> None:
    orch = NeuroPACAOrchestrator(config)
    module = NullModule("null", EventBus.get_instance(), config)
    orch.register_module(module)
    await orch.initialize()
    await orch.start()

    # module health: a plain sync call, no coroutine, sub-millisecond
    start = time.perf_counter()
    report = module.health()
    assert not asyncio.iscoroutine(report)
    assert isinstance(report, ModuleHealth) and report.ok
    assert time.perf_counter() - start < 0.005

    # system health: aggregates the module report, never raises, stays fast even
    # while the loop is doing other work
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        start = time.perf_counter()
        for _ in range(100):
            system_health = orch.health_check()
            await asyncio.sleep(0)
        elapsed = time.perf_counter() - start
    finally:
        task.cancel()

    assert isinstance(system_health, SystemHealth)
    assert system_health.ok is True
    assert system_health.modules == (ModuleHealth(name="null", ok=True, detail="null"),)
    assert elapsed < 0.5
    assert ticks >= 90  # the loop kept turning between checks

    await orch.stop()
