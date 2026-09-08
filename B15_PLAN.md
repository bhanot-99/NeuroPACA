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

### 2a · Two `pywayland.Display` connections in one process (the cause)

`ActivityCollector` opened `WaylandIdleSource` **and** `WaylandWindowSource` as
two independent `Display` connections. Measured, running the sources directly
(no systemd, in an interactive session):

| collector shape | forced focus/title changes | events seen | teardown |
|---|---|---|---|
| real window source + `FakeIdleSource` (**one** connection) | 25 | **~7** | clean |
| both real (**two** connections) | 25 | **1** | **SIGSEGV (exit 139)** |

The second `Display`'s toplevel/`state` stream stops reaching the handlers, and
the two cffi `Display` objects crash on finalize. libwayland is **one connection
per client**; nothing in the docs supports two.

pywayland's asyncio + `loop.add_reader` integration is also a known-fragile
upstream area (flacjacket/pywayland#16 — the `global` callback never fires until
a blocking `dispatch()` / `roundtrip()`), and `_on_readable` only called
`dispatch(block=False)` **when the fd was readable**, where the working B2.5
spike (`spikes/b2_5_activity/spike_idle_notify.py`) calls it **unconditionally
every tick**.

### 2b · Silent, permanent, invisible death (latent, B7-shaped)

`_on_readable` (both sources) caught **every** exception and permanently
`self.stop()`d the connection — no log — and `ActivityCollector._window_ok` was
never updated, so `health()` kept printing `window✓` for a dead sensor.

### 2c · Investigation note — a phantom worth recording

~2 h were lost to a false lead: `XDG_SESSION_ID`. A `systemd --user` service's
environment lacks it, and it looked like cosmic-comp was withholding the focus
stream from a "sessionless" client — early daemon tests with the var hardcoded
seemed to fix it. It did not. **`systemctl --user restart` was silently not
taking effect** for a stretch (a daemon started by `systemctl start` at 17:31
kept running with the old two-connection code while drop-in edits changed
nothing about the live process). A clean A/B — `stop` → confirm the PID is gone
→ `start` → confirm a new PID/timestamp — showed the fixed shared-connection
code registers focus changes **with or without `XDG_SESSION_ID`** (1 → 13
switches either way). No unit change is needed. Lesson: when a systemd restart
"does nothing", verify MainPID actually changed before attributing behaviour to
the config.

---

## 3 · Fixes

### 3a · One shared `WaylandConnection` (`sensing/activity/wayland_conn.py`, new)

Idle-notify and toplevel-info bind on **one** `Display`, share one fd, one
poll-pump. `WaylandIdleSource` / `WaylandWindowSource` became
`WaylandProtocolHandler`s — `wants()` / `bound()` / `primed()` / `lost()` — that
attach to a connection instead of owning one (given none, they make a private
one, for standalone use). `ActivityCollector` builds the one connection and
passes it to both. This is the fix; it also erases the segfault.

### 3b · The poll-pump (mirrors the working spike)

Every `_POLL_INTERVAL_SECONDS` (0.2 s): non-blocking `select`, `read()` **only**
when the fd is readable, then **always** `dispatch(block=False)` + `flush()`.
Cost: ~5 Hz of a non-blocking syscall + a queue drain — nil.

### 3c · Resilience + honest health

- a pump tick that raises → `_log.exception`, `handler.lost()`, teardown, then
  **bounded reconnect** (`2, 4, 8, 16, 32 s`); on success it re-runs
  `bound()` + `primed()` and logs recovery; budget spent → give up and
  `is_alive` goes False.
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
  additions. Full suite 561+ pass, `ruff` + `mypy` clean.
- **Live (`scripts/b15_live_check.py`, autonomous):** a real `ActivityCollector`
  on the real compositor — 5 forced focus changes → **10** APP_SWITCH events,
  payload carries the B14 fields and no raw title, health stays green, shared
  connection tears down without segfault. Repeated ×2, no flakiness.
- **Daemon A/B:** restarted clean on the unchanged unit → 6 forced focus changes
  → **13** switches, a diagnosis signal, graph growth. Same with `XDG_SESSION_ID`
  forced in — confirming it is irrelevant.

## 6 · Exit criteria

1. Daemon on a normal working session records **dozens** of focus/tab switches
   per hour (not ~4), and `webapp:` nodes appear from browser tabs. ✅ (forced)
2. `ActivityCollector.health().ok` goes False within one poll interval of the
   Wayland connection dropping, and recovers when it comes back. ✅ (unit)
3. A forced transient (`read()` raises once) does not disable the source. ✅ (unit)
4. Full suite + `ruff` + `mypy` green. ✅
5. No segfault on `collector.stop()`. ✅

## 7 · Deferred

- The B9 soak gate's liveness check still keys on the old counters; a re-run on
  the target box during a real session window should show an order-of-magnitude
  more switches.
- A real browser-tab `webapp:` node was verified live earlier in the session
  (`webapp:gmail` / `crunchyroll` / `claude` / `jiohotstar` from a standalone
  collector); an autonomous browser-driven check is not included here because
  closing a specific Brave window from the CLI on Wayland is not clean.
