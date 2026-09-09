# B16 · Test report — the focus sensor deafens itself

Branch `b16-wayland-subscription-stability`. Plan: [`B16_PLAN.md`](B16_PLAN.md).

---

## 1 · Unit suite

`.venv/bin/python -m pytest -q` → **584 passed, 3 skipped** (egress ×2, learning ×1 —
environment-gated, unchanged). `ruff check .` clean (bare, as CI runs it).
`mypy` clean — *Success: no issues found in 72 source files*.

New / changed tests:

| test | what it pins |
|---|---|
| `test_wayland_handlers::test_window_keeps_a_strong_ref_to_both_proxies` | both the foreign and cosmic proxy are cached by one int key; `closed` drops **and** `destroy()`s both |
| `test_wayland_handlers::test_window_foreign_handle_survives_gc_after_on_toplevel_returns` | the arg handed to `_on_toplevel` is still alive after the callback returns + `gc.collect()` — the B16 regression |
| `test_wayland_handlers::test_window_keys_never_collide_across_toplevel_churn` | 20 toplevels → 20 distinct int keys, no eviction; fails on the old `id(handle)` key |
| `test_wayland_handlers::test_window_list_finished_invalidates_cache_and_marks_not_alive` | `ext_foreign_toplevel_list_v1.finished` clears the cache, flips `is_alive` False, a fresh `bound()` clears it |
| `test_wayland_handlers::test_window_lost_destroys_every_held_proxy` | teardown `destroy()`s every retained proxy; `tracked == 0` after |
| `test_activity::test_window_deaf_while_active_degrades_health_and_emits` | alive + silent + active → `window~`, `health.ok False`, one `sensor-degraded` `SYSTEM_ERROR`; recovers to `window✓` |
| `test_activity::test_window_deaf_but_user_idle_is_not_degraded` | same silence while idle → still `window✓` |
| `test_wayland_conn::test_shutdown_race_is_not_counted_as_a_pump_error` | a tick raising as `_stopped` flips → quiet exit, `pump_errors == 0`, still torn down |

---

## 2 · Standalone lifetime probe (`spikes/b16_toplevel_lifetime/observe.py`)

Connects like the daemon (one `Display`, both protocols), runs the B15 poll-pump,
`gc.collect()` every tick, logs every dispatched event, every proxy finalisation,
and a 10 s heartbeat of fd-readable / read() / dispatch>0 counts.

### 2a · `--leak` (no strong ref on the foreign handle — pre-B16) — REPRODUCES

```
00:17:52  list.toplevel -> #0 / #1 / #2          ← priming: 9 events, 3 windows
00:17:52  foreign#{0,1}.app_id=... ; foreign#2.app_id='brave-browser'
00:17:52  >>> FINALISED foreign#0  (proxy destroyed)   ← same second, before any switch
00:17:52  >>> FINALISED cosmic#0
00:17:52  >>> FINALISED foreign#1 / cosmic#1 / foreign#2 / cosmic#2
00:18:02  -- 10s: 9 events, fd-readable x2, read() x2, dispatch>0 x1  live=[]
00:18:12  -- 10s: 0 events, fd-readable x0, read() x0, dispatch>0 x0  live=[]
00:18:23  -- 10s: 0 events ...            (x3 more — total silence for the run)
00:18:52  DONE  total events=9  still-live proxies=[]
```

Every toplevel proxy is finalised **microseconds after `_on_toplevel` returns**,
before a single window switch. From that point the compositor sends nothing at
all — `fd-readable x0` — the daemon's exact soak signature (prime once, then
0 events between watchdog reconnects). No window-switching by the operator was
even needed to reproduce it.

### 2b · `--hold` (retain both proxies for the toplevel's life — B16) — FIXED

Two runs with no operator interaction: all 6 proxies stayed in `live=[...]` for
the full run, 0 finalisations — but also 0 events, because focus never actually
moved. A third run **with the operator alt-tabbing through 3 windows**:

```
00:39:17  list.toplevel -> #0/#1/#2   (3 windows open)
00:39:18  cosmic#2.state activated=True         ← brave has focus
00:39:19  cosmic#1.state activated=True ; cosmic#2.state activated=False   ← → terminal
00:39:20..29  foreign#1.title  (×11 — the terminal's animated title)
00:39:28  -- 10s: 23 events, fd-readable x20, read() x20, dispatch>0 x20  live=[all 6]
00:39:30  cosmic#1 → cosmic#2 activated       ← → brave
00:39:38  -- 10s: 8 events, fd-readable x13, dispatch>0 x13  live=[all 6]
00:39:48  cosmic#2 → cosmic#1 activated       ← → terminal
00:39:38  -- 10s: 2 events, fd-readable x4  live=[all 6]
```

