# B2.5 · Process & Activity Sensing — blocker spike

Throwaway. De-risks the two hard parts of B2.5 on **Wayland + COSMIC** (the
target box: Pop!_OS, `cosmic-comp` 0.1~24.04, `XDG_SESSION_TYPE=wayland`,
`XDG_CURRENT_DESKTOP=COSMIC`). `problems.md` 1.7 said "the active window is
OS-specific and harder than it looks" and D-7/D-8 deferred `ActivityCollector`
entirely — this spike found the actual APIs.

## What the box exposes (verified `wl_registry` globals)

| Wayland global | Version | Use |
| --- | --- | --- |
| `ext_idle_notifier_v1` | 2 | **idle / activity edges** — bundled in `pywayland` |
| `wl_seat` | 9 | required by `get_idle_notification(timeout, seat)` |
| `zcosmic_toplevel_info_v1` | 3 | **active window** (`app_id`, `title`, `state=activated`) — needs vendored XML |
| `ext_foreign_toplevel_list_v1` | 1 | toplevel enumeration (no focus state) — bundled |

`org.freedesktop.ScreenSaver` (owned by `cosmic-idle`) implements only
`Inhibit`/`UnInhibit` — **no** `GetSessionIdleTime` / `GetActiveTime`. X11 via
XWayland is dead: `xprop -root _NET_ACTIVE_WINDOW` -> `0x0`. So: Wayland
protocols, not D-Bus, not X11.

## Blocker 1 — idle seconds / idle-state · SOLVED

`spike_idle_notify.py` — binds `ext_idle_notifier_v1`, creates a
`get_idle_notification(2000, seat)`, runs a `select()` + `Display.read()` +
`Display.dispatch(block=False)` loop. **Result: `['idled', 'resumed']`** — the
protocol fires on real input transitions. Zero vendored XML (pywayland bundles
`ext_idle_notify_v1`). asyncio: `loop.add_reader(display.get_fd(), _on_readable)`.

Caveat: pywayland's cffi dispatcher wants handlers to return `None` — don't use
tuple-lambda handlers (the spike's harmless `TypeError: an integer is required`).

## Blocker 2 — active window · feasible, deferred to B2.5b

`cosmic-toplevel-info.xml` (fetched from `pop-os/cosmic-protocols@main`, matches
the box's advertised v3). v2+ style: get an `ext_foreign_toplevel_handle_v1`
from `ext_foreign_toplevel_list_v1`, then
`zcosmic_toplevel_info_v1.get_cosmic_toplevel(handle)` -> `state` event carries
`activated`. `python -m pywayland.scanner` needs the **full** dependency chain
passed together (`wayland.xml`, `ext-foreign-toplevel-list-v1.xml`,
`ext-workspace-v1.xml`) — it failed with `KeyError: 'wl_output'` on the cosmic
XML alone. Solvable, but its own build-pipeline task -> B2.5b.

## Decision -> D-9 (memory.md)

Split B2.5:
- **B2.5a** — idle sensing only. Real `IDLE_DETECTED` / `ACTIVITY_DETECTED` from
  `ext-idle-notify-v1`, replacing the CPU-derived stand-in; top-N process-by-CPU
  in `SystemMetricCollector`. `pywayland` as an optional extra, lazy-imported,
  collector self-disables when absent / headless.
- **B2.5b** — `zcosmic_toplevel_info_v1` active-window, `app_map.toml`,
  `APP_SWITCH`, `FocusSessionPattern` / `DistractionPattern`, domain
  classification + `bridge_value`.
