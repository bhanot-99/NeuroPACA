# B15 · Test report

**Branch:** `fix/wayland-poll-pump`
**Run:** 2026-09-08, target box (Pop!_OS / COSMIC `cosmic-comp`, Wayland)
**Result:** all green — 561 pytest pass, `ruff` + `mypy` clean, live checks pass, daemon verified.

---

## 1 · What is on this branch

`fix/wayland-poll-pump` was cut from `feat/brave-webapp-attribution` (B14, commit
`cbc2493`). B15 is the uncommitted work on top:

| # | Subpart | Files |
|---|---|---|
| **S1** | `WaylandConnection` — one shared `Display`, one poll-pump, bounded reconnect, `is_alive` | `sensing/activity/wayland_conn.py` (new) |
| **S2** | `WaylandWindowSource` → a `WaylandProtocolHandler` on the shared connection (toplevel-info) | `sensing/activity/window.py` |
| **S3** | `WaylandIdleSource` → a `WaylandProtocolHandler` on the shared connection (idle-notify) | `sensing/activity/wayland_idle.py` |
| **S4** | `ActivityCollector` — builds the one shared connection, wires both handlers; `health()` reads `is_alive` live | `sensing/activity/collector.py` |
| **S5** | `IdleSource` / `WindowSource` protocols + fakes gain `is_alive` | `sensing/activity/idle.py`, `window.py` |
| **S6** | Docs | `B15_PLAN.md` (new), `phases.md` |

No systemd unit change (see §5, the `XDG_SESSION_ID` false lead).

---

## 2 · Test programs built, per subpart

### 2.1 `tests/test_wayland_conn.py` — S1, the shared connection (13 tests, no compositor)

Two layers, neither needs Wayland:

- **poll-pump / resilience** — a fake `display` double + a real `os.pipe()` fd:
  - `test_dispatch_runs_every_tick_even_when_fd_not_readable` — `dispatch(block=False)`
    fires on **every** tick, `read()` **zero** times (the unconditional drain is the fix)
  - `test_read_runs_only_when_fd_is_readable` — write to the pipe → `read()` fires
  - `test_transient_error_reconnects_recovers_and_re_primes` — one `RuntimeError`
    from `dispatch()` → old display disconnected, `handler.lost()` called, a new
    display created, `bound()` + `primed()` re-run, pump resumes
  - `test_permanent_failure_gives_up_and_reports_dead` — `_connect` always raises →
    after the backoff budget the task exits, `is_alive` False
  - `test_cancellation_propagates` — `task.cancel()` → `CancelledError` re-raised
  - `test_is_alive_state_machine` — False before start / True connected+running /
    False when `_connected` drops / False when the task is done
- **connect flow** — a fake `pywayland.client` module injected into `sys.modules`:
  - `test_connect_binds_globals_and_calls_bound_then_primed_in_order` — 2 roundtrips,
    `bound` strictly before `primed`, `_connected` set, fd captured
  - `test_connect_raises_collectorerror_without_wayland_display`
  - `test_connect_wraps_a_handler_collectorerror_and_disconnects` — a handler that
    raises `CollectorError` in `bound()` → display disconnected, error propagates
  - `test_connect_wraps_a_primed_failure_as_collectorerror` — `primed()` blows up →
    wrapped as `CollectorError`, display torn down
  - `test_multiple_handlers_all_get_bound_and_primed`
  - `test_teardown_calls_lost_on_every_handler_and_is_idempotent`
  - `test_teardown_survives_a_handler_that_raises_in_lost`

### 2.2 `tests/test_wayland_handlers.py` — S2 + S3, the two handlers (17 tests, no compositor)

Drives the `WaylandProtocolHandler` hooks and event callbacks directly with fake
wayland proxies. `wants()` (the one method that imports pywayland) is
`importorskip`-guarded.

- **window:** `wants` returns the 2 toplevel interfaces · `bound` stores the info
  manager + wires the `toplevel` dispatcher · `bound` **raises** when a global is
  missing · fires the callback on `app_id` change · **no** fire when nothing
  changed · **title-sensitivity** — a title tick fires only for a configured
  browser app_id, never for a terminal · `closed` drops the toplevel and
  recomputes · `lost()` clears every field · `is_alive` delegates to the
  connection · shared-mode `start()` does **not** call `conn.start()` (the
  collector owns that)
- **idle:** `wants` returns notifier + seat · `bound` **raises** on a missing
  global · `primed` calls `get_idle_notification(threshold_ms, seat)` and wires
  `idled`/`resumed` · transitions fire the callback · `lost()` **fails safe to
  ACTIVE** and clears state · `is_alive` delegates
- **wiring:** two handlers register on one connection

### 2.3 `tests/test_activity.py` — S4, the collector (13 tests; 4 new for B15)

- `test_health_turns_unhealthy_when_a_started_source_goes_deaf` — a source that
  started then died → `health().ok` False, detail shows `window✗ idle✓`
- `test_real_path_builds_one_shared_connection_with_both_handlers` — exactly **one**
  `WaylandConnection`, **2** handlers on it, `conn.start()` called once, `health`
  green; `collector.stop()` → `conn.stop()` once
- `test_real_path_wayland_unavailable_disables_both_halves` — `conn.start()` raises
  `CollectorError` → both `✗`, **one** `SYSTEM_ERROR` labelled
  `sensing.activity.wayland`, module stays tolerated (`health().ok` True)
- `test_real_path_connection_dies_later_drags_health_unhealthy`

The pre-existing degraded-path / edge-triggered / APP_SWITCH / B14 webapp tests
in this file all still pass unchanged.

### 2.4 `scripts/b15_live_check.py` — S1–S4 end to end, on the real compositor (autonomous)

