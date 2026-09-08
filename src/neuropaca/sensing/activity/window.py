# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""`WindowSource` — the focused window's `app_id` / `title` (B2.5b, D-9).

`WaylandWindowSource` binds `ext_foreign_toplevel_list_v1` (bundled) to enumerate
toplevels and the vendored `zcosmic_toplevel_info_v1` to learn which one is
`activated`. B15: it is a **protocol handler on a shared `WaylandConnection`** —
it owns no `Display` of its own, because two `Display` connections in one process
makes the second go deaf (see `wayland_conn.py`). Given no connection it creates
a private one (standalone use); the `ActivityCollector` passes in the connection
it also shares with `WaylandIdleSource`.

pywayland is optional + lazy-imported; a missing library, no compositor, or a
compositor without the protocol raises `CollectorError`, which `ActivityCollector`
turns into a graceful self-disable (rules.md §2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.activity.wayland_conn import WaylandConnection

__all__ = ["FakeWindowSource", "WaylandWindowSource", "WindowInfo", "WindowSource"]

_log = logging.getLogger(__name__)

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

    @property
    def is_alive(self) -> bool: ...


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

    @property
    def is_alive(self) -> bool:
        return self.started

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
    def __init__(
        self,
        *,
        title_sensitive_app_ids: frozenset[str] = frozenset(),
        connection: WaylandConnection | None = None,
    ) -> None:
        self._cb: WindowCallback | None = None
        self._info_manager: Any = None
        self._toplevels: dict[int, _Toplevel] = {}
        # B15 · hold a strong ref to every zcosmic_toplevel_handle_v1 proxy. Its
        # `state` dispatcher is the ONLY source of "which window is focused"; if
        # the proxy is only a local in `_on_toplevel` it can be GC'd before the
        # first `state` event arrives and that window becomes permanently
        # focus-invisible (the flaky ~1-in-3 deafness).
        self._cosmic_handles: dict[int, Any] = {}
        self._focused_app_id: str | None = None
        # B14 · for these app_ids (browsers) a *title* change is a real focus
        # change (a new tab) and must fire the callback; for everything else only
        # an app_id change does, so a terminal's animated title glyph or a
        # document's autosave-dirtied title stays a no-op.
        self._title_sensitive = title_sensitive_app_ids
        self._focused_title = ""
        # B15 · one shared Wayland connection. If none is supplied we own a
        # private one (standalone use); the collector passes in the connection it
        # also shares with WaylandIdleSource.
        self._owns_connection = connection is None
        self._conn = connection if connection is not None else WaylandConnection()
        self._conn.add(self)

    def start(self, on_switch: WindowCallback) -> None:
        self._cb = on_switch
        if self._owns_connection:
            self._conn.start()  # synchronous — raises CollectorError to self-disable

    @property
    def is_alive(self) -> bool:
        return self._conn.is_alive

    # ------------------------------------------- WaylandConnection protocol hooks
    def wants(self) -> dict[str, tuple[type, int]]:
        from pywayland.protocol.ext_foreign_toplevel_list_v1 import ExtForeignToplevelListV1

        from neuropaca.sensing.activity._protocols.cosmic_toplevel_info_unstable_v1 import (
            ZcosmicToplevelInfoV1,
        )

        return {
            "ext_foreign_toplevel_list_v1": (ExtForeignToplevelListV1, _TOPLEVEL_LIST_MAX_VERSION),
            "zcosmic_toplevel_info_v1": (ZcosmicToplevelInfoV1, _TOPLEVEL_INFO_MAX_VERSION),
        }

    def bound(self, globals_: dict[str, Any]) -> None:
        toplevel_list = globals_.get("ext_foreign_toplevel_list_v1")
        info = globals_.get("zcosmic_toplevel_info_v1")
        if toplevel_list is None or info is None:
            raise CollectorError(
                "compositor lacks ext_foreign_toplevel_list_v1 + zcosmic_toplevel_info_v1"
            )
        self._info_manager = info
        toplevel_list.dispatcher["toplevel"] = self._on_toplevel
        self._toplevels.clear()
        self._cosmic_handles.clear()
        self._focused_app_id = None
        self._focused_title = ""

    def primed(self) -> None:
        self._recompute_focus()

    def lost(self) -> None:
        self._info_manager = None
        self._toplevels.clear()
        self._cosmic_handles.clear()
        self._focused_app_id = None
        self._focused_title = ""

    # ------------------------------------------------------ dispatcher callbacks
    def _on_toplevel(self, _list: Any, handle: Any) -> None:
        key = id(handle)
        self._toplevels[key] = _Toplevel()
        handle.dispatcher["app_id"] = lambda h, app_id: self._set(id(h), "app_id", app_id)
        handle.dispatcher["title"] = lambda h, title: self._set(id(h), "title", title)
        handle.dispatcher["closed"] = lambda h: self._drop(id(h))
        cosmic_handle = self._info_manager.get_cosmic_toplevel(handle)
        self._cosmic_handles[key] = cosmic_handle  # strong ref — see __init__
        cosmic_handle.dispatcher["state"] = lambda _ch, state: self._set(
            key, "activated", _STATE_ACTIVATED in list(state)
        )

    def _set(self, key: int, attr: str, value: Any) -> None:
        top = self._toplevels.get(key)
        if top is not None:
            setattr(top, attr, value)
            self._recompute_focus()

    def _drop(self, key: int) -> None:
        self._cosmic_handles.pop(key, None)
        if self._toplevels.pop(key, None) is not None:
            self._recompute_focus()

    def _recompute_focus(self) -> None:
        focused = next((t for t in self._toplevels.values() if t.activated), None)
        if focused is None or not focused.app_id:
            return
        app_id, title = focused.app_id, focused.title
        changed = app_id != self._focused_app_id or (
            app_id in self._title_sensitive and title != self._focused_title
        )
        if not changed:
            return
        self._focused_app_id = app_id
        self._focused_title = title
        if self._cb is not None:
            self._cb(WindowInfo(app_id=app_id, title=title))

    def stop(self) -> None:
        if self._owns_connection:
            self._conn.stop()
        self._cb = None


# gen-ref: 208f5216
