"""B15 · one shared pywayland `Display` for every activity protocol.

**Why this exists.** Running two `pywayland.client.Display` connections in one
process is broken on this stack: the second connection's event stream is
unreliable and teardown segfaults (exit 139, measured 2026-09-08). An
`ActivityCollector` with a `WaylandIdleSource` **and** a `WaylandWindowSource`,
each its own `Display`, hit exactly this. libwayland is one connection per
client, so idle-notify and toplevel-info now bind on this one `Display`, share
one fd, and are drained by one poll-pump.

(The daemon's "~4 focus events an hour" also had a second, deeper cause — the
`zcosmic_toplevel_handle_v1` proxies in `window.py` were GC'd before their
`state` events arrived; fixed there with a strong-ref dict. Both fixes are
needed.)

**The pump** (mirrors the working B2.5 spike, not `loop.add_reader`, which does
not reliably flush pywayland's queue to the handlers — flacjacket/pywayland#16):
every `_POLL_INTERVAL_SECONDS`, a non-blocking `select`, `read()` only when the
fd is readable, then **always** `dispatch(block=False)` + `flush()`. A tick that
raises tears the connection down and reconnects with bounded backoff; once that
budget is spent the connection gives up and `is_alive` is False so
`ActivityCollector.health()` stops printing a healthy sensor (the B7 failure was
a dead collector that still looked fine).
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import time
from collections.abc import Callable
from typing import Any, Protocol

from neuropaca.core.errors import CollectorError

_log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.2
_RECONNECT_DELAYS_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0)
# Roundtrips during connect to drain the compositor's initial state. Binding the
# toplevel list makes each handler send a `get_cosmic_toplevel` request; a
# further round collects the `state` replies to those.
_PRIME_ROUNDTRIPS = 2
# Liveness watchdog: if the user has had input recently (the `activity_probe`
# says not-idle) but this connection has dispatched **zero** events for this
# long, the toplevel-info subscription has probably half-established on a bad
# start (~1 in 20). Force one reconnect — cheap (~50 ms) and it re-rolls the
# dice. On a genuinely idle machine the probe is False and this never fires.
_STALE_RECONNECT_SECONDS = 180.0


class WaylandProtocolHandler(Protocol):
    """A protocol bound on the shared connection (idle-notify, toplevel-info)."""

    def wants(self) -> dict[str, tuple[type, int]]:
        """`{interface_name: (proxy_class, max_version)}` to bind from the registry."""

    def bound(self, globals_: dict[str, Any]) -> None:
        """Called once after the bind roundtrip with `{interface_name: proxy}`.
        Raise `CollectorError` if a required global is absent."""

    def primed(self) -> None:
        """Called after the second roundtrip — the compositor's initial state
        (current toplevels, etc.) has been received."""

    def lost(self) -> None:
        """The connection dropped. Drop any per-connection proxy references; the
        handler will be `bound()` + `primed()` again on reconnect."""


class WaylandConnection:
    def __init__(self) -> None:
        self._display: Any = None
        self._fd = -1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._connected = False
        self._handlers: list[WaylandProtocolHandler] = []
        # optional "is the user active right now" probe (the collector wires this
        # to `not activity_collector._idle`); gates the liveness watchdog.
        self.activity_probe: Callable[[], bool] | None = None
        self._last_event_at = 0.0

    def add(self, handler: WaylandProtocolHandler) -> None:
        self._handlers.append(handler)

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done() and self._connected

    def start(self) -> None:
        """Connect (synchronous — raises `CollectorError` so the caller can
        disable the activity half cleanly) and spawn the poll-pump."""
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        self._connect()
        self._task = self._loop.create_task(self._pump())

    # ------------------------------------------------------------- connection
    def _connect(self) -> None:
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise CollectorError("no $WAYLAND_DISPLAY — not a Wayland session")
        try:
            from pywayland.client import Display
        except ImportError as exc:
            raise CollectorError(
                f"pywayland not installed (pip install .[activity]): {exc}"
            ) from exc
        try:
            display = Display()
            display.connect()
        except Exception as exc:  # pywayland raises bare exceptions on connect failure
            raise CollectorError(f"cannot connect to the Wayland display: {exc}") from exc

        registry = display.get_registry()
        wanted: dict[str, tuple[type, int]] = {}
        try:
            for handler in self._handlers:
                wanted.update(handler.wants())
        except ImportError as exc:
            display.disconnect()
            raise CollectorError(
                f"pywayland protocol bindings missing (pip install .[activity]): {exc}"
            ) from exc
        got: dict[str, Any] = {}

        def _on_global(_reg: Any, name: int, interface: str, version: int) -> None:
            spec = wanted.get(interface)
            if spec is not None:
                cls, max_version = spec
                got[interface] = registry.bind(name, cls, min(version, max_version))

        registry.dispatcher["global"] = _on_global
        display.roundtrip()

        try:
            for handler in self._handlers:
                handler.bound(got)
        except CollectorError:
            display.disconnect()
            raise

        self._display = display
        self._fd = display.get_fd()
        for _ in range(_PRIME_ROUNDTRIPS):
            display.roundtrip()  # toplevel list, then the get_cosmic_toplevel state replies
        try:
            for handler in self._handlers:
                handler.primed()
        except Exception as exc:
            display.disconnect()
            self._display = None
            raise CollectorError(f"Wayland protocol setup failed: {exc}") from exc
        display.flush()
        self._connected = True
        self._last_event_at = time.monotonic()

    async def _pump(self) -> None:
        failures = 0
        while not self._stopped:
            try:
                if self._display is None:
                    self._connect()
                    _log.info("WaylandConnection reconnected")
                assert self._display is not None
                if select.select([self._fd], [], [], 0)[0]:
                    self._display.read()
                dispatched = self._display.dispatch(block=False)
                self._display.flush()
                self._connected = True
                failures = 0
                now = time.monotonic()
                if dispatched:
                    self._last_event_at = now
                elif (
                    now - self._last_event_at > _STALE_RECONNECT_SECONDS
                    and self.activity_probe is not None
                    and self.activity_probe()
                ):
                    _log.warning(
                        "no Wayland events in %.0fs while the user is active — "
                        "reconnecting in case the subscription half-established",
                        now - self._last_event_at,
                    )
                    self._teardown()  # next tick reconnects; the sleep below keeps this bounded
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                _log.exception("WaylandConnection pump tick failed — will reconnect")
                self._teardown()
                if failures >= len(_RECONNECT_DELAYS_SECONDS):
                    _log.error("WaylandConnection giving up after %d reconnect attempts", failures)
                    return
                await asyncio.sleep(_RECONNECT_DELAYS_SECONDS[failures])
                failures += 1
                continue
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _teardown(self) -> None:
        self._connected = False
        self._fd = -1
        for handler in self._handlers:
            try:
                handler.lost()
            except Exception:
                _log.exception("Wayland handler.lost() failed")
        if self._display is not None:
            try:
                self._display.disconnect()
            except Exception:
                pass
        self._display = None

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._teardown()
