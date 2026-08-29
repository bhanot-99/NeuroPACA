"""B2 · `Clock` — deterministic time for the poll loop (D-7 B1)."""

from __future__ import annotations

import asyncio
import time

from neuropaca.core.clock import Clock, FakeClock, SystemClock


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(), Clock)


async def test_fake_clock_time_only_moves_on_advance() -> None:
    clock = FakeClock(start=100.0)
    assert clock.monotonic() == 100.0
    await clock.advance(30)
    assert clock.monotonic() == 130.0


async def test_fake_clock_wakes_a_sleeper_only_at_its_deadline() -> None:
    clock = FakeClock()
    woke_at: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(60)
        woke_at.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    assert clock.pending_sleepers == 1

    await clock.advance(30)
    assert woke_at == []  # deadline not reached

    await clock.advance(30)
    assert woke_at == [60.0]
    await task


async def test_fake_clock_zero_sleep_does_not_hang() -> None:
    await FakeClock().sleep(0)


async def test_system_clock_sleep_is_real() -> None:
    clock = SystemClock()
    start = time.monotonic()
    await clock.sleep(0.01)
    assert time.monotonic() - start >= 0.005
