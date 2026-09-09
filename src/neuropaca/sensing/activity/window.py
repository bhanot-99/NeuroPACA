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

**B16 · proxy lifetime, round two.** B15 held a strong ref to the
`zcosmic_toplevel_handle_v1` proxy (the `state`/activation carrier) but *not* to
its parent `ext_foreign_toplevel_handle_v1` (the `app_id`/`title` carrier), and
keyed both caches by `id(handle)`. pywayland retains proxies only weakly and runs
`wl_proxy_destroy` on GC, so the unreferenced parent handle died microseconds
after `_on_toplevel` returned — its later `app_id`/`title`/`closed` events landed
on a dead id and libwayland dropped them silently. Worse, once the parent was
collected its `id()` was reused, so a newly-opened window could evict a *live*
cosmic handle from the cache and kill a working subscription. Net effect in the
7-day soak: the connection dispatched **zero** events between the 180 s liveness
watchdog's forced reconnects — focus was effectively polled every 3 minutes.
B16 keeps a strong ref to **both** proxies, keyed by a monotonic int that is
never reused, for the whole life of the toplevel, and destroys them explicitly on
close so the compositor stops streaming to them.

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


def _safe_destroy(proxy: Any) -> None:
    """Best-effort `wl_proxy` destructor. The protocol says a client should
    `destroy` a handle once it is done with it (and always after `closed`);
    doing it explicitly — rather than waiting for GC to run `wl_proxy_destroy`
    via a reference cycle — is what actually tells the compositor to stop
    streaming events to that id."""
    destroy = getattr(proxy, "destroy", None)
    if destroy is None:
        return
    try:
        destroy()
    except Exception:  # a double-destroy or a torn-down display must not propagate
        _log.debug("toplevel handle destroy() raised", exc_info=True)


class WaylandWindowSource:
    def __init__(
        self,
        *,
        title_sensitive_app_ids: frozenset[str] = frozenset(),
        connection: WaylandConnection | None = None,
    ) -> None:
        self._cb: WindowCallback | None = None
        self._toplevel_list: Any = None
        self._info_manager: Any = None
        # B16 · one monotonic int per toplevel, bound into every dispatcher lambda
        # as a default arg. NEVER `id(handle)` — that is reused the instant the
        # (unreferenced) handle is collected, and a reused key silently evicts a
        # live subscription.
        self._next_key = 0
        self._toplevels: dict[int, _Toplevel] = {}
        # B16 · strong ref to the ext_foreign_toplevel_handle_v1 proxy — the
        # ONLY carrier of app_id / title / closed on this protocol version. B15
        # missed this one; without it the handle is GC'd (→ wl_proxy_destroy)
        # microseconds after creation and every later event for it is discarded.
        self._foreign_handles: dict[int, Any] = {}
        # B15 · strong ref to every zcosmic_toplevel_handle_v1 proxy — the carrier
        # of `state` (focus/activation). Kept for the toplevel's life, dropped on
        # close.
        self._cosmic_handles: dict[int, Any] = {}
        self._list_finished = False
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
        return self._conn.is_alive and not self._list_finished

    @property
    def tracked(self) -> int:
        """How many toplevels the source is currently holding proxies for. Used
        by the soak to confirm the caches drain as windows close (B16 §6.4)."""
        return len(self._foreign_handles)

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
        self._toplevel_list = toplevel_list
        self._info_manager = info
        toplevel_list.dispatcher["toplevel"] = self._on_toplevel
        toplevel_list.dispatcher["finished"] = self._on_list_finished
        self._reset_toplevels()
        self._list_finished = False
        self._focused_app_id = None
        self._focused_title = ""

    def primed(self) -> None:
        self._recompute_focus()

    def lost(self) -> None:
        self._toplevel_list = None
        self._info_manager = None
        self._reset_toplevels()
        self._list_finished = False
        self._focused_app_id = None
        self._focused_title = ""

    # ------------------------------------------------------ dispatcher callbacks
    def _on_toplevel(self, _list: Any, handle: Any) -> None:
        key = self._next_key
        self._next_key += 1
        self._toplevels[key] = _Toplevel()
        self._foreign_handles[key] = handle  # B16 — the strong ref B15 missed
        handle.dispatcher["app_id"] = lambda _h, app_id, k=key: self._set(k, "app_id", app_id)
        handle.dispatcher["title"] = lambda _h, title, k=key: self._set(k, "title", title)
        handle.dispatcher["closed"] = lambda _h, k=key: self._drop(k)
        cosmic_handle = self._info_manager.get_cosmic_toplevel(handle)
        self._cosmic_handles[key] = cosmic_handle
        cosmic_handle.dispatcher["state"] = lambda _ch, state, k=key: self._set(
            k, "activated", _STATE_ACTIVATED in list(state)
        )

    def _on_list_finished(self, _list: Any) -> None:
        # The compositor has retired the toplevel-list global (its own shutdown,
        # a compositor reload). Our cache is now stale; drop it, mark the window
        # half not-alive (surfaces in health), and ask the connection to rebind
        # so a re-advertised global is picked back up (B16 §3b).
        _log.warning("ext_foreign_toplevel_list_v1 finished — window cache invalidated, rebinding")
        self._reset_toplevels()
        self._list_finished = True
        self._focused_app_id = None
        self._focused_title = ""
        self._conn.request_reconnect()

    def _set(self, key: int, attr: str, value: Any) -> None:
        top = self._toplevels.get(key)
        if top is not None:
            setattr(top, attr, value)
            self._recompute_focus()

    def _drop(self, key: int) -> None:
        cosmic = self._cosmic_handles.pop(key, None)
        handle = self._foreign_handles.pop(key, None)
        removed = self._toplevels.pop(key, None) is not None
        if cosmic is not None:
            _safe_destroy(cosmic)
        if handle is not None:
            _safe_destroy(handle)
        if removed:
            self._recompute_focus()

    def _reset_toplevels(self) -> None:
        for handle in self._foreign_handles.values():
            _safe_destroy(handle)
        for cosmic in self._cosmic_handles.values():
            _safe_destroy(cosmic)
        self._toplevels.clear()
        self._foreign_handles.clear()
        self._cosmic_handles.clear()

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