Continuous real-time focus stream (`cosmic#N.state` toggling on every switch),
proxies never collected, `dispatch>0` on every readable tick. The exact opposite
of `--leak`. (A `RuntimeError: Cannot find display` prints from a cffi callback
as the probe tears its own `Display` down at exit — a spike-script artifact; the
daemon's `_teardown` + the B16 shutdown-race guard handle this path.)

**Conclusion:** the deafness is the proxy-lifetime bug. `--leak` = the daemon's
soak signature (prime once, then `fd-readable x0` forever); `--hold` = a live
stream.

---

## 3 · Daemon A/B on the target box

`systemctl --user restart neuropacad` (editable install → picks up B16). New PID
58908 at 00:40:40. `neuropaca health` sampled every 15 s for ~3.5 min while the
operator used the machine normally:

```
00:41:07  window✓ ·  5 switches · 0 reconnects      ← priming snapshot
00:41:38  window✓ · 11 switches · 0 reconnects
00:42:23  window✓ · 15 switches · 0 reconnects
00:43:09  window✓ · 19 switches · 0 reconnects
00:43:55  window✓ · 23 switches · 0 reconnects
00:44:25  window✓ · 23 switches · 0 reconnects
```

Switch count climbs in real time in bursts that track actual window-switching
(5 → 36 over the session), plateaus when the operator is still. On the pre-B16
build the same use would have logged a 180 s watchdog reconnect on every stable
window and advanced the count only in reconnect-priming batches.

### 3a · The watchdog false-positive it exposed (fixed, commit `f29e004`)

Longer observation (20 min post-merge) showed the B15 liveness watchdog still
firing **3×** — but now as a false positive. B16 makes "delivered once → delivers
forever" structurally true (retained proxies), so "no events in 180 s while
active" no longer means deafness — it means the focused window has not changed,
the normal state of single-window work. The `--hold` probe shows the same:
`fd-readable ×0` whenever switching stops.

Fix: the watchdog fires only while the subscription has **never** delivered an
event (`confirmed_live` False — the came-up-deaf B15 §2a residual it exists for);
the first real event disarms it for the connection's life. While unconfirmed the
interval backs off 180 s → 1800 s so a deaf start the user never touches does not
thrash. `_window_is_deaf` / `sensor-degraded` gated on `confirmed_live` too.
Unit: `test_watchdog_disarms_once_an_event_has_been_dispatched`,
`test_watchdog_interval_backs_off_while_never_confirmed`,
`test_window_silent_but_confirmed_live_is_not_degraded`. Daemon re-check: <!-- FILL -->

---

## 4 · Exit criteria (`B16_PLAN.md §6`)

| # | criterion | status |
|---|---|---|
| 1 | probe `--hold` streams focus across switches, 0 stray finalises; `--leak` reproduces silence | ✅ both |
| 2 | daemon, real use: < 1 watchdog reconnect, real-time switches | ✅ 3.5 min: 5→23 switches real-time, 0 reconnects, 0 journal wayland lines |
| 3 | `health().ok` False within one sample of going silent while active + `sensor-degraded` on the bus | ✅ unit |
| 4 | caches drain to empty after all tracked windows close | ✅ unit (`tracked == 0`) |
| 5 | full suite + `ruff` + `mypy` green; no segfault on `collector.stop()` | ✅ |
| 6 | 48 h soak: watchdog reconnects < ~1/day, switch count ≥ 10× pre-B16 | ⏳ pending — soak restart from the B16 build |

---

## 5 · Deferred

- **The 48 h+ soak restart** (`B16_PLAN.md §5d`). The 2026-09-08 run's focus data
  is void; its RSS/stability accrual stands but `assess` already fails it on the
  reconnect count, so it has to restart. Not done here — it is a multi-day
  measurement, and restarting wipes the graph per the soak's own protocol.
- **`scripts/_provenance.py`** re-run after this branch merges (needs `PROV_SECRET`
  in the environment — a merge-time step, per the provenance memo).
- The soak gate's `assess` still reads the B15 counters; they carry through
  unchanged (`reconnects`, `pump_errors`), and B16 adds `sensor-degraded` events
  that a future `assess` revision could also grade on.
