# B15 · The Wayland activity sensor goes deaf — one shared connection

**Branch:** `fix/wayland-poll-pump` (off `feat/brave-webapp-attribution` — lands after B14)
**Found:** 2026-09-08, during the B14 live smoke test.
**Status: FIXED + verified** (unit + autonomous live). See `B15_TEST_REPORT.md`.

---

## 1 · Symptom

The long-running `neuropacad` process barely registers window/focus events:

- B7 burned three soaks with L5 firing **zero** times.
- B9 soak gate saw **4 app switches in a full hour** of real use.
- B14 live test: the daemon produced **0** `webapp:` nodes and **1 switch** in a
  session, while a freshly-constructed `ActivityCollector` on the *identical
  code*, run from an interactive shell, caught **7** switches with correct labels
  in 50 seconds. The collector reported `window✓` throughout — it was **deaf**,
  not dead.

## 2 · Root cause

Three distinct bugs, found in this order (the first was the hardest to see):

### 2a · GC'd `zcosmic_toplevel_handle_v1` proxies — the flaky deafness

`_on_toplevel` did `cosmic_handle = self._info_manager.get_cosmic_toplevel(handle)`
and set `cosmic_handle.dispatcher["state"] = ...` — but `cosmic_handle` was only a
**local variable**. Nothing held a strong reference. Python's GC collected it
non-deterministically, and a collected pywayland proxy silently stops delivering
its events — so that window's `state` (focus/activation) changes became
**permanently invisible**. Measured on the daemon: **~1 in 3 starts deaf**, binary
per start (GC either ran between setup and the first event or it didn't). This is
the mechanism behind B7's zero-L5 soaks and B13's 4-switch-an-hour gate; it has
been in `window.py` since B2.5b. **Fix:** a `self._cosmic_handles: dict[int, Any]`
keyed by toplevel, dropped in `_drop`. 10/10 daemon restarts clean after.

### 2b · Two `pywayland.Display` connections in one process — the segfault

`ActivityCollector` opened `WaylandIdleSource` **and** `WaylandWindowSource` as
two independent `Display` connections. Run directly (no systemd, in-session):

| collector shape | 25 forced changes | teardown |
|---|---|---|
| real window source + `FakeIdleSource` (**one** connection) | ~7 events | clean |
| both real (**two** connections) | 1 event | **SIGSEGV (exit 139)** |

The two cffi `Display` objects crash on finalize, and the second connection's
event delivery is unreliable. libwayland is **one connection per client**. **Fix:**
one shared `WaylandConnection` for both protocols.

### 2c · Silent, permanent, invisible death (latent, B7-shaped)

`_on_readable` (both sources) caught **every** exception and permanently
`self.stop()`d the connection — no log — and `ActivityCollector._window_ok` was
never updated, so `health()` kept printing `window✓` for a dead sensor.
pywayland's `loop.add_reader` integration is also known-fragile
(flacjacket/pywayland#16), and `_on_readable` only dispatched **when the fd was
readable**, where the working B2.5 spike dispatches **unconditionally every
tick**. **Fix:** the poll-pump (§3c) with `_log.exception` + bounded reconnect,
and `health()` reads `is_alive` live.

### 2d · Investigation note — a phantom worth recording

~2 h were lost to a false lead: `XDG_SESSION_ID`. A `systemd --user` service's
environment lacks it, and early daemon tests with it hardcoded seemed to restore
the focus stream. They did not — **`systemctl --user restart` was silently not
taking effect** for a stretch (a daemon from a `systemctl start` at 17:31 kept
running the old code; `ExecMainStartTimestamp` never advanced). A clean A/B —
`stop` → confirm the PID is gone → `start` → confirm a new PID — showed the fixed
code works with or without the var. **No unit change.** Lesson: when a systemd
restart "does nothing", check `MainPID` / `ExecMainStartTimestamp` before
attributing behaviour to config.

---

## 3 · Fixes

### 3a · Strong-ref the cosmic toplevel handles (`window.py`)

`WaylandWindowSource._cosmic_handles: dict[int, Any]` holds every
`get_cosmic_toplevel(...)` proxy for the life of its toplevel. Cleared in
`bound()` / `lost()`, entry dropped in `_drop()`. **This is the fix for the
flaky deafness** — 10/10 clean daemon restarts, was ~1/3 deaf.

### 3b · One shared `WaylandConnection` (`sensing/activity/wayland_conn.py`, new)

Idle-notify and toplevel-info bind on **one** `Display`, share one fd, one
poll-pump. `WaylandIdleSource` / `WaylandWindowSource` became
`WaylandProtocolHandler`s — `wants()` / `bound()` / `primed()` / `lost()` — that
attach to a connection instead of owning one (given none, they make a private
one, for standalone use). `ActivityCollector` builds the one connection and
passes it to both. This is the fix; it also erases the segfault.

### 3c · The poll-pump (mirrors the working spike)

`_connect()` does `_PRIME_ROUNDTRIPS` (2) roundtrips then `primed()`. The pump,
every `_POLL_INTERVAL_SECONDS` (0.2 s): non-blocking `select`, `read()` **only**
when the fd is readable, then **always** `dispatch(block=False)` + `flush()`.
Cost: ~5 Hz of a non-blocking syscall + a queue drain — nil. (A warm-up that did
a `roundtrip()` per tick before `primed()` was tried and *increased* the deafness
rate — the real cause was 2a, not a drain race — so it was dropped.)

### 3d · Resilience + honest health + a liveness watchdog

- a pump tick that raises → `_log.exception`, `handler.lost()`, teardown, then
  **bounded reconnect** (`2, 4, 8, 16, 32 s`); on success it re-runs
  `bound()` + `primed()` and logs recovery; budget spent → give up and
  `is_alive` goes False.
- **liveness watchdog** for the residual ~1/20 startup race (2a): if the pump has
  dispatched **zero** events for `_STALE_RECONNECT_SECONDS` (180 s) **while an
  `activity_probe` says the user is active** (`ActivityCollector` wires this to
  `not self._idle`), force one reconnect — cheap, re-rolls the subscription, and
  `_last_event_at` is reset on connect so it fires at most once per 180 s. On an
  idle machine the probe is False and it never fires.
- `ActivityCollector.health()` reads `source.is_alive` **live**: `window✓` only
  while the pump is running and connected. A source that started and then died
  drags the module `health().ok` to False (a source that never started stays
  tolerated, unchanged).

**No systemd unit change.**

---

## 4 · Files

```
NEW   src/neuropaca/sensing/activity/wayland_conn.py     WaylandConnection + WaylandProtocolHandler
EDIT  src/neuropaca/sensing/activity/window.py           WaylandWindowSource -> protocol handler
EDIT  src/neuropaca/sensing/activity/wayland_idle.py     WaylandIdleSource  -> protocol handler
EDIT  src/neuropaca/sensing/activity/idle.py             IdleSource.is_alive + Fake
EDIT  src/neuropaca/sensing/activity/collector.py        one shared connection; live is_alive in health()
NEW   tests/test_wayland_conn.py                         pump cadence, reconnect, connect flow (fake pywayland)
NEW   tests/test_wayland_handlers.py                     window/idle handler hooks + callbacks
EDIT  tests/test_activity.py                             shared-connection wiring, health died/live
NEW   scripts/b15_live_check.py                          autonomous live check (zenity-forced focus)
EDIT  phases.md                                          B15 entry
```

## 5 · Verification (full run in `B15_TEST_REPORT.md`)

- **Unit:** `test_wayland_conn.py` + `test_wayland_handlers.py` + `test_activity.py`
  additions. Full suite 561 pass, `ruff` + `mypy` clean.
- **Live (`scripts/b15_live_check.py`, autonomous):** a real `ActivityCollector`
  on the real compositor — 5 forced focus changes → **10** APP_SWITCH events,
  payload carries the B14 fields and no raw title, health stays green, shared
  connection tears down without segfault. Repeated ×2.
- **Daemon flakiness A/B** (the load-bearing one for 2a — `stop` → confirm PID
  gone → `start` → 3 forced focus changes):
  - before the `_cosmic_handles` ref: **~1 in 3 restarts deaf** (delta 0–1)
  - after: **0 / 10 deaf**, then **0 / 20** on a confidence run — every restart
    delta 6, base 1
- **Daemon (`XDG_SESSION_ID` A/B):** with and without → identical; the var is
  irrelevant.

## 6 · Exit criteria

1. Daemon records **dozens** of focus/tab switches per hour, not ~4, and
   consistently across restarts. ✅ (20/20 forced-focus restarts)
2. `ActivityCollector.health().ok` goes False within one poll interval of the
   Wayland connection dropping, and recovers when it comes back. ✅ (unit)
3. A forced transient (`read()` raises once) does not disable the source. ✅ (unit)
4. Full suite + `ruff` + `mypy` green. ✅
5. No segfault on `collector.stop()`. ✅

## 7 · Deferred

- **Fully eliminating the ~1/20 startup race.** The `_cosmic_handles` ref took it
  from ~1/3 to ~1/20; the liveness watchdog (§3d) self-heals what's left within
  180 s of user activity. A guaranteed fix would be a **dedicated Wayland thread**
  doing `wl_display_dispatch(block=True)` (the canonical libwayland pattern) and
  marshalling events via `loop.call_soon_threadsafe` — a bigger change to
  load-bearing code, and `rules.md §3` says "no thread". Worth it if the watchdog
  proves insufficient in the soak.
- The B9 soak gate's liveness check still keys on the old counters; a re-run on
  the target box during a real session window should show an order-of-magnitude
  more switches.
- A real browser-tab `webapp:` node was verified live earlier in the session
  (`webapp:gmail` / `crunchyroll` / `claude` / `jiohotstar` from a standalone
  collector); an autonomous browser-driven check is not included here because
  closing a specific Brave window from the CLI on Wayland is not clean.
