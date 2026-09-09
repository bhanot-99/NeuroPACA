# B17 · One app, one node — canonical identity, readable names, structured graph view

**Branch:** `b17-app-identity-canonicalization` (off `main`, after B16)
**Found:** 2026-09-10, graph review.
**Status: IN PROGRESS.** Re-planned 2026-09-10 to the operator's brief:
1. one node per real app (canonical identity),
2. **a naming layer so node labels read in plain English**,
3. **click a node → a side panel with its details** (native graph window),
4. **the 11 master nodes pinned in a fixed, even structure**,
5. execute · full test · update docs · manually clean the live graph · merge · push.

---

## 1 · Symptom

`data/graph.json` at ~22 h of soak had 24 `app:`/`webapp:` nodes for ~12 real
apps. The split is systematic:

| Real app | focus node (`ram_mb 0`, high `access_count`, all the edges) | census node (`ram_mb > 0`, ~no edges) |
|---|---|---|
| Brave | `app:brave-browser` — ac 109, 4 `webapp:` children, 2 insights | `app:brave` — 3811 MiB |
| Claude | `app:com.anthropic.Claude` — ac 0 | `app:claude` 342 MiB · `app:claude-desktop` 1192 MiB |
| Cosmic Files | `app:com.system76.CosmicFiles` — ac 10 | `app:cosmic-files` 588 MiB |
| Obsidian | `app:md.obsidian.Obsidian` — ac 3 | `app:obsidian` 890 MiB |

Behavioural truth on one node, resource truth on another. Also `app:MainThread`
(a thread name), `app:neuropacad` / `app:python3` / `app:chrome-devtools-mcp`
(the daemon and its tooling in its own graph), and the node labels are raw ids
(`domain:mental_models`, `webapp:google-gemini`) that do not read as English.

## 2 · Root cause

`upsert_node` de-dups perfectly by **exact `node_id`**. Two sources format the id
from two different names:

| Source | Field | Value |
|---|---|---|
| focus sensor → `APP_SWITCH` → `correlator._classify_into_graph`, focus-pattern `NodeSpec`s | Wayland `app_id` (reverse-DNS) | `com.system76.CosmicFiles` |
| B13 `ProcessCollector` census → `_heavy_app_specs`, `_idle_app_spec`, census patterns | Unix process name (`psutil` `proc.info["name"]`) | `cosmic-files` |

`patterns.py:98-101` literally says *"where they differ … a round-2 name map
merges them (B13 §7)"* — never built. `AppMap` maps `app_id → domain` only, no
identity notion. `psutil.name()` is `/proc/<pid>/comm`, which a runtime can set to
a thread name (`MainThread`) or which names an Electron helper separately from its
main binary (`claude` vs `claude-desktop`). `GraphMemory._merge_nodes_unsafe`
exists and is correct; nothing tells it these are the same thing.

## 3 · Design decisions (made for autonomous execution — recorded here)

- **No schema bump.** v4 and a de-duplicated v4 file are structurally identical, so
  a schema gate buys nothing and a `_migrate` step is new risk. Instead: canonical
  ids are written *going forward*, and a **single idempotent
  `canonicalise_app_nodes(resolver)` pass** runs once at orchestrator start after
  `load()` — covered by the same quarantine machinery as any load-time failure.
- **`display_name` is NOT a stored field.** It is derived, presentation-only, in
  the graph window from the (now canonical) id + `label`. Keeps `src/` changes
  small and off the serialisation path. If a stored name is ever wanted it is its
  own phase.
- **Canonical form = the short process-style slug** (`brave`, not
  `brave-browser`). The census can only ever produce that; the focus sensor is
  taught the alias. Ships a solid default table; the operator tunes it like
  `webapp_map` / `app_map`.
- **Graph window** changes are pure presentation (no `src/` coupling, per its
  "imports nothing from `src/`" contract).

## 4 · What gets built

### 4a · `src/neuropaca/diagnosis/app_identity.py` (new)

`AppIdentity` — pure, immutable, built once at
`SignalCorrelator.initialize()` from `data/app_identity.default.toml`
(`Config.app_identity_path`).

```
resolve(raw: str) -> str          # canonical slug for the node id
is_non_app(raw: str) -> bool      # thread names, shells — never a real activity
pretty(canonical: str) -> str     # "Cosmic Files" — used by the graph window via `label`
```

Resolution: exact `[alias]` hit → minimal normalise (lowercase; strip a trailing
`-browser`/`-desktop`/`-bin`/`-gtk`/`-wayland`; `_`/`.`/space → `-`; trim) →
normalised passthrough. **No reverse-DNS flattening** — `com.system76.CosmicTerm`
and `cosmic-comp` do not converge, so those go in `[alias]`. `[non_app]` is an
exact-match set.

