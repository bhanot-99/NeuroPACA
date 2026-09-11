# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B15 · `WaylandConnection` — the shared Wayland connection + poll-pump.

Two layers of coverage, neither needs a compositor:

  * the poll-pump / reconnect / give-up state machine, driven with a fake
    `display` double and a real `os.pipe()` fd;
  * the connect + bind + bound()/primed() flow, driven with a fake `pywayland`
    module injected into `sys.modules`.

Contract under test:
  - `dispatch(block=False)` every tick (the unconditional drain is the fix);
  - `read()` only when `select` says the fd is readable;
  - a tick that raises → `handler.lost()` + teardown + bounded-backoff reconnect;
  - budget spent → give up, `is_alive` False (so health tells the truth);
  - reconnect re-runs `bound()` + `primed()` on every handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
import types

import pytest

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.activity import wayland_conn as wc
from neuropaca.sensing.activity.wayland_conn import WaylandConnection

# --------------------------------------------------------------------------- doubles


class _FakeDisplay:
    def __init__(self) -> None:
        self.reads = 0
        self.dispatches = 0
        self.flushes = 0
        self.roundtrips = 0
        self.disconnected = False
        self.dispatch_error: Exception | None = None  # raised once, then cleared
        self._registry = _FakeRegistry()

    # client-loop surface
    def read(self) -> None:
        self.reads += 1

    def dispatch(self, *, block: bool = False) -> int:
        self.dispatches += 1
        if self.dispatch_error is not None:
            err, self.dispatch_error = self.dispatch_error, None
            raise err
        return 0

    def flush(self) -> None:
        self.flushes += 1

    def disconnect(self) -> None:
        self.disconnected = True

    # connect surface
    def connect(self) -> None:
        pass

    def get_registry(self):
        return self._registry

    def roundtrip(self) -> None:
        self.roundtrips += 1
        # emulate the compositor advertising the globals the handlers asked for
        if self.roundtrips == 1:
            self._registry.announce()

    def get_fd(self) -> int:
        return _PIPE_R


class _FakeRegistry:
    def __init__(self) -> None:
        self.dispatcher: dict = {}
        self._wanted_names = {
            "ext_idle_notifier_v1": 1,
            "wl_seat": 1,
            "ext_foreign_toplevel_list_v1": 1,
            "zcosmic_toplevel_info_v1": 1,
        }

    def announce(self) -> None:
        cb = self.dispatcher.get("global")
        if cb is None:
            return
        for i, (name, ver) in enumerate(self._wanted_names.items()):
            cb(self, i, name, ver)

    def bind(self, name: int, cls, version: int):
        return _FakeProxy(cls.__name__)


class _FakeProxy:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.dispatcher: dict = {}


class _FakeHandler:
    def __init__(
        self, wants: dict | None = None, *, primed_raises: Exception | None = None
    ) -> None:
        self._wants = wants or {}
        self._primed_raises = primed_raises
        self.bound_calls = 0
        self.primed_calls = 0
        self.lost_calls = 0
        self.last_globals: dict | None = None

    def wants(self) -> dict[str, tuple[type, int]]:
        return self._wants

    def bound(self, globals_: dict) -> None:
        self.bound_calls += 1
        self.last_globals = globals_

    def primed(self) -> None:
        self.primed_calls += 1
        if self._primed_raises is not None:
            raise self._primed_raises

    def lost(self) -> None:
        self.lost_calls += 1


_PIPE_R = -1  # set per-test


async def _run_pump(conn: WaylandConnection, seconds: float) -> asyncio.Task[None]:
    task = asyncio.get_running_loop().create_task(conn._pump())
    conn._task = task
    await asyncio.sleep(seconds)
    conn._stopped = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


# --------------------------------------------------------------- pump: cadence


async def test_dispatch_runs_every_tick_even_when_fd_not_readable(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.02)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    fake = _FakeDisplay()
    conn._display = fake
    conn._fd = r
    try:
        await _run_pump(conn, 0.2)
    finally:
        os.close(r)
        os.close(w)
    assert fake.reads == 0
    assert fake.dispatches >= 3
    assert fake.flushes >= 3


