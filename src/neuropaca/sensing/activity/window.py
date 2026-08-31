"""`WindowSource` — the focused window's `app_id` / `title` (B2.5b, D-9).

`WaylandWindowSource` uses `ext_foreign_toplevel_list_v1` (bundled) to enumerate
toplevels and the vendored `zcosmic_toplevel_info_v1` to learn which one is
`activated`. pywayland is optional + lazy-imported; a missing library, no
compositor, or a compositor without the protocol raises `CollectorError`, which
`ActivityCollector` turns into a graceful self-disable (rules.md §2). One Display
connection, `loop.add_reader` on its fd, no thread, no lock (rules.md §3).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from neuropaca.core.errors import CollectorError

__all__ = ["FakeWindowSource", "WaylandWindowSource", "WindowInfo", "WindowSource"]

_TOPLEVEL_LIST_MAX_VERSION = 1
_TOPLEVEL_INFO_MAX_VERSION = 3
_STATE_ACTIVATED = 2  # zcosmic_toplevel_handle_v1.state enum: activated == 2


@dataclass(frozen=True, slots=True)
class WindowInfo:
    app_id: str
    title: str


WindowCallback = Callable[[WindowInfo], None]


class WindowSource(Protocol):
    def start(self, on_switch: WindowCallback) -> None: ...

    def stop(self) -> None: ...


class FakeWindowSource:
    """Test double — `emit(app_id, title)` drives a focus change."""

    def __init__(self) -> None:
        self._cb: WindowCallback | None = None
        self.started = False

    def start(self, on_switch: WindowCallback) -> None:
        self._cb = on_switch
        self.started = True

    def stop(self) -> None:
        self._cb = None
        self.started = False

    def emit(self, app_id: str, title: str = "") -> None:
        if self._cb is None:
            raise RuntimeError("FakeWindowSource.emit() before start()")
        self._cb(WindowInfo(app_id=app_id, title=title))


class _Toplevel:
    __slots__ = ("activated", "app_id", "title")

    def __init__(self) -> None:
        self.app_id = ""
        self.title = ""
        self.activated = False


class WaylandWindowSource:
    def __init__(self) -> None:
        self._display: Any = None
        self._fd = -1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cb: WindowCallback | None = None
        self._info_manager: Any = None
        self._toplevels: dict[int, _Toplevel] = {}
        self._focused_app_id: str | None = None

    def start(self, on_switch: WindowCallback) -> None:
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise CollectorError("no $WAYLAND_DISPLAY — not a Wayland session")
        try:
            from pywayland.client import Display
            from pywayland.protocol.ext_foreign_toplevel_list_v1 import ExtForeignToplevelListV1

            from neuropaca.sensing.activity._protocols.cosmic_toplevel_info_unstable_v1 import (
                ZcosmicToplevelInfoV1,
            )
        except ImportError as exc:
            raise CollectorError(
                f"pywayland / cosmic protocol bindings missing (pip install .[activity]): {exc}"
            ) from exc

        self._cb = on_switch
        self._loop = asyncio.get_running_loop()

        try:
            display = Display()
            display.connect()
        except Exception as exc:
            raise CollectorError(f"cannot connect to the Wayland display: {exc}") from exc

        found: dict[str, Any] = {}
        registry = display.get_registry()

        def _on_global(_reg: Any, name: int, interface: str, version: int) -> None:
            if interface == "ext_foreign_toplevel_list_v1":
                found["list"] = registry.bind(
                    name, ExtForeignToplevelListV1, min(version, _TOPLEVEL_LIST_MAX_VERSION)
                )
            elif interface == "zcosmic_toplevel_info_v1":
                found["info"] = registry.bind(
                    name, ZcosmicToplevelInfoV1, min(version, _TOPLEVEL_INFO_MAX_VERSION)
                )

        registry.dispatcher["global"] = _on_global
        display.roundtrip()

        if "list" not in found or "info" not in found:
            display.disconnect()
            raise CollectorError(
                "compositor lacks ext_foreign_toplevel_list_v1 + zcosmic_toplevel_info_v1"
            )

        self._info_manager = found["info"]
        found["list"].dispatcher["toplevel"] = self._on_toplevel

        self._display = display
        self._fd = display.get_fd()
        display.roundtrip()  # prime the current toplevel set
        self._recompute_focus()
        display.flush()
        self._loop.add_reader(self._fd, self._on_readable)

    # ------------------------------------------------------ dispatcher callbacks
    def _on_toplevel(self, _list: Any, handle: Any) -> None:
        key = id(handle)
        self._toplevels[key] = _Toplevel()
        handle.dispatcher["app_id"] = lambda h, app_id: self._set(id(h), "app_id", app_id)
        handle.dispatcher["title"] = lambda h, title: self._set(id(h), "title", title)
        handle.dispatcher["closed"] = lambda h: self._drop(id(h))
        cosmic_handle = self._info_manager.get_cosmic_toplevel(handle)
        cosmic_handle.dispatcher["state"] = lambda _ch, state: self._set(
            key, "activated", _STATE_ACTIVATED in list(state)
        )

    def _set(self, key: int, attr: str, value: Any) -> None:
        top = self._toplevels.get(key)
        if top is not None:
            setattr(top, attr, value)
            self._recompute_focus()

    def _drop(self, key: int) -> None:
        if self._toplevels.pop(key, None) is not None:
            self._recompute_focus()

    def _recompute_focus(self) -> None:
        focused = next((t for t in self._toplevels.values() if t.activated), None)
        app_id = focused.app_id if focused is not None else None
        if app_id and app_id != self._focused_app_id:
            self._focused_app_id = app_id
            if self._cb is not None and focused is not None:
                self._cb(WindowInfo(app_id=app_id, title=focused.title))

    def _on_readable(self) -> None:
        display = self._display
        if display is None:
            return
        try:
            display.read()
            display.dispatch(block=False)
            display.flush()
        except Exception:
            self.stop()

    def stop(self) -> None:
        if self._loop is not None and self._fd >= 0:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, OSError):
                pass
        self._fd = -1
        if self._display is not None:
            try:
                self._display.disconnect()
            except Exception:
                pass
        self._display = None
        self._info_manager = None
        self._toplevels.clear()
        self._cb = None
