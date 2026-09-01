"""Injectable time (D-7).

Every module with a poll loop, a decay timer, or an idle threshold takes a
`Clock` rather than calling `time` / `asyncio.sleep` directly, so tests advance
time deterministically instead of sleeping (rules.md §8). B2 is the first user;
B5 (L9 daily surfacing cap) and B6 (DMN timing) reuse it.

`monotonic()` is for durations; `now()` (B5) is wall-clock — the L9 insight cap
resets at local midnight, which only a real calendar time can express.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime:
        """Timezone-aware wall-clock time. Never naive."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The production clock — real monotonic time, real `asyncio.sleep`."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now().astimezone()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests. Time only moves when `advance()` is called;
    a coroutine awaiting `sleep()` wakes when time reaches its deadline. `now()`
    tracks the same advances against a fixed wall-clock origin."""

    def __init__(self, start: float = 0.0, *, wall: datetime | None = None) -> None:
        self._now = start
        self._wall = wall if wall is not None else datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    def now(self) -> datetime:
        return self._wall

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._sleepers.append((self._now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking every sleeper whose deadline is now past.

        Yields first so a just-created coroutine reaches its `sleep()` and
        registers before the clock moves, then yields after so the woken
        coroutines run to their next suspension point.
        """
        for _ in range(5):
            await asyncio.sleep(0)
        self._now += seconds
        self._wall += timedelta(seconds=seconds)
        due = [(d, f) for d, f in self._sleepers if d <= self._now]
        self._sleepers = [(d, f) for d, f in self._sleepers if d > self._now]
        for _deadline, future in due:
            if not future.done():
                future.set_result(None)
        for _ in range(20):
            await asyncio.sleep(0)

    @property
    def pending_sleepers(self) -> int:
        return len(self._sleepers)