async def test_read_runs_only_when_fd_is_readable(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.02)
    r, w = os.pipe()
    os.write(w, b"x")
    conn = WaylandConnection()
    conn._stopped = False

    fake = _FakeDisplay()
    conn._display = fake
    conn._fd = r
    try:
        await _run_pump(conn, 0.1)
    finally:
        os.close(r)
        os.close(w)
    assert fake.reads >= 1


# ------------------------------------------------------------ pump: resilience


async def test_transient_error_reconnects_recovers_and_re_primes(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.02,) * 5)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    handler = _FakeHandler()
    conn.add(handler)
    displays: list[_FakeDisplay] = []

    def fake_connect() -> None:
        d = _FakeDisplay()
        displays.append(d)
        conn._display = d
        conn._fd = r
        conn._connected = True

        for h in conn._handlers:
            h.bound({})
            h.primed()

    monkeypatch.setattr(conn, "_connect", fake_connect)
    fake_connect()
    displays[0].dispatch_error = RuntimeError("Failed to read events")

    try:
        await _run_pump(conn, 0.3)
    finally:
        os.close(r)
        os.close(w)

    assert len(displays) >= 2, "did not reconnect"
    assert displays[0].disconnected, "broken display not torn down"
    assert handler.lost_calls >= 1, "handler not told the connection was lost"
    assert handler.bound_calls >= 2 and handler.primed_calls >= 2, "handler not re-primed"
    assert displays[-1].dispatches >= 1, "new display not pumping"
    # B15 soak instrumentation — a raised tick is a pump error, and the recovery
    # from it is a reconnect. The 7-day summary keys "watchdog busy" off these.
    assert conn.pump_errors >= 1, "pump error not counted"
    assert conn.reconnects >= 1, "reconnect not counted"


async def test_liveness_watchdog_reconnects_when_active_but_silent(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(wc, "_STALE_RECONNECT_SECONDS", 0.05)
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.01,) * 5)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn.activity_probe = lambda: True  # user is active
    conn._last_event_at = 0.0  # ancient — nothing dispatched
    handler = _FakeHandler()
    conn.add(handler)
    displays: list[_FakeDisplay] = []

    def fake_connect() -> None:
        d = _FakeDisplay()
        displays.append(d)
        conn._display = d
        conn._fd = r
        conn._connected = True
        conn._last_event_at = 0.0  # keep it stale so the watchdog trips again
        for h in conn._handlers:
            h.bound({})
            h.primed()

    monkeypatch.setattr(conn, "_connect", fake_connect)
    fake_connect()
    try:
        await _run_pump(conn, 0.2)
    finally:
        os.close(r)
        os.close(w)
    assert len(displays) >= 2, "watchdog did not force a reconnect"
    assert handler.lost_calls >= 1
    # a watchdog reconnect is counted, but it is not a pump error — the tick
    # never raised, the connection was just silent.
    assert conn.reconnects >= 1
    assert conn.watchdog_reconnects >= 1
    assert conn.pump_errors == 0


async def test_watchdog_disarms_once_an_event_has_been_dispatched(monkeypatch) -> None:
    # B16 · the false-positive fix. A subscription that delivers even one event is
    # proven; after that, silence is a stable focused window, not deafness, so the
    # watchdog must never fire again for this connection.
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(wc, "_STALE_RECONNECT_SECONDS", 0.05)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn.activity_probe = lambda: True  # user active the whole time
    conn._last_event_at = 0.0

    fake = _FakeDisplay()
    calls = {"n": 0}

    def dispatch_once(*, block: bool = False) -> int:
        calls["n"] += 1
        return 1 if calls["n"] == 1 else 0  # one event, then silence forever

    fake.dispatch = dispatch_once  # type: ignore[method-assign]
    conn._display = fake
    conn._fd = r
    try:
        await _run_pump(conn, 0.3)  # many times the stale window
    finally:
        os.close(r)
        os.close(w)
    assert conn.confirmed_live is True
    assert conn.watchdog_reconnects == 0, "watchdog fired on a proven subscription"
    assert fake.disconnected is False


