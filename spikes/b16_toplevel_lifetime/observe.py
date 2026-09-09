# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B16 · standalone proof that the focus deafness is a proxy-lifetime bug.

Connects to the compositor exactly like the daemon does (ONE `Display`, both
`ext_foreign_toplevel_list_v1` + `zcosmic_toplevel_info_v1` bound), then runs the
B15 poll-pump for `--seconds` while logging:

  * every dispatched Wayland event as `interface.event`
  * every pywayland proxy finalisation (via `gc.callbacks`)
  * a periodic "N events in the last 10 s" heartbeat

Two modes:

  --leak   reproduce: the ext_foreign_toplevel_handle_v1 proxy is NOT retained
           (what `window.py` did before B16). Expect a finalise log within
           milliseconds of each `toplevel`, then `events=0` across manual window
           switches.
  --hold   candidate fix: retain BOTH proxies for the toplevel's life. Expect a
           continuous app_id / state stream and zero surprise finalisations.

No imports from `neuropaca/`. Run each mode once, switching windows and
opening/closing a scratch window throughout:

    .venv/bin/python spikes/b16_toplevel_lifetime/observe.py --leak  --seconds 90
    .venv/bin/python spikes/b16_toplevel_lifetime/observe.py --hold  --seconds 90
"""

from __future__ import annotations

import argparse
import gc
import select
import sys
import time
import weakref

from pywayland.client import Display
from pywayland.protocol.ext_foreign_toplevel_list_v1 import ExtForeignToplevelListV1

sys.path.insert(0, "src")
from neuropaca.sensing.activity._protocols.cosmic_toplevel_info_unstable_v1 import (
    ZcosmicToplevelInfoV1,
)

_POLL = 0.2
_ACTIVATED = 2


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class Observer:
    def __init__(self, *, hold: bool) -> None:
        self.hold = hold
        self.info_manager = None
        self.events = 0
        self.events_window = 0
        # --hold keeps these; --leak deliberately does not
        self._foreign: dict[int, object] = {}
        self._cosmic: dict[int, object] = {}
        self._next = 0
        self._live_names: set[str] = set()

    # ---- registry / bind -------------------------------------------------
    def bind(self, display: Display) -> None:
        registry = display.get_registry()
        got: dict[str, object] = {}
        wanted = {
            "ext_foreign_toplevel_list_v1": (ExtForeignToplevelListV1, 1),
            "zcosmic_toplevel_info_v1": (ZcosmicToplevelInfoV1, 3),
        }

        def on_global(_r, name, iface, version):
            spec = wanted.get(iface)
            if spec:
                cls, maxv = spec
                got[iface] = registry.bind(name, cls, min(version, maxv))

        registry.dispatcher["global"] = on_global
        display.roundtrip()
        tl = got.get("ext_foreign_toplevel_list_v1")
        self.info_manager = got.get("zcosmic_toplevel_info_v1")
        if tl is None or self.info_manager is None:
            raise SystemExit("compositor lacks the toplevel protocols")
        tl.dispatcher["toplevel"] = self._on_toplevel
        tl.dispatcher["finished"] = lambda _l: print(f"{_ts()}  list.finished")
        self._toplevel_list = tl  # always retain the list itself
        for _ in range(2):
            display.roundtrip()

    # ---- dispatcher callbacks -----------------------------------------
    def _on_toplevel(self, _list, handle) -> None:
        key = self._next
        self._next += 1
        self._track_finalise(handle, f"foreign#{key}")
        handle.dispatcher["app_id"] = lambda _h, app_id, k=key: self._evt(
            f"foreign#{k}.app_id={app_id!r}"
        )
        handle.dispatcher["title"] = lambda _h, title, k=key: self._evt(f"foreign#{k}.title")
        handle.dispatcher["closed"] = lambda _h, k=key: self._closed(k)
        cosmic = self.info_manager.get_cosmic_toplevel(handle)
        self._track_finalise(cosmic, f"cosmic#{key}")
        cosmic.dispatcher["state"] = lambda _c, state, k=key: self._evt(
            f"cosmic#{k}.state activated={_ACTIVATED in list(state)}"
        )
        if self.hold:
            self._foreign[key] = handle
            self._cosmic[key] = cosmic
        self._evt(f"list.toplevel -> #{key}")

    def _closed(self, key: int) -> None:
        self._foreign.pop(key, None)
        self._cosmic.pop(key, None)
        self._evt(f"foreign#{key}.closed")

    def _evt(self, what: str) -> None:
        self.events += 1
        self.events_window += 1
        print(f"{_ts()}  {what}")

    def _track_finalise(self, obj: object, label: str) -> None:
        self._live_names.add(label)

        def _gone(_ref, label=label) -> None:
            self._live_names.discard(label)
            print(f"{_ts()}  >>> FINALISED {label}  (proxy destroyed)")

        weakref.ref(obj, _gone)


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--leak", action="store_true", help="do not retain the handle (pre-B16)")
    mode.add_argument("--hold", action="store_true", help="retain both proxies (B16 fix)")
    ap.add_argument("--seconds", type=float, default=90.0)
    args = ap.parse_args()

    obs = Observer(hold=args.hold)
    display = Display()
    display.connect()
    obs.bind(display)
    display.flush()
    fd = display.get_fd()

    mode_name = "HOLD" if args.hold else "LEAK"
    print(f"{_ts()}  mode={mode_name}  — switch windows / open+close a scratch window now")
    end = time.time() + args.seconds
    last_beat = time.time()
    while time.time() < end:
        if select.select([fd], [], [], 0)[0]:
            try:
                display.read()
            except RuntimeError as exc:
                print(f"{_ts()}  read() raised: {exc}")
                break
        display.dispatch(block=False)
        display.flush()
        gc.collect()  # force the finaliser race the daemon hits non-deterministically
        now = time.time()
        if now - last_beat >= 10.0:
            print(
                f"{_ts()}  -- {obs.events_window} events in 10s "
                f"(live proxies: {sorted(obs._live_names)})"
            )
            obs.events_window = 0
            last_beat = now
        time.sleep(_POLL)

    print(f"{_ts()}  DONE  total events={obs.events}  still-live proxies={sorted(obs._live_names)}")
    display.disconnect()


if __name__ == "__main__":
    main()