The B15 bug was live-only, so this is the load-bearing check. It builds a **real**
`ActivityCollector` (real `ext-foreign-toplevel` + `ext-idle-notify` on the real
`cosmic-comp`) and pops throwaway `zenity` dialogs to force real focus changes —
no human. Six assertions:

1. collector starts, both Wayland halves `is_alive`, `health().ok`
2. exactly **one** shared `WaylandConnection`, 2 handlers, one `Display`
3. 5 forced focus changes → **≥ 5** `APP_SWITCH` events
4. `APP_SWITCH` payload carries the B14 fields and **no** `title`
5. collector still healthy after the focus storm
6. `collector.stop()` disconnects the shared connection with **no segfault**

---

## 3 · Unit test results

```
$ .venv/bin/python -m pytest -q
561 passed, 3 skipped, 23 deselected

  tests/test_wayland_conn.py          13 pass
  tests/test_wayland_handlers.py      17 pass
  tests/test_activity.py              13 pass   (4 new for B15)
  ...all other suites unchanged and green (B14: test_webapp_* etc.)

$ .venv/bin/ruff check .          All checks passed!
$ .venv/bin/mypy src/neuropaca    Success: no issues found in 72 source files
```

The 3 skips are pre-existing and environmental (`NEUROPACA_NO_NETWORK`,
`llama-cpp-python` present). CI does not install `pywayland`; the new tests are
import-safe without it (`wants()` calls are `importorskip`-guarded; everything
else uses fakes).

---

## 4 · Live test results

### 4.1 `scripts/b15_live_check.py` — 2 consecutive runs

| Run | Result | Forced focus changes | APP_SWITCH gained |
|---|---|---|---|
| 1 | **ALL PASSED** (6/6) | 5 | 10 |
| 2 | **ALL PASSED** (6/6) | 5 | 10 |

Sample payload observed: `{'app_id': 'zenity', 'webapp': None, 'webapp_domain':
None, 'previous_app_id': 'com.system76.CosmicTerm', 'previous_webapp': None}` —
B14 shape, no raw title. Clean teardown, no SIGSEGV.

### 4.2 Root-cause confirmation — two connections vs one (run directly, in-session)

| collector | 25 forced focus/title changes | teardown |
|---|---|---|
| real window + `FakeIdleSource` (**one** `Display`) | **~7** events | clean |
| **two** real `Display` connections | **1** event | **SIGSEGV (exit 139)** |

### 4.3 Daemon — clean A/B on the unchanged unit

`systemctl --user stop` → confirm MainPID gone → `start` → confirm new
PID/timestamp, then 5–6 `zenity` focus steals:

| daemon | `XDG_SESSION_ID` in env | switches (baseline → after) | diagnosis |
|---|---|---|---|
| ablation: plain `ExecStart`, shared-conn code | absent | 1 → **11** | — |
| with `XDG_SESSION_ID=4` forced in | present | 1 → **11** | 1 signal |
| **final: clean unit, shared-conn code** | absent | 1 → **13** | 1 signal, +2 graph nodes, drive tracking |

`XDG_SESSION_ID` makes **no difference** — the fix is the shared connection.

---

## 5 · The `XDG_SESSION_ID` false lead (recorded for the paper)

~2 h of the investigation chased a phantom. A `systemd --user` service's
environment lacks `XDG_SESSION_ID`, and early daemon tests with it hardcoded
appeared to restore the focus stream. They did not: **`systemctl --user restart`
was silently not taking effect** — a daemon started by `systemctl start` at 17:31
kept running the old two-connection code while every drop-in edit and "restart"
changed nothing about the live process (`Active: active (running) since 17:31`,
`MainPID` unchanged). The tell was `ExecMainStartTimestamp` never advancing.
Forcing a real cycle (`stop` → `pgrep` confirms gone → `start` → new PID) and
re-testing showed the shared-connection code works with or without the var.
**Lesson:** when a systemd restart "does nothing", check `MainPID` /
`ExecMainStartTimestamp` before attributing behaviour to config.

---

## 6 · Files changed

```
 src/neuropaca/sensing/activity/wayland_conn.py  | 191 ++++++++++++  (new)
 src/neuropaca/sensing/activity/window.py        | 155 +++++--------
 src/neuropaca/sensing/activity/wayland_idle.py  | 132 +++++-------
 src/neuropaca/sensing/activity/collector.py     |  58 +++++--
 src/neuropaca/sensing/activity/idle.py          |   7 +
 tests/test_wayland_conn.py                      | 320 ++++++++  (new)
 tests/test_wayland_handlers.py                  | 250 ++++++++  (new)
 tests/test_activity.py                          | 132 ++++++
 scripts/b15_live_check.py                       | 210 ++++++++  (new)
 B15_PLAN.md                                     | (new)
 phases.md                                       |  33 ++
```

Daemon left running on the final clean unit (`neuropacad.service`, no wrapper),
config `neuropaca.b13.toml`, verified `window✓` and switch-count climbing.

---

## 7 · Open / not done

- **B9 soak gate re-run** — its liveness check keys on the old counters; a re-run
  during a real interactive session window should now show dozens of switches, not
  ~4. Not run here (needs a real work session, not forced dialogs).
- **Autonomous browser-tab `webapp:` check** — a real `webapp:gmail` /
  `crunchyroll` / `claude` node was verified live earlier in the session from a
  standalone collector; not baked into `b15_live_check.py` because closing a
  specific Brave window from the CLI on Wayland is not clean.
- **Commit + PR** — B15 is committed on `fix/wayland-poll-pump`; PR not opened
  (waiting on you). B14 (`feat/brave-webapp-attribution`) is its parent and also
  unmerged.