The default TOML ships the common desktop set (COSMIC apps, Brave/Chrome/Firefox,
VS Code, Zed, Obsidian, Claude, Slack, Discord, Spotify, …) and a `[non_app]`
list (`MainThread`, `sh`, `bash`, `zsh`, `Thread-*` via a small regex handled in
code).

### 4b · Wire canonical ids in (`correlator.py`, `patterns.py`)

`SignalCorrelator` builds `self._identity`; `on_app_switch` resolves `app_id`
once and passes the canonical id to `_classify_into_graph` /
`_classify_webapp_into_graph` (browser node id in the `webapp PART_OF app` edge).
Every `app:` `NodeSpec` in `patterns.py` (`_idle_app_spec`, `DistractionPattern`,
`FocusSessionPattern`, `_heavy_app_specs`, `HeavyAppStartedPattern`,
`MemoryPressurePattern`) resolves its raw name first. `label` on the node becomes
`identity.pretty(canonical)` so the graph window and `consolidate()` both benefit.
`AppMap.classify` keeps keying on the raw `app_id` — routing and identity are
independent.

### 4c · `GraphMemory.canonicalise_app_nodes(resolver, non_app)` (new, public)

One lock-cycle-per-mutation pass, same shape as `consolidate()`:
- group every `app:` / `webapp:` node by `resolver(bare id)`; `_merge_nodes_unsafe`
  each group into the survivor with the most edges (oldest `created_at` on a tie),
  id `app:<canonical>` / `webapp:<canonical>`.
- delete any node whose bare id `is_non_app` **and** has `access_count == 0` and no
  edge that came from a focus event (`APP_SWITCH`-origin) — a real focused app is
  never dropped.
- returns `(merged, dropped)`. Idempotent: a second run is a no-op.
Orchestrator calls it once after `load()` (skips on a reseeded fresh graph).

### 4d · Census hygiene (`sensing/collectors/process.py`, `config.py`)

- drop a row whose `name` matches `_THREAD_NAME_RE` (`^MainThread$`,
  `^Thread-\d+`, `^asyncio_\d+`, `^ThreadPoolExecutor`, …) — names only, no
  `cmdline` (rules §6).
- `Config.process_exclude_names` default now populated (B13 §7(8), soak-informed):
  `neuropacad, python3, node, chrome-devtools-mcp, cosmic-comp, Xwayland`. Still a
  list the operator can empty.

### 4e · `scripts/neuropaca_graph_window.py` — readable names, pinned hubs, detail panel

- **`_pretty(node_id, node)`** — `YOU` → "You"; `domain:mental_models` → "Mental
  Models"; `app:brave` → node `label` if set else "Brave"; `webapp:google-gemini`
  → "Google Gemini"; `insight:…` → first line of `label` (trimmed) or "Insight";
  `ephemeral:summary:…` → "Summary". Used everywhere a label is drawn.
- **Pinned master nodes** — `YOU` at world origin; the 10 `domain:` hubs on a
  circle of radius `HUB_RING` at `2πi/10` in `DOMAIN_ORDER` (matches
  `graph_memory.DOMAIN_SLUGS`). All 11 added to `layout.pinned` at `sync()` and
  re-pinned every step; the FR loop skips them; a hub is not draggable. Members
  still cluster to their hub (existing `CLUSTER` force) so the picture is a clean
  ring of labelled domains with their apps around each.
- **Detail panel** — `Gtk.Box` docked right of the drawing area (fixed ~300 px,
  hidden until first selection). Left-click a node → select + populate:
  pretty name (title), raw id (mono), type, relevance, access count, priority,
  first/last seen + RAM/CPU when present, then **Connected to:** a list of
  neighbours (`relation` + pretty name), click-through to select that neighbour.
  Left-click empty space → deselect + hide. `Esc` also hides.

### 4f · `scripts/graph_cleanup.py` (new) — the one-time manual clean, reusable

Standalone (stdlib only). Loads `data/graph.json`, applies the **alias-table**
fold (reads the same `[alias]` / `[non_app]` from the TOML — no `src` import, the
~10-line normaliser is duplicated), drops non-hub 0-degree nodes, drops
`[non_app]` nodes with no focus history, writes back atomically (`.tmp` +
`os.replace`), prints a before/after diff. `--dry-run` default; `--apply` to write.

---

## 5 · Files

