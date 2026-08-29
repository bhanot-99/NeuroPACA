"""`BaseCollector` — the contract every L2 collector implements (Architecture.md §4).

A collector is a pure data source: `collect()` reads the system and returns a
`MetricSnapshot`, or raises. It holds no `EventBus` reference and makes no
decisions (rules.md §0.1, "no intelligence" — Architecture.md §4). Publishing,
buffering, failure counting, and idle inference all live in `XMetricCollector`.

`collect()` may block (e.g. `psutil.cpu_percent(interval=1)` costs a second);
`XMetricCollector` always calls it via `asyncio.to_thread` (D-7 B3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from neuropaca.sensing.snapshot import MetricSnapshot


class BaseCollector(ABC):
    # True  -> collect() may block; XMetricCollector runs it via asyncio.to_thread.
    # False -> collect() is a cheap loop-safe read (e.g. draining a deque that the
    #          watchdog thread fills via loop.call_soon_threadsafe); run inline.
    is_blocking: bool = True

    def __init__(self, name: str, poll_interval_seconds: float) -> None:
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.last_poll: datetime | None = None
        self.is_enabled = True
        self.consecutive_failures = 0

    @abstractmethod
    def collect(self) -> MetricSnapshot:
        """Read the system and return a snapshot. Blocking and synchronous —
        the caller runs it off the event loop. Raise on failure."""

    def should_poll(self) -> bool:
        """Cadence is handled by the per-collector task's sleep; this is the
        enable gate (Architecture.md §4)."""
        return self.is_enabled

    async def start(self) -> None:  # noqa: B027 — optional hook, subclasses override as needed
        """Optional async setup (e.g. start a watchdog Observer). Default no-op."""

    async def stop(self) -> None:  # noqa: B027 — optional hook, subclasses override as needed
        """Optional async teardown. Default no-op."""
