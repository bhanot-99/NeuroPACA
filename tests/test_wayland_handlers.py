# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B15 · `WaylandWindowSource` and `WaylandIdleSource` as protocol handlers.

Both are now `WaylandProtocolHandler`s bound on a shared `WaylandConnection`
rather than owners of a `Display`. These tests drive the handler hooks
(`wants` / `bound` / `primed` / `lost`) and the event callbacks directly with
fake wayland proxies — no compositor, no pywayland import for the hook tests
(`wants()` is the one method that imports pywayland, guarded with importorskip).
"""

from __future__ import annotations

import gc
from typing import Any

import pytest

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.activity.idle import IdleTransition
from neuropaca.sensing.activity.wayland_conn import WaylandConnection
from neuropaca.sensing.activity.wayland_idle import WaylandIdleSource
from neuropaca.sensing.activity.window import WaylandWindowSource, WindowInfo

# --------------------------------------------------------------------- fakes


class _FakeProxy:
    def __init__(self) -> None:
        self.dispatcher: dict[str, Any] = {}


class _FakeCosmicHandle:
    def __init__(self) -> None:
        self.dispatcher: dict[str, Any] = {}
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


class _FakeInfoManager:
    def __init__(self) -> None:
        self.handles: list[_FakeCosmicHandle] = []

    def get_cosmic_toplevel(self, _handle: Any) -> _FakeCosmicHandle:
        h = _FakeCosmicHandle()
        self.handles.append(h)
        return h


class _FakeToplevelHandle:
    """Stands in for an ext_foreign_toplevel_handle_v1 proxy."""

    def __init__(self) -> None:
        self.dispatcher: dict[str, Any] = {}
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


# ==================================================== WaylandWindowSource ===


def _window(**kw) -> WaylandWindowSource:
    return WaylandWindowSource(connection=WaylandConnection(), **kw)


def _bind_window(src: WaylandWindowSource) -> tuple[_FakeProxy, _FakeInfoManager]:
    toplevel_list = _FakeProxy()
    info = _FakeInfoManager()
    src.bound({"ext_foreign_toplevel_list_v1": toplevel_list, "zcosmic_toplevel_info_v1": info})
    return toplevel_list, info


def _add_toplevel(
    src: WaylandWindowSource, toplevel_list: _FakeProxy, *, app_id: str, title: str, activated: bool
) -> None:
    handle = _FakeToplevelHandle()
    toplevel_list.dispatcher["toplevel"](toplevel_list, handle)
    handle.dispatcher["app_id"](handle, app_id)
    handle.dispatcher["title"](handle, title)
    cosmic = src._info_manager.handles[-1]
    cosmic.dispatcher["state"](cosmic, [2] if activated else [])


def test_window_wants_the_two_toplevel_interfaces() -> None:
    pytest.importorskip("pywayland")
    src = _window()
    keys = set(src.wants())
    assert keys == {"ext_foreign_toplevel_list_v1", "zcosmic_toplevel_info_v1"}


def test_window_bound_stores_the_info_manager_and_sets_the_dispatcher() -> None:
    src = _window()
    toplevel_list, info = _bind_window(src)
    assert src._info_manager is info
    assert "toplevel" in toplevel_list.dispatcher


def test_window_bound_raises_when_a_global_is_missing() -> None:
    src = _window()
    with pytest.raises(CollectorError, match="ext_foreign_toplevel_list_v1"):
        src.bound({"zcosmic_toplevel_info_v1": _FakeInfoManager()})


def test_window_fires_callback_on_app_id_change() -> None:
    seen: list[WindowInfo] = []
    src = _window()
    src.start(seen.append)
    toplevel_list, _ = _bind_window(src)

    _add_toplevel(src, toplevel_list, app_id="term", title="a", activated=True)
    assert [w.app_id for w in seen] == ["term"]

    # second window takes focus
    _add_toplevel(src, toplevel_list, app_id="brave-browser", title="Gmail", activated=True)
    # ...but the first is still marked activated in our fake — the *newest*
    # activated wins in insertion order? _recompute_focus takes the first match.
    # De-activate the terminal so focus actually moves.
    term_key = next(iter(src._toplevels))
    src._set(term_key, "activated", False)
    assert seen[-1].app_id == "brave-browser"


def test_window_dedup_no_fire_when_nothing_changed() -> None:
    seen: list[WindowInfo] = []
    src = _window()
    src.start(seen.append)
    toplevel_list, _ = _bind_window(src)
    _add_toplevel(src, toplevel_list, app_id="term", title="a", activated=True)
    n = len(seen)
    # a pure title tick on a non-title-sensitive app → no event
    key = next(iter(src._toplevels))
    src._set(key, "title", "a v2")
    src._set(key, "title", "a v3")
    assert len(seen) == n


def test_window_title_sensitivity_only_for_configured_browsers() -> None:
    seen: list[WindowInfo] = []
    src = _window(title_sensitive_app_ids=frozenset({"brave-browser"}))
    src.start(seen.append)
    toplevel_list, _ = _bind_window(src)

    _add_toplevel(src, toplevel_list, app_id="brave-browser", title="Gmail", activated=True)
    key = next(iter(src._toplevels))
    src._set(key, "title", "YouTube")  # browser + title change → event
    src._set(key, "title", "GitHub")
    assert [w.title for w in seen] == ["Gmail", "YouTube", "GitHub"]

    # a non-browser terminal in the same source does NOT fire on title
    th = _FakeToplevelHandle()
    toplevel_list.dispatcher["toplevel"](toplevel_list, th)
    th.dispatcher["app_id"](th, "term")
    src._info_manager.handles[-1].dispatcher["state"](None, [2])
    src._set(key, "activated", False)  # brave loses focus, term has it
    assert seen[-1].app_id == "term"  # the app_id switch fired
    before = len(seen)
    th.dispatcher["title"](th, "spinner tick")
    assert len(seen) == before  # a title tick on a non-browser is inert


def test_window_keeps_a_strong_ref_to_both_proxies() -> None:
    # B15 kept the cosmic handle (carries `state`); B16 also keeps the parent
    # ext_foreign_toplevel_handle_v1 (carries app_id/title/closed). Both must be
    # retained for the toplevel's life and dropped + destroyed on close.
    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    handle = _FakeToplevelHandle()
    toplevel_list.dispatcher["toplevel"](toplevel_list, handle)
    (key,) = src._foreign_handles  # one int key
    assert src._foreign_handles[key] is handle
    assert src._cosmic_handles[key] is src._info_manager.handles[-1]
    cosmic = src._cosmic_handles[key]
    handle.dispatcher["closed"](handle)
    assert key not in src._cosmic_handles and key not in src._foreign_handles
    assert handle.destroyed == 1 and cosmic.destroyed == 1


def test_window_foreign_handle_survives_gc_after_on_toplevel_returns() -> None:
    # The B16 bug: pywayland retains proxies only weakly and runs wl_proxy_destroy
    # on GC, so an unreferenced handle dies microseconds after _on_toplevel and
    # every later event for it is dropped. _foreign_handles is the strong ref.
    import weakref

    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)

    def add_one() -> weakref.ref[Any]:
        h = _FakeToplevelHandle()
        toplevel_list.dispatcher["toplevel"](toplevel_list, h)
        return weakref.ref(h)  # `h` goes out of scope here — like pywayland's arg

    ref = add_one()
    gc.collect()
    handle = ref()
    assert handle is not None, "foreign toplevel handle was collected — B16 regression"
    # and its app_id dispatcher still routes to the right toplevel
    (key,) = src._foreign_handles
    handle.dispatcher["app_id"](handle, "term")
    assert src._toplevels[key].app_id == "term"


def test_window_keys_never_collide_across_toplevel_churn() -> None:
    # The old code keyed the caches by id(handle); once the (unreferenced) handle
    # was collected its id() was reused and a new window evicted a live handle.
    # The monotonic-int key must give every toplevel — past or present — a
    # distinct slot.
    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    seen_keys: set[int] = set()
    for i in range(20):
        h = _FakeToplevelHandle()
        toplevel_list.dispatcher["toplevel"](toplevel_list, h)
        new = set(src._foreign_handles) - seen_keys
        assert len(new) == 1
        (k,) = new
        seen_keys.add(k)
        h.dispatcher["app_id"](h, f"app{i}")
        src._info_manager.handles[-1].dispatcher["state"](None, [2] if i == 5 else [])
    # every one of the 20 is still tracked with its own cosmic handle
    assert len(src._foreign_handles) == 20
    assert len(src._cosmic_handles) == 20
    assert src._toplevels[sorted(seen_keys)[5]].activated is True


def test_window_drop_recomputes_focus() -> None:
    seen: list[WindowInfo] = []
    src = _window()
    src.start(seen.append)
    toplevel_list, _ = _bind_window(src)
    handle = _FakeToplevelHandle()
    toplevel_list.dispatcher["toplevel"](toplevel_list, handle)
    handle.dispatcher["app_id"](handle, "term")
    src._info_manager.handles[-1].dispatcher["state"](None, [2])
    assert seen[-1].app_id == "term"
    handle.dispatcher["closed"](handle)  # window closed
    assert src._toplevels == {}


def test_window_lost_and_bound_clear_the_cosmic_handles() -> None:
    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    _add_toplevel(src, toplevel_list, app_id="term", title="a", activated=True)
    assert src._cosmic_handles
    src.lost()
    assert src._cosmic_handles == {}


def test_window_lost_clears_all_state() -> None:
    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    _add_toplevel(src, toplevel_list, app_id="term", title="a", activated=True)
    assert src._toplevels
    src.lost()
    assert src._toplevels == {}
    assert src._info_manager is None
    assert src._focused_app_id is None


def test_window_list_finished_invalidates_cache_and_marks_not_alive() -> None:
    # B16 §3b — the compositor retiring the toplevel-list global must not leave
    # the source reporting a focus that can no longer change.
    conn = WaylandConnection()
    conn._connected = True

    class _T:
        def done(self) -> bool:
            return False

    conn._task = _T()  # type: ignore[assignment]
    src = WaylandWindowSource(connection=conn)
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    _add_toplevel(src, toplevel_list, app_id="term", title="a", activated=True)
    assert src.is_alive is True

    toplevel_list.dispatcher["finished"](toplevel_list)
    assert src._toplevels == {}
    assert src._foreign_handles == {} and src._cosmic_handles == {}
    assert src.is_alive is False  # forces the connection watchdog to reconnect
    src.bound(
        {
            "ext_foreign_toplevel_list_v1": toplevel_list,
            "zcosmic_toplevel_info_v1": _FakeInfoManager(),
        }
    )
    assert src.is_alive is True  # a fresh bind clears the flag


def test_window_lost_destroys_every_held_proxy() -> None:
    src = _window()
    src.start(lambda w: None)
    toplevel_list, _ = _bind_window(src)
    _add_toplevel(src, toplevel_list, app_id="a", title="", activated=True)
    _add_toplevel(src, toplevel_list, app_id="b", title="", activated=False)
    foreign = list(src._foreign_handles.values())
    cosmic = list(src._cosmic_handles.values())
    src.lost()
    assert all(h.destroyed == 1 for h in foreign)
    assert all(h.destroyed == 1 for h in cosmic)
    assert src.tracked == 0


def test_window_is_alive_delegates_to_the_connection() -> None:
    conn = WaylandConnection()
    src = WaylandWindowSource(connection=conn)
    assert src.is_alive is conn.is_alive is False
    conn._connected = True

    class _T:
        def done(self):
            return False

    conn._task = _T()  # type: ignore[assignment]
    assert src.is_alive is True


def test_window_shared_mode_start_does_not_touch_the_connection() -> None:
    conn = WaylandConnection()
    started = {"n": 0}
    conn.start = lambda: started.__setitem__("n", started["n"] + 1)  # type: ignore[method-assign]
    src = WaylandWindowSource(connection=conn)
    src.start(lambda w: None)
    assert started["n"] == 0  # the collector owns conn.start(), not the source


# ====================================================== WaylandIdleSource ===


def _idle(threshold: int = 300) -> WaylandIdleSource:
    return WaylandIdleSource(threshold, connection=WaylandConnection())


def test_idle_wants_notifier_and_seat() -> None:
    pytest.importorskip("pywayland")
    assert set(_idle().wants()) == {"ext_idle_notifier_v1", "wl_seat"}


def test_idle_bound_raises_when_a_global_is_missing() -> None:
    src = _idle()
    with pytest.raises(CollectorError, match="ext_idle_notifier_v1"):
        src.bound({"wl_seat": object()})


def test_idle_primed_creates_the_notification_and_wires_dispatchers() -> None:
    src = _idle(threshold=42)

    class _Notif:
        def __init__(self) -> None:
            self.dispatcher: dict[str, Any] = {}

    class _Notifier:
        def __init__(self) -> None:
            self.calls: list[tuple[int, Any]] = []

        def get_idle_notification(self, ms: int, seat: Any) -> _Notif:
            self.calls.append((ms, seat))
            return _Notif()

    notifier, seat = _Notifier(), object()
    src.bound({"ext_idle_notifier_v1": notifier, "wl_seat": seat})
    src.primed()
    assert notifier.calls == [(42_000, seat)]
    assert set(src._notification.dispatcher) == {"idled", "resumed"}


def test_idle_transitions_fire_the_callback() -> None:
    seen: list[IdleTransition] = []
    src = _idle()
    src.start(seen.append)
    src._on_idled(None)
    src._on_resumed(None)
    assert seen == [IdleTransition.IDLE, IdleTransition.ACTIVE]


def test_idle_lost_fails_safe_to_active() -> None:
    seen: list[IdleTransition] = []
    src = _idle()
    src.start(seen.append)
    src.lost()
    assert seen == [IdleTransition.ACTIVE]
    assert src._notifier is None and src._notification is None


def test_idle_is_alive_delegates_to_the_connection() -> None:
    conn = WaylandConnection()
    src = WaylandIdleSource(300, connection=conn)
    assert src.is_alive is False
    conn._connected = True

    class _T:
        def done(self):
            return False

    conn._task = _T()  # type: ignore[assignment]
    assert src.is_alive is True


# =============================================== shared connection wiring ===


def test_two_handlers_register_on_one_connection() -> None:
    conn = WaylandConnection()
    WaylandIdleSource(300, connection=conn)
    WaylandWindowSource(connection=conn)
    assert len(conn._handlers) == 2


# gen-ref: 00631d39
