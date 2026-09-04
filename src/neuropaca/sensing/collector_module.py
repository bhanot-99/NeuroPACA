"""L2 · `XMetricCollector` — owns the collectors, the poll tasks, the ring
buffer, and all publishing (Architecture.md §4, D-7).

One `asyncio.Task` per collector (D-7 B2): distinct cadences, and killing one
collector's task leaves the others running. `collect()` is always dispatched via
`asyncio.to_thread` (D-7 B3). A collector that raises `max_failures` times in a
row self-disables and a `SYSTEM_ERROR` is published; siblings continue.

The module publishes `METRIC_COLLECTED` (`{"snapshot": snapshot}`, D-7 B7) and,
from the `system` collector's CPU reading, edge-triggered `IDLE_DETECTED` /
`ACTIVITY_DETECTED` — the stand-in for the deferred `ActivityCollector` (D-7 A3).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.base_collector import BaseCollector
from neuropaca.sensing.collectors.system import ACTIVE_CPU_PERCENT, IDLE_CPU_PERCENT
from neuropaca.sensing.snapshot import MetricSnapshot

_log = logging.getLogger(__name__)

_Runner = Callable[[Callable[[], MetricSnapshot]], Awaitable[MetricSnapshot]]


class _IdleWatcher:
    """Edge-triggered idle/activity from CPU load. Hysteresis (< IDLE to enter,
    >= ACTIVE to leave) stops it flapping at the threshold."""

    def __init__(self) -> None:
        self._idle = False
        self._since: datetime | None = None

    def observe(self, cpu_percent: float | None, now: datetime) -> Event | None:
        if cpu_percent is None:
            return None
        if not self._idle and cpu_percent < IDLE_CPU_PERCENT:
            self._idle = True
            self._since = now
            return Event(
                event_type=EventType.IDLE_DETECTED,
                source="sensing.system",
                payload={"source": "cpu", "cpu_percent": cpu_percent, "since": now.isoformat()},
            )
        if self._idle and cpu_percent >= ACTIVE_CPU_PERCENT:
            idle_seconds = (now - self._since).total_seconds() if self._since else 0.0
            self._idle = False
            self._since = None
            return Event(
                event_type=EventType.ACTIVITY_DETECTED,
                source="sensing.system",
                payload={"source": "cpu", "cpu_percent": cpu_percent, "idle_seconds": idle_seconds},
            )
        return None


class XMetricCollector(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        clock: Clock | None = None,
        runner: _Runner | None = None,
        emit_idle_from_cpu: bool = True,
    ) -> None:
        super().__init__("sensing", event_bus, config)
        self._clock: Clock = clock or SystemClock()
        self._runner = runner
        self._collectors: list[BaseCollector] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._buffer: deque[MetricSnapshot] = deque(maxlen=max(1, config.snapshot_buffer_size))
        self._idle_watcher = _IdleWatcher()
        # B2.5 (D-9): the real ActivityCollector supersedes this CPU-derived idle
        # stand-in — build_modules passes False when activity_enabled.
        self._emit_idle_from_cpu = emit_idle_from_cpu
        self._max_failures = config.max_failures

    def register_collector(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)

    @property
    def snapshot_buffer(self) -> tuple[MetricSnapshot, ...]:
        """Read-only view of the ring buffer — health() and $? retrieval only
        (D-7 B9). Never load-bearing for L3."""
        return tuple(self._buffer)

    async def initialize(self) -> None:
        return None  # L2 publishes only; it subscribes to nothing (Architecture.md §4)

    async def start(self) -> None:
        if self.is_running:
            return
        for collector in self._collectors:
            try:
                await collector.start()
            except Exception as exc:
                collector.is_enabled = False
                self._publish_error(collector.name, exc, "collector-start")
                continue
            if collector.is_enabled:
                self._tasks.append(asyncio.create_task(self._poll_collector(collector)))
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        for collector in self._collectors:
            try:
                await collector.stop()
            except Exception as exc:
                self._publish_error(collector.name, exc, "collector-stop")

    def health(self) -> ModuleHealth:
        enabled = [c for c in self._collectors if c.is_enabled]
        return ModuleHealth(
            name=self.name,
            ok=self.is_running and len(enabled) > 0,
            detail=(
                f"{len(enabled)}/{len(self._collectors)} collectors up, "
                f"buffer {len(self._buffer)}/{self._buffer.maxlen}"
            ),
            last_event_at=self._buffer[-1].timestamp if self._buffer else None,
        )

    # ------------------------------------------------------------------ internal
    async def _poll_collector(self, collector: BaseCollector) -> None:
        while self.is_running and collector.is_enabled:
            await self._clock.sleep(collector.poll_interval_seconds)
            if not (self.is_running and collector.is_enabled):
                return
            await self._poll_once(collector)

    async def _poll_once(self, collector: BaseCollector) -> None:
        try:
            if not collector.is_blocking:
                snapshot = collector.collect()  # loop-safe read (e.g. FS deque drain)
            elif self._runner is not None:
                snapshot = await self._runner(collector.collect)
            else:
                snapshot = await asyncio.to_thread(collector.collect)
        except Exception as exc:
            collector.consecutive_failures += 1
            if collector.consecutive_failures >= self._max_failures:
                collector.is_enabled = False
                self._publish_error(collector.name, exc, "collector-disabled")
            else:
                self._publish_error(collector.name, exc, "collector-failure")
            return

        collector.consecutive_failures = 0
        collector.last_poll = snapshot.timestamp
        self._buffer.append(snapshot)
        self.event_bus.publish(
            Event(
                event_type=EventType.METRIC_COLLECTED,
                source=f"sensing.{collector.name}",
                payload={"snapshot": snapshot},
            )
        )
        if collector.name == "system" and self._emit_idle_from_cpu:
            raw = snapshot.data.get("cpu_percent")
            cpu = float(raw) if isinstance(raw, (int, float)) else None
            event = self._idle_watcher.observe(cpu, snapshot.timestamp)
            if event is not None:
                self.event_bus.publish(event)

    def _publish_error(self, collector_name: str, exc: Exception, severity: str) -> None:
        _log.warning("collector %s %s: %r", collector_name, severity, exc)
        self.event_bus.publish(
            system_error_event(
                module=f"sensing.{collector_name}", exception=str(exc), severity=severity
            )
        )