```
NEW   src/neuropaca/diagnosis/app_identity.py
NEW   data/app_identity.default.toml
EDIT  src/neuropaca/core/config.py                  app_identity_path; process_exclude_names default
EDIT  src/neuropaca/diagnosis/correlator.py         build + use AppIdentity
EDIT  src/neuropaca/diagnosis/patterns.py           resolve every app: NodeSpec
EDIT  src/neuropaca/core/graph_memory.py            canonicalise_app_nodes()
EDIT  src/neuropaca/orchestration/orchestrator.py   call it once after load()
EDIT  src/neuropaca/orchestration/modules.py        pass identity into SignalCorrelator (or build in orch)
EDIT  src/neuropaca/sensing/collectors/process.py   thread-name guard
NEW   scripts/graph_cleanup.py
EDIT  scripts/neuropaca_graph_window.py             _pretty, pinned hubs, detail panel
NEW   tests/test_app_identity.py
NEW   tests/test_graph_window.py                    _pretty + hub-pin math (pure, no gi)
NEW   tests/test_graph_cleanup.py
EDIT  tests/test_graph_memory.py                    canonicalise_app_nodes: fold, drop, idempotent, hub-safe
EDIT  tests/test_diagnosis_activity_patterns.py     canonical ids in every spec
EDIT  tests/test_process_collector.py               thread-name rows dropped
EDIT  tests/test_activity.py / test_orchestrator*   wiring
EDIT  phases.md · RESEARCH_DOSSIER.md (v7)
NEW   B17_TEST_REPORT.md
```

No systemd unit change. No new runtime dependency (`tomllib` is stdlib).

## 6 · Verification

1. `AppIdentity`: alias hits; normaliser (`Brave-Browser`→`brave`,
   `foot`→`foot`); passthrough of unknowns; `is_non_app`; `pretty`.
2. `canonicalise_app_nodes` on a hand-built graph: `app:brave` + `app:brave-browser`
   (+ webapp child on one, `ram_mb` on the other) → one `app:brave`, ac summed,
   edge rewired, `ram_mb` kept; `app:MainThread` ac 0 no-focus → gone; `app:zed`
   ac 5 in `[non_app]` → **kept**; hubs never touched; second run = `(0, 0)`.
3. `canonicalise_app_nodes` on a **copy of the real soak graph**: 24 → ~12
   `app:`/`webapp:` nodes, every `webapp:` still reaches its browser, no dangling
   edges, merged Brave carries `ram_mb ≈ 3811` **and** `access_count ≈ 114` **and**
   all 4 webapp children.
4. patterns: one focus + one census snapshot for the same app in one ingest →
   exactly one `app:<canonical>` node with both the domain edge and the census
   attributes.
5. `ProcessCollector`: mocked `psutil` row `name="MainThread"` dropped;
   `name="zed"` kept; the exclude default applied.
6. graph window (pure helpers only, no `gi`): `_pretty` table; the 11 hub
   positions are the deterministic ring; `layout.pinned` ⊇ the 11 after `sync`.
7. `graph_cleanup.py --dry-run` on the soak-graph copy prints the right diff;
   `--apply` writes a valid graph the daemon then loads clean.
8. Full suite + `ruff check .` + `mypy` green. Graph load + the canonical pass
   inside B1's 2 s / 10k-node budget.
9. **Live**: daemon restart on B17 → graph auto-canonicalises once → 20 min use →
   `neuropaca overview` shows one node per touched app, no `MainThread`/tooling
   nodes, no new dup. Graph window: 10 evenly-spaced labelled domain hubs around
   "You", click any node → panel with correct details + neighbours.

## 7 · Manual graph clean (operator step, done in this session)

`systemctl --user stop neuropacad` → `scripts/graph_cleanup.py --apply` → eyeball
the diff → `systemctl --user start neuropacad` (the orchestrator's canonical pass
then confirms it is a no-op). `data/graph.json` is gitignored — not committed.

## 8 · Risks / open

- **Default table completeness** — a COSMIC/GNOME box the table does not know
  still splits until the operator adds an alias; the normaliser catches the
  `-browser`/`-desktop` half. Documented like `webapp_map`.
- **Over-merge** — two real apps normalising equal. `[alias]` is exact-match and
  wins first; the normaliser is deliberately conservative.
- **Hub pinning vs. existing FR** — the FR loop currently pushes hubs apart with a
  `2.6×` term; with hubs pinned that term is dead code for hubs (kept for the
  no-data fallback). Members re-settle around the fixed ring — a few seconds after
  first load, same as today.
- **Hebbian weights all `0.0`** in the soak graph — a real bug found in the same
  review, **out of B17 scope**, flagged in the dossier so it is not lost.