async def test_watchdog_interval_backs_off_while_never_confirmed(monkeypatch) -> None:
    # B16 · a came-up-deaf connection whose user never interacts must not thrash —
    # each fruitless re-roll doubles the interval.
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(wc, "_STALE_RECONNECT_SECONDS", 0.02)
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.01,) * 5)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn.activity_probe = lambda: True
    conn._last_event_at = 0.0
    conn.add(_FakeHandler())

    def fake_connect() -> None:
        d = _FakeDisplay()
        conn._display = d
        conn._fd = r
        conn._connected = True
        conn._last_event_at = 0.0  # still nothing ever dispatched
        for h in conn._handlers:
            h.bound({})
            h.primed()

    monkeypatch.setattr(conn, "_connect", fake_connect)
    fake_connect()
    start_interval = conn._watchdog_interval
    try:
        await _run_pump(conn, 0.3)
    finally:
        os.close(r)
        os.close(w)
    assert conn.watchdog_reconnects >= 2
    assert conn._watchdog_interval > start_interval, "interval did not back off"
    assert conn._watchdog_interval <= wc._WATCHDOG_MAX_INTERVAL_SECONDS


async def test_liveness_watchdog_stays_quiet_when_user_is_idle(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(wc, "_STALE_RECONNECT_SECONDS", 0.05)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn.activity_probe = lambda: False  # user is idle — no watchdog
    conn._last_event_at = 0.0
    fake = _FakeDisplay()
    conn._display = fake
    conn._fd = r
    try:
        await _run_pump(conn, 0.15)
    finally:
        os.close(r)
        os.close(w)
    assert fake.disconnected is False  # never torn down
    assert conn.reconnects == 0 and conn.pump_errors == 0  # a quiet idle box


async def test_a_healthy_pump_leaves_the_soak_counters_at_zero(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn._display = _FakeDisplay()
    conn._fd = r
    conn._last_event_at = 0.0
    try:
        await _run_pump(conn, 0.1)
    finally:
        os.close(r)
        os.close(w)
    assert conn.reconnects == 0
    assert conn.watchdog_reconnects == 0
    assert conn.pump_errors == 0
    # nothing was ever dispatched, so the "since last event" clock never started
    assert conn.seconds_since_event == 0.0


def test_seconds_since_event_climbs_once_an_event_has_landed() -> None:
    conn = WaylandConnection()
    assert conn.seconds_since_event == 0.0  # no event yet
    conn._last_event_at = time.monotonic() - 5.0
    assert 4.0 < conn.seconds_since_event < 10.0


async def test_a_long_outage_never_gives_up_and_reports_dead_meanwhile(monkeypatch) -> None:
    """V-11 · the pump used to `return` after the fast ladder (~62 s), leaving the
    focus sensor deaf for the rest of the daemon's life. It now keeps trying —
    and, as B15 requires, reports itself dead the whole time it is not connected."""
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(wc, "_SLOW_RECONNECT_SECONDS", 0.01)
    conn = WaylandConnection()
    conn._stopped = False
    conn._display = None
    conn.add(_FakeHandler())
    attempts = 0

    def refused() -> None:
        nonlocal attempts
        attempts += 1
        raise CollectorError("no compositor")

    monkeypatch.setattr(conn, "_connect", refused)
    task = asyncio.get_running_loop().create_task(conn._pump())
    conn._task = task
    try:
        await asyncio.sleep(0.2)
        assert not task.done(), "the pump gave up"
        assert conn.is_alive is False
        assert attempts > len(wc._RECONNECT_DELAYS_SECONDS) + 3  # well past the old limit
    finally:
        conn._stopped = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_the_sensor_comes_back_when_the_compositor_does(monkeypatch) -> None:
    """The point of never giving up: the compositor returns long after the old
    give-up point, and focus sensing resumes with no daemon restart."""
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(wc, "_SLOW_RECONNECT_SECONDS", 0.01)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn._display = None
    handler = _FakeHandler()
    conn.add(handler)
    attempts = 0

    def comes_back() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 6:  # three times the old two-attempt budget
            raise CollectorError("no compositor")
        d = _FakeDisplay()
        conn._display = d
        conn._fd = r
        conn._connected = True
        for h in conn._handlers:
            h.bound({})
            h.primed()

    monkeypatch.setattr(conn, "_connect", comes_back)
    task = asyncio.get_running_loop().create_task(conn._pump())
    conn._task = task
    try:
        await asyncio.sleep(0.3)
        assert conn.is_alive, "did not recover"
        assert conn.reconnects >= 1
        assert handler.primed_calls >= 1  # handlers re-primed on the new connection
    finally:
        conn._stopped = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        os.close(r)
        os.close(w)


async def test_a_long_outage_logs_once_not_once_a_minute(monkeypatch, caplog) -> None:
    """A week-long outage must not write a traceback every minute."""
    caplog.set_level(logging.DEBUG, logger="neuropaca.sensing.activity.wayland_conn")
    monkeypatch.setattr(wc, "_RECONNECT_DELAYS_SECONDS", (0.01,))
    monkeypatch.setattr(wc, "_SLOW_RECONNECT_SECONDS", 0.01)
    conn = WaylandConnection()
    conn._stopped = False
    conn._display = None
    conn.add(_FakeHandler())
    monkeypatch.setattr(conn, "_connect", lambda: (_ for _ in ()).throw(CollectorError("x")))
    task = asyncio.get_running_loop().create_task(conn._pump())
    conn._task = task
    try:
        await asyncio.sleep(0.2)
    finally:
        conn._stopped = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    switch = [r for r in caplog.records if "retrying every" in r.getMessage()]
    assert len(switch) == 1 and switch[0].levelno == logging.ERROR
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == len(wc._RECONNECT_DELAYS_SECONDS)  # the fast ladder only


async def test_shutdown_race_is_not_counted_as_a_pump_error(monkeypatch, caplog) -> None:
    # B16 §3d — stop() landing at the exact moment a tick raises (the SIGTERM race
    # that logged a "Failed to read events" traceback in the soak) must exit
    # quietly: no pump_errors, no traceback, still torn down.
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.01)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    fake = _FakeDisplay()
    conn._display = fake
    conn._fd = r

    real_dispatch = fake.dispatch

    def dispatch_then_shutdown(*, block: bool = False) -> int:
        conn._stopped = True  # stop() lands now
        raise RuntimeError("Failed to read events")

    fake.dispatch = dispatch_then_shutdown  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        task = asyncio.get_running_loop().create_task(conn._pump())
        await asyncio.wait_for(task, timeout=1.0)
    os.close(r)
    os.close(w)
    _ = real_dispatch
    assert conn.pump_errors == 0
    assert fake.disconnected is True
    assert "pump tick failed" not in caplog.text


async def test_cancellation_propagates(monkeypatch) -> None:
    monkeypatch.setattr(wc, "_POLL_INTERVAL_SECONDS", 0.02)
    r, w = os.pipe()
    conn = WaylandConnection()
    conn._stopped = False
    conn._display = _FakeDisplay()
    conn._fd = r
    task = asyncio.get_running_loop().create_task(conn._pump())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    os.close(r)
    os.close(w)


# ---------------------------------------------------------------- is_alive


async def test_is_alive_state_machine(monkeypatch) -> None:
    conn = WaylandConnection()
    assert conn.is_alive is False  # never started

    conn._connected = True
    conn._task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    assert conn.is_alive is True  # connected + task running

    conn._connected = False
    assert conn.is_alive is False  # task alive but connection down

    conn._connected = True
    conn._task.cancel()
    try:
        await conn._task
    except asyncio.CancelledError:
        pass
    assert conn.is_alive is False  # task done


# --------------------------------------------------------- teardown / stop


def test_teardown_calls_lost_on_every_handler_and_is_idempotent() -> None:
    conn = WaylandConnection()
    h1, h2 = _FakeHandler(), _FakeHandler()
    conn.add(h1)
    conn.add(h2)
    conn._display = _FakeDisplay()
    conn._connected = True

    conn._teardown()
    assert h1.lost_calls == 1 and h2.lost_calls == 1
    assert conn._display is None
    assert conn._connected is False

    conn._teardown()  # again — must not raise
    assert h1.lost_calls == 2  # lost() is cheap + idempotent by contract


def test_teardown_survives_a_handler_that_raises_in_lost(caplog) -> None:
    conn = WaylandConnection()

    class _Bad:
        def wants(self):  # pragma: no cover - unused
            return {}

        def bound(self, g):  # pragma: no cover - unused
            pass

        def primed(self):  # pragma: no cover - unused
            pass

        def lost(self):
            raise RuntimeError("boom")

    good = _FakeHandler()
    conn.add(_Bad())
    conn.add(good)
    conn._display = _FakeDisplay()
    conn._teardown()  # must not raise
    assert good.lost_calls == 1  # the good handler still got told


# ------------------------------------------------- _connect with a fake pywayland


def _install_fake_pywayland(monkeypatch, display: _FakeDisplay) -> None:
    client_mod = types.ModuleType("pywayland.client")
    client_mod.Display = lambda: display  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywayland.client", client_mod)


def test_connect_binds_globals_then_primes_and_marks_connected(monkeypatch) -> None:
    global _PIPE_R
    r, w = os.pipe()
    _PIPE_R = r
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
        display = _FakeDisplay()
        _install_fake_pywayland(monkeypatch, display)

        order: list[str] = []
        h = _FakeHandler(wants={"wl_seat": (object, 1), "ext_idle_notifier_v1": (object, 1)})
        h.bound = lambda g: order.append("bound")  # type: ignore[assignment]
        h.primed = lambda: order.append("primed")  # type: ignore[assignment]
        conn = WaylandConnection()
        conn.add(h)

        conn._connect()

        assert order == ["bound", "primed"]  # bound strictly before primed
        assert display.roundtrips >= 2  # bind + prime roundtrips
        assert conn._connected is True
        assert conn._fd == r
    finally:
        os.close(r)
        os.close(w)


def test_connect_raises_collectorerror_without_wayland_display(monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    conn = WaylandConnection()
    with pytest.raises(CollectorError, match="WAYLAND_DISPLAY"):
        conn._connect()


def test_connect_wraps_a_handler_collectorerror_and_disconnects(monkeypatch) -> None:
    global _PIPE_R
    r, w = os.pipe()
    _PIPE_R = r
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
        display = _FakeDisplay()
        _install_fake_pywayland(monkeypatch, display)

        h = _FakeHandler()
        h.bound = lambda g: (_ for _ in ()).throw(CollectorError("no such global"))  # type: ignore[assignment]
        conn = WaylandConnection()
        conn.add(h)

        with pytest.raises(CollectorError, match="no such global"):
            conn._connect()
        assert display.disconnected is True
        assert conn._connected is False
    finally:
        os.close(r)
        os.close(w)


def test_connect_wraps_a_primed_failure_as_collectorerror(monkeypatch) -> None:
    global _PIPE_R
    r, w = os.pipe()
    _PIPE_R = r
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
        display = _FakeDisplay()
        _install_fake_pywayland(monkeypatch, display)
        conn = WaylandConnection()
        conn.add(_FakeHandler(primed_raises=RuntimeError("get_idle_notification blew up")))
        with pytest.raises(CollectorError, match="protocol setup failed"):
            conn._connect()
        assert display.disconnected is True and conn._display is None
    finally:
        os.close(r)
        os.close(w)


def test_connect_binds_and_primes_every_handler(monkeypatch) -> None:
    global _PIPE_R
    r, w = os.pipe()
    _PIPE_R = r
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
        _install_fake_pywayland(monkeypatch, _FakeDisplay())
        conn = WaylandConnection()
        a, b = _FakeHandler(), _FakeHandler()
        conn.add(a)
        conn.add(b)
        conn._connect()
        assert a.bound_calls == 1 and b.bound_calls == 1
        assert a.primed_calls == 1 and b.primed_calls == 1
    finally:
        os.close(r)
        os.close(w)


# gen-ref: 82d035bd
