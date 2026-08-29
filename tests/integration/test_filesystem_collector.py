"""B2 integration · FileSystemCollector with a live watchdog Observer.

Real threads and a real temp directory. Excluded from the default suite
(`-m 'not integration'`); run with `uv run pytest -m integration`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import CollectorError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.sensing.collector_module import XMetricCollector
from neuropaca.sensing.collectors.filesystem import FileSystemCollector
from neuropaca.sensing.collectors.system import SystemMetricCollector

pytestmark = pytest.mark.integration


async def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s}s")


def _events(sink: list[Event]) -> Callable[[Event], object]:
    async def handler(event: Event) -> None:
        sink.append(event)

    return handler


class _ExhaustedObserver:
    """Stand-in for watchdog.Observer that refuses every watch (inotify limit)."""

    def schedule(self, *args: object, **kwargs: object) -> None:
        raise OSError(28, "inotify watch limit reached")

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, *args: object) -> None: ...


# --------------------------------------------------------------- live observer


async def test_live_observer_captures_a_real_change_across_the_thread_boundary(tmp_path) -> None:
    collector = FileSystemCollector(watch_paths=[str(tmp_path)], ignore_globs=[], buffer_size=100)
    await collector.start()
    try:
        (tmp_path / "created.txt").write_text("hello")
        await _wait_until(lambda: any("created.txt" in c["path"] for c in collector._recent))

        snapshot = collector.collect()
        assert snapshot.collector_name == "filesystem"
        assert snapshot.data["change_count"] >= 1
        assert any("created.txt" in p for p in snapshot.data["changed_paths"])
        assert len(collector._recent) == 0  # collect() drained the deque
    finally:
        await collector.stop()


async def test_ignore_globs_are_honoured(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    collector = FileSystemCollector(
        watch_paths=[str(tmp_path)], ignore_globs=["*/.git/*"], buffer_size=100
    )
    await collector.start()
    try:
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
        (tmp_path / "real.txt").write_text("x")
        await _wait_until(lambda: any("real.txt" in c["path"] for c in collector._recent))
        await asyncio.sleep(0.2)  # give any .git event a chance to (wrongly) land
        assert not any(".git" in c["path"] for c in collector._recent)
    finally:
        await collector.stop()


# ------------------------------------------------------------ thread marshalling


async def test_watchdog_callback_is_marshalled_onto_the_loop_thread(tmp_path) -> None:
    collector = FileSystemCollector(watch_paths=[str(tmp_path)], ignore_globs=[], buffer_size=50)
    await collector.start()

    ran_on: list[str] = []
    real_record = collector._record

    def traced(path: str, kind: str) -> None:
        ran_on.append(threading.current_thread().name)
        real_record(path, kind)

    collector._record = traced  # type: ignore[method-assign]
    try:
        (tmp_path / "probe.txt").write_text("x")
        await _wait_until(lambda: len(ran_on) > 0)
        # If _record had been called directly on the watchdog thread this would
        # be a watchdog thread name, not the loop's thread.
        assert ran_on[0] == threading.main_thread().name
    finally:
        await collector.stop()


async def test_a_real_change_reaches_the_bus_as_metric_collected(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    clock = FakeClock()
    config = Config(inference_backend="fake", watch_paths=[str(tmp_path)])
    module = XMetricCollector(bus, config, clock=clock)
    fs_collector = FileSystemCollector(
        watch_paths=[str(tmp_path)], ignore_globs=[], buffer_size=100
    )
    module.register_collector(fs_collector)

    seen: list[Event] = []
    bus.subscribe(EventType.METRIC_COLLECTED, _events(seen))

    await module.start()
    (tmp_path / "watched.txt").write_text("data")
    await _wait_until(lambda: any("watched.txt" in c["path"] for c in fs_collector._recent))

    await clock.advance(60)  # drive the filesystem poll
    await bus.join()

    fs_snapshots = [
        e.payload["snapshot"] for e in seen if e.payload["snapshot"].collector_name == "filesystem"
    ]
    assert fs_snapshots
    assert any("watched.txt" in p for s in fs_snapshots for p in s.data["changed_paths"])

    await module.stop()
    await bus.stop()


# ------------------------------------------------------------ inotify exhaustion


async def test_inotify_exhaustion_raises_collector_error_and_disables(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("neuropaca.sensing.collectors.filesystem.Observer", _ExhaustedObserver)
    collector = FileSystemCollector(watch_paths=[str(tmp_path)], ignore_globs=[], buffer_size=10)
    with pytest.raises(CollectorError, match="watch setup failed"):
        await collector.start()
    assert collector.is_enabled is False


async def test_inotify_exhaustion_publishes_system_error_and_isolates_others(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("neuropaca.sensing.collectors.filesystem.Observer", _ExhaustedObserver)
    bus = EventBus.get_instance()
    await bus.start()
    errors: list[Event] = []
    bus.subscribe(EventType.SYSTEM_ERROR, _events(errors))

    config = Config(inference_backend="fake", watch_paths=[str(tmp_path)])
    module = XMetricCollector(bus, config, clock=FakeClock())
    healthy = SystemMetricCollector()
    doomed = FileSystemCollector(watch_paths=[str(tmp_path)], ignore_globs=[], buffer_size=10)
    module.register_collector(healthy)
    module.register_collector(doomed)

    await module.start()
    await bus.join()

    assert doomed.is_enabled is False
    assert healthy.is_enabled is True
    assert module.is_running is True
    assert any(e.payload["module"] == "sensing.filesystem" for e in errors)
    assert any(e.payload["severity"] == "collector-start" for e in errors)

    await module.stop()
    await bus.stop()
