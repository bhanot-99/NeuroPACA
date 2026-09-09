# B16 · The Wayland focus sensor deafens itself — proxy lifetime, round two

**Branch:** `b16-wayland-subscription-stability` (off `main`, after #25)
**Found:** 2026-09-09, ~21 h into the B15-rebuilt 7-day soak.
**Status: ANALYSED — plan only. No code yet.**

---

## 1 · Symptom

The B15 liveness watchdog — meant to be a rare safety net — is doing **all** the
work. From `data/soak/` and `data/neuropaca.log`, soak session 2 (active daytime
use, 2026‑09‑09 21:47 IST → ongoing):

| signal | value |
|---|---|
| `assess` verdict | `FAIL 72 Wayland reconnects over 0.9d (watchdog)` |
| inter‑reconnect gap, active hours | **54 of ~65 gaps are exactly 180–181 s** — back‑to‑back watchdog fires |
| events dispatched between reconnects | **zero** (that is *why* each 180 s watchdog fires) |
| longer gaps (4790 s, 5696 s, 16450 s) | user idle — watchdog gated off by `activity_probe` |
| session 1 (overnight, idle) | 44 reconnects / 19 h ≈ 2.3/h — *looked* tolerable, was equally deaf, just unmeasured |
| session 2 (active) | ~14/h and climbing |
| `health()` | `window✓` throughout — reports healthy |
| focus data that *does* land | batch‑stamped at reconnect priming (e.g. 5 nodes all at `18:22:12.941002Z`) — snapshots, not a stream |

So B15's headline ("0/20 restarts deaf") was true **for the first snapshot after a
connect** and false for everything after. The sensor connects, primes once, then
goes silent within 180 s and stays silent until the watchdog kicks it — forever.
Effective behaviour: **focus is polled every 3 minutes**, and only while the user
is active enough to keep the watchdog armed.

Downstream cost: `FocusSessionPattern` / `DistractionPattern` / `IdlePattern` see a
3‑minute‑granular, gap‑ridden focus history; the whole point of the 7‑day soak
(prove sensing runs *clean* unattended for a week) cannot be met on this data.

## 2 · Root cause

**The compositor is not going quiet. The daemon is destroying its own toplevel
objects and then dropping the compositor's events as "zombie" traffic.**

### 2a · `ext_foreign_toplevel_handle_v1` proxies are never strong‑referenced — they die microseconds after creation

`WaylandWindowSource._on_toplevel(self, _list, handle)` (`window.py:167`):

```python
def _on_toplevel(self, _list, handle):
    key = id(handle)                          # (2b)
    self._toplevels[key] = _Toplevel()        # plain data object — NOT the proxy
    handle.dispatcher["app_id"] = lambda h, app_id: self._set(id(h), "app_id", app_id)
    handle.dispatcher["title"]  = lambda h, title:  self._set(id(h), "title",  title)
    handle.dispatcher["closed"] = lambda h:         self._drop(id(h))
    cosmic_handle = self._info_manager.get_cosmic_toplevel(handle)
    self._cosmic_handles[key] = cosmic_handle  # B15 strong ref — cosmic handle ONLY
    cosmic_handle.dispatcher["state"] = lambda _ch, state: self._set(key, "activated", ...)
```

`handle` is the `ext_foreign_toplevel_handle_v1` proxy the compositor just created.
It is stored **nowhere** — only `id(handle)` (an integer) is kept. pywayland does
not retain proxies either:

- `pywayland/protocol_core/interface.py:72` — `registry: WeakValueDictionary`
- `pywayland/client/display.py:96` — `_children: WeakSet`
- `pywayland/protocol_core/message.py:169` — an event's `new_id` arg builds a fresh
  `iface.proxy_class(proxy_ptr, display)` and hands it to our callback; the only
  retention is the two weak collections above.
- `pywayland/protocol_core/proxy.py:83` — `self._ptr = ffi.gc(ptr, lib.wl_proxy_destroy)`

So the moment `_on_toplevel` returns and `dispatcher_func`'s arg list is freed,
`handle`'s refcount hits 0 (no cycle → immediate) and cffi runs
**`wl_proxy_destroy`** on it. The client has now told libwayland "this object is
gone." Every subsequent `app_id` / `title` / `closed` event the compositor sends
for that toplevel arrives for a destroyed id and libwayland **discards it
silently** (zombie‑proxy handling — no error, not counted as dispatched).

`app_id` / `title` land at all today only because the compositor sends them in the
same read‑batch as the `toplevel` event, and *sometimes* the first one is
processed in the same `dispatch_pending` sweep before the GC finaliser runs. After
priming, nothing.

### 2b · The dicts are keyed by `id()` of an already‑dead object → address reuse evicts *live* cosmic handles

`key = id(handle)`. Once `handle` is finalised (2a), that address is freed and
CPython's allocator **reuses it aggressively** for the next same‑sized object. The
next `_on_toplevel` for a newly‑opened window frequently gets
`id(new_handle) == id(old_handle)`, so:

```python
self._cosmic_handles[key] = new_cosmic_handle   # evicts old_cosmic_handle
```

The evicted cosmic handle's refcount drops → GC → `wl_proxy_destroy` → **that
window's `state` (focus/activation) events are now also silently dropped.** B15's
strong‑ref dict is defeated by its own key.

Net effect over a session: every window the user opens has a chance of
destroying a *previously‑working* window's focus subscription. Session 1
(few new windows overnight) decays slowly; session 2 (many window opens) decays to
total silence between every watchdog tick. **This is the exact
active‑vs‑idle split in the soak numbers.**

### 2c · `closed` never fires → `_drop` never runs → the caches leak and never self‑heal

`_drop` is wired to `handle.dispatcher["closed"]`. `handle` is dead (2a), so
`closed` is never delivered, so `_drop` is never called. `self._toplevels` and
`self._cosmic_handles` grow for the life of the connection (bounded only by the
`id()` collisions in 2b overwriting entries). Minor contributor to the
`+22 MiB/day` warm RSS slope the soak also flagged; more importantly it means the
window source has **no way to notice or recover** a dead subscription on its own —
only a full connection teardown clears the dicts (`bound()` / `lost()`).

### 2d · On `zcosmic_toplevel_info_v1` v3, `app_id`/`title` are *only* on the foreign handle

The vendored XML (`_protocols/xml/cosmic-toplevel-info-unstable-v1.xml`) marks
`app_id`, `title`, `closed`, `done` on `zcosmic_toplevel_handle_v1` as
`deprecated-since="2"`. The daemon binds `_TOPLEVEL_INFO_MAX_VERSION = 3`. So on
the wire, identity (`app_id`/`title`) comes **only** from
`ext_foreign_toplevel_handle_v1` — precisely the proxy 2a throws away. `state`
(activation) is the one thing correctly homed on the (2b‑fragile) cosmic handle.
Any fix has to keep the foreign handle alive for the window's whole life, or drop
to `_TOPLEVEL_INFO_MAX_VERSION = 1` and take identity off the cosmic handle (worse
— re‑introduces the deprecated path).

### 2e · Monitoring blind spot — `health()` ignores the one field that would catch this

`WaylandConnection.seconds_since_event` exists (`wayland_conn.py:105`) and is the
literal "deafness signature" per its own docstring. `ActivityCollector.health()`
never reads it — `window✓` is `is_alive` = *pump task running and `_connected`*,
both true for a fully deaf connection. B15 redefined `window✓` from "started" to
"pump alive" but stopped one step short of "events actually arriving." The soak's
`assess` only caught this because it independently counts `reconnects`.

### 2f · Why the poll‑pump can't paper over it

Even with a perfect pump, a destroyed proxy receives nothing. The pump is not
implicated in 2a–2d. It *is* worth hardening while we are here (see §3d) but it is
not the bug.

---

## 3 · Fix

Ordered by leverage. §3a is expected to resolve the symptom on its own; the rest
harden and make the failure impossible to miss next time.

### 3a · Strong‑ref **both** proxies, keyed by a stable counter, for the toplevel's whole life

`window.py`:

```python
self._next_key = 0
self._foreign: dict[int, Any] = {}   # NEW — ext_foreign_toplevel_handle_v1 proxies
self._cosmic:  dict[int, Any] = {}   # was _cosmic_handles
self._tops:    dict[int, _Toplevel] = {}
self._key_by_ptr: dict[Any, int] = {}   # id(proxy) -> key, for dispatcher lookups only while alive
```

`_on_toplevel`:

```python
def _on_toplevel(self, _list, handle):
    key = self._next_key
    self._next_key += 1
    self._foreign[key] = handle            # THE missing strong ref
    self._tops[key] = _Toplevel()
    handle.dispatcher["app_id"] = lambda _h, app_id, k=key: self._set(k, "app_id", app_id)
    handle.dispatcher["title"]  = lambda _h, title,  k=key: self._set(k, "title",  title)
    handle.dispatcher["closed"] = lambda _h,         k=key: self._drop(k)
    cosmic = self._info_manager.get_cosmic_toplevel(handle)
    self._cosmic[key] = cosmic
    cosmic.dispatcher["state"] = lambda _c, state, k=key: self._set(
        k, "activated", _STATE_ACTIVATED in list(state)
    )
```

- Key is a monotonically increasing `int` bound into each lambda as a default arg
  — never `id()`, never reused, no address‑collision hazard.
- `_drop(key)` pops all three dicts; now actually reachable because the foreign
  handle lives to deliver `closed`.
- `bound()` / `lost()` reset all four structures (unchanged intent).
- `_recompute_focus` walks `self._tops.values()` (unchanged).
- On `lost()` / teardown, dropping the dicts releases the proxies → pywayland
  destroys them cleanly, in bulk, off the fd.

`wl_proxy_get_id` is not exposed by pywayland 0.4.19, so a wrapped C id is not an
option — the counter is the right key.

### 3b · Also bind `ext_foreign_toplevel_list_v1.finished`

If the compositor retires the list global (screen lock, compositor reload), we
get `finished`; treat it as `lost()` for the window half so the connection
re‑primes rather than silently believing its cache. Low cost, closes a real
COSMIC edge case.

### 3c · `health()` reads `seconds_since_event`

`ActivityCollector.health()`:

- `window✓` requires `is_alive` **and** (`seconds_since_event < STALE` **or**
  user is idle). Otherwise `window~` (degraded) and `health().ok = False`.
- Surface `seconds_since_event` and `reconnects` in `detail` unconditionally on
  the real path (already partly there).
- New `EventType.SYSTEM_ERROR` (severity `sensor-degraded`, rate‑limited to once
  per 10 min) when the window source is `is_alive` but silent while active — so a
  regression shows up in the daemon log and the soak sample, not just in a
  reconnect counter.

### 3d · Pump / watchdog hardening (opportunistic, not load‑bearing)

- Keep `_STALE_RECONNECT_SECONDS = 180` as the safety net but **count every
  watchdog fire as a defect signal**, not routine self‑healing. `soak_state.py
  assess` already fails > ~1/day; leave that threshold, it is correct.
- On reconnect, log at `WARNING` with `seconds_since_event` and the live
  `len(self._foreign)` so a soak log shows whether caches are growing.
- `_teardown` during interpreter/loop shutdown: guard the `display.read()` /
  `dispatch()` path so the shutdown‑race `RuntimeError("Failed to read events")`
  (seen once at the session‑1 SIGTERM) is swallowed at `DEBUG`, not `exception`.

### 3e · Escalation path if 3a is *not* enough — the dedicated Wayland thread (B15 §7 deferred)

Only if the live check in §5 still shows silence: move the shared `Display` onto a
dedicated daemon thread running the canonical
`while running: wl_display_dispatch(block=True)` loop, marshalling each decoded
event to the asyncio bus via `loop.call_soon_threadsafe`. This removes the
poll‑pump, the `select` race, and the `add_reader` fragility
(flacjacket/pywayland#16) entirely. `rules.md §3` ("no threads") gets one
documented exception — the same argument the inference lock already makes. Held in
reserve because 2a fully explains the symptom and a thread does not fix a
GC'd‑proxy bug.

---

## 4 · Files (expected)

```
EDIT  src/neuropaca/sensing/activity/window.py          §3a strong-ref both proxies, int keys, reachable _drop; §3b finished
EDIT  src/neuropaca/sensing/activity/wayland_conn.py    §3d reconnect logging, shutdown-race guard
EDIT  src/neuropaca/sensing/activity/collector.py       §3c health() reads seconds_since_event; sensor-degraded event
EDIT  tests/test_wayland_handlers.py                    proxy retained for toplevel life; _drop reachable via closed; int-key stability; id()-reuse regression
EDIT  tests/test_wayland_conn.py                        shutdown-race guard; reconnect log fields
EDIT  tests/test_activity.py                            health() degraded-while-silent; sensor-degraded event
NEW   spikes/b16_toplevel_lifetime/observe.py           standalone lifetime probe (§5)
NEW   B16_TEST_REPORT.md                                results
EDIT  phases.md                                         B16 entry
EDIT  RESEARCH_DOSSIER.md                               §11.10 / §15.11 / §16 — B16, v6
```

No systemd unit change. No `neuropaca.toml` change.

## 5 · Verification

### 5a · Unit (deterministic, fake pywayland)

1. **`_on_toplevel` retains the foreign proxy** — after the callback returns and a
   `gc.collect()`, the object handed to `_on_toplevel` is still referenced by
   `src._foreign` and its `dispatcher` is intact.
2. **`id()`‑reuse regression** — drive 3 `_on_toplevel` calls where the fake
   handles deliberately share an `id()` (reuse a freed slot); assert all 3
   cosmic handles survive and all 3 windows stay focus‑visible. This test fails
   on the current `id(handle)` key and passes on the int key.
3. **`_drop` is reachable** — `closed` on a live foreign handle pops all three
   dicts; caches return to empty after every window closes.
4. **`health()` degrades while silent** — `seconds_since_event > STALE` + active
   probe ⇒ `window~`, `health().ok is False`, one `sensor-degraded` event; idle
   probe ⇒ still `window✓`.
5. Full suite + `ruff check .` (bare, per CI) + `mypy` green.

### 5b · Standalone lifetime probe (`spikes/b16_toplevel_lifetime/observe.py`)

A ~90 s script, no `src/` imports, that connects like the daemon (both protocols,
one `Display`), installs `gc.callbacks` to log every pywayland proxy finalisation,
and logs every dispatched event as `interface.event`. Two modes:

- `--leak` (reproduce): no strong ref on the foreign handle → expect a finalise
  log within milliseconds of each `toplevel`, then `dispatch()==0` across manual
  window switches, then the operator sees the same 180 s silence.
- `--hold` (candidate fix): strong‑ref both → expect a continuous
  `ext_foreign_toplevel_handle_v1.app_id` / `zcosmic_toplevel_handle_v1.state`
  stream as the operator switches windows, zero unexpected finalisations.

Operator runs each mode once, switching windows + opening/closing a scratch
window throughout. This is the load‑bearing evidence that §3a is the fix.

### 5c · Daemon A/B on the target box

`stop` → confirm PID gone → `start` on the B16 build → 20 min of normal use →
`neuropaca overview` / soak sample. Pass = **dozens of switches, < 1 watchdog
reconnect** in the window (vs ~7 reconnects / 20 min today).

### 5d · Soak

Restart the 7‑day soak from the B16 build (the current run is already void for
focus data — same call B15 made). `assess` after 48 h of accrued active use must
show `reconnects` back in the "quiet" band (< ~1/day) with focus/switch counts an
order of magnitude up.

## 6 · Exit criteria

1. Standalone probe `--hold`: uninterrupted focus event stream across 10+ manual
   switches, 0 stray proxy finalisations. `--leak` reproduces the silence.
2. Daemon, 20 min real use: **< 1** Wayland watchdog reconnect; focus switches
   tracked in real time (timestamps spread, not batch‑stamped).
3. `ActivityCollector.health().ok` goes False within one sample of the window
   source going silent while the user is active, and a `sensor-degraded`
   `SYSTEM_ERROR` is on the bus.
4. Caches (`_foreign` / `_cosmic` / `_tops`) return to empty after all tracked
   windows close — no unbounded growth over an 8 h run.
5. Full suite + `ruff check .` + `mypy` green. No segfault on `collector.stop()`.
6. 48 h soak accrual: watchdog reconnects < ~1/day, switch count ≥ 10× the
   pre‑B16 rate.

## 7 · Open questions / risks

- **Does §3a alone silence the watchdog, or is there a second cause?** The soak
  data is consistent with a single cause (2a/2b), but B15 also thought it was
  done. §5b `--leak`/`--hold` settles this before any daemon change.
- **`get_cosmic_toplevel` on a long‑lived foreign handle** — confirm the
  compositor keeps streaming `state` for the handle's whole life and does not
  expect periodic re‑request. XML says `state` is "emitted once on creation …
  and again whenever the state changes" — no re‑request implied.
- **Thread escalation (§3e)** touches load‑bearing code and breaks a `rules.md`
  invariant; only pull it if §5b `--hold` still shows silence.
- **`RawMetricsRecorder` / CSV** already 845 KB after 21 h — unrelated to B16 but
  worth a glance during the soak restart (it is meant to be off by default;
  `neuropaca.b13.toml` turns it on for the soak).
