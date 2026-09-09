# B17 · One app, one node — canonical activity identity

**Branch:** `b17-app-identity-canonicalization` (off `main`, after B16)
**Found:** 2026-09-10, reviewing the soak graph — the operator asked why the same
app has two or three nodes.
**Status: ANALYSED — plan only. No code yet.**

---

## 1 · Symptom

The graph builds **two or three `app:` nodes for one real application**. From
`data/graph.json` at ~22 h of soak (24 `app:`/`webapp:` nodes, should be ~12):

| Real app | Focus-sensor node (`ram_mb = 0`, high `access_count`) | Census node (`ram_mb > 0`, low `access_count`) | also |
|---|---|---|---|
| Brave | `app:brave-browser` — ac **109**, all 4 `webapp:` children, both `insight:` edges | `app:brave` — ac 5, **3811 MiB** | |
| Claude desktop | `app:com.anthropic.Claude` — ac 0 | `app:claude` — ac 2, 342 MiB | `app:claude-desktop` — ac 0, 1192 MiB |
| Cosmic Files | `app:com.system76.CosmicFiles` — ac 10, `PART_OF tools`, idle edges | `app:cosmic-files` — ac 1, 588 MiB | |
| Obsidian | `app:md.obsidian.Obsidian` — ac 3, `PART_OF research` | `app:obsidian` — ac 1, 890 MiB | |
| VS Code | `app:code` — ac 2 *(the two names agree — one node)* | — | |

The split is not random: **the behavioural truth** (which webapps, which domain,
which insights, how often focused) lives on the focus node; **the resource truth**
(3.8 GB, first-seen, CPU) lives on the census node. They should be the same node.

Also visible, and in scope because they are the same class of bug:

- **`app:MainThread`** — `ram_mb 219`, ac 0. `psutil` returned a *thread* name,
  not a process name. Pure junk in the graph.
- **`app:neuropacad` / `app:python3` / `app:chrome-devtools-mcp` / `app:cosmic-comp`**
  — the daemon's own footprint and its tooling. B13 §7(8) kept the census
  unfiltered "so the operator can see it first". It has been seen; it is noise.
- Every edge weight in the graph is still `0.0` — Hebbian reinforcement is not
  accumulating. **Out of scope for B17** (noted for a follow-up) but it means the
  duplicate-dilution cost is currently masked; it will bite once weights move.

Cost: `relevance_score`, `find_related`, the LLM's node vocabulary, and every
paper metric that counts "apps the operator uses" are all diluted 2–3×, exactly
on the busiest nodes.

## 2 · Root cause

`GraphMemory.upsert_node` de-dups perfectly — **by exact `node_id` string**. The
bug is that one app produces several different `node_id`s, from independent
sources that each format `app:{whatever they call it}`.

### 2a · Two naming authorities, never reconciled

| Source | Field | Value on this box |
|---|---|---|
| `ActivityCollector` focus sensor → `APP_SWITCH` → `correlator._classify_into_graph` (`correlator.py:197`) and the focus patterns' `NodeSpec`s (`patterns.py:331/408/511`) | Wayland **`app_id`** (`ext_foreign_toplevel_handle_v1.app_id`) — reverse-DNS desktop identity | `com.system76.CosmicFiles`, `com.anthropic.Claude`, `brave-browser` |
| `ProcessCollector` census (B13) → `_heavy_app_specs` / `_top_app_nodes` (`patterns.py:110`) | Unix **process name** (`psutil` `proc.info["name"]`, grouped) | `cosmic-files`, `claude`, `brave` |

Both then do `f"app:{name}"`. Where the two happen to match (`code`,
`sublime_text`) you get one node; where they differ (every reverse-DNS app, every
browser, every Electron app) you get two. **This is a documented, deferred debt** —
`patterns.py:98-101` literally says *"where they differ (browsers: `brave` vs
`brave-browser`) a round-2 name map merges them (B13 §7)"* and B13 §7 lists it
under "If … / Then …" as never-built.

### 2b · The census reports thread names and helper-process names

`psutil.Process.name()` reads `/proc/<pid>/comm`, which a process (or its runtime)
can set to anything — hence `app:MainThread` (a Python process whose main thread
renamed `comm`) and `app:claude-desktop` **and** `app:claude` (the Electron main
binary vs a helper, both above the 200 MB group threshold). Grouping by `name`
(D-19(f)) assumed `name` ≈ "the app". It does not, for runtimes and Electron.

### 2c · No canonicalisation layer exists

`AppMap` (`diagnosis/app_map.py`, `data/app_map.default.toml`) looks like the
place for this but only maps `app_id → domain:<slug>`. It has no notion of
"`com.anthropic.Claude` and `claude` are the same thing." There is a **fuzzy
matcher** in `patterns.py` (`_focused_app_is_top_group`, `zed ↔ dev.zed.Zed`) but
it is used only for the `FocusSessionPattern` confidence bump — it never feeds
back into the node id.

### 2d · The merge machinery is already there, unused for this

`GraphMemory._merge_nodes_unsafe` (`graph_memory.py:746`) folds one node into
another with correct D-13 math **including the B13 resource attributes and edge
rewiring**. `consolidate()` (called by the DMN when idle) uses it — but only for
nodes with identical `node_type` **and case-insensitive `label`**. The dup pairs
have different labels (`"com.system76.CosmicFiles"` vs `"cosmic-files"`), so
`consolidate()` never touches them. The tool to merge exists; nothing tells it
these are dups.

## 3 · Fix

Three parts: resolve identity **before** the id is formed (so no new dups),
migrate the **existing** graph once (so the current dups collapse), and stop the
census emitting non-apps.

### 3a · `AppIdentity` — one resolver both paths call (`diagnosis/app_identity.py`, new)

A pure, synchronous resolver built at `SignalCorrelator.initialize()` from the
same TOML file as `AppMap` (a new `[identity]` section) plus a deterministic
normaliser. `resolve(raw: str) -> str` returns the **canonical key**; the node id
is then `f"app:{key}"` everywhere.

```
resolve("com.anthropic.Claude")  -> "claude"
resolve("claude")                -> "claude"
resolve("claude-desktop")        -> "claude"
resolve("brave-browser")         -> "brave"
resolve("Brave-Browser")         -> "brave"
resolve("dev.zed.Zed")           -> "zed"
resolve("some-new-app")          -> "some-new-app"   # unknown → normalised passthrough, still tracked
```

Resolution order (first hit wins):
1. **exact `[identity]` alias** — `"com.anthropic.Claude" = "claude"`. O(1). The
   escape hatch for anything the normaliser gets wrong; user-editable, a dogfood
   output like `webapp_map`.
2. **normalise**: lowercase → strip a reverse-DNS prefix to the last segment
   (`com.system76.CosmicTerm` → `cosmicterm`)… **no.** Reverse-DNS last-segment
   and process name do *not* reliably converge (`cosmicterm` vs `cosmic-term`).
   So the normaliser is deliberately minimal: lowercase, strip a trailing
   `-browser` / `-desktop` / `-bin` / `-gtk` / `-wayland`, collapse `_`/`.`/spaces
   to `-`, trim. It catches the easy half (`Brave-Browser` → `brave`, `foot` →
   `foot`) and **the alias table carries the reverse-DNS cases** (there are ~5–10
   per machine and they are stable).
3. passthrough of the normalised form.

**Canonical form = the shorter, process-name-like slug**, not the reverse-DNS id
— because the census can only ever produce the former, while the focus sensor can
be taught the alias. The default `[identity]` table ships the common desktop apps
(COSMIC, Brave, Chrome, Firefox, VS Code, Zed, Obsidian, Claude, Slack, …), same
"starting guess, real list is a dogfood output" stance as `app_map.default.toml`.

Call sites that change (all just wrap the raw name):
- `correlator._classify_into_graph`, `_classify_webapp_into_graph` (the browser
  node id in the `webapp PART_OF app` edge)
- `correlator.on_app_switch` — resolve once, pass the canonical id down
- `patterns.py` `_heavy_app_specs`, `_idle_app_spec`, `DistractionPattern` /
  `FocusSessionPattern` / `HeavyAppStartedPattern` `NodeSpec`s
- `AppMap.classify` still keys on the **raw** `app_id` (its rules are written
  against reverse-DNS ids and that is fine) — identity resolution and domain
  routing are independent lookups on the same raw string.

`label` on the node becomes the canonical key too, so `consolidate()` stays a
correct backstop.

### 3b · One-time graph migration — schema v5

`_SCHEMA_VERSION 4 → 5`. Add the first real entry to the `_migrate` hook the code
already anticipates (`graph_memory.py:58`). On loading a v4 file:

- for every `app:` / `webapp:` node, compute `resolve(label)`; group nodes whose
  canonical key collides; `_merge_nodes_unsafe` each group into one survivor
  (keep the one with the most edges, or oldest `created_at` on a tie) with id
  `app:<key>`.
- drop `app:MainThread` and any node whose canonical key is in a shipped
  `_NON_APP_NAMES` set (thread names, `sh`, `bash`, …) **iff it has no focus
  history** (`access_count == 0` and no `APP_SWITCH`-derived edge) — a real app
  the user actually focused is never dropped.
- bump written files to v5. A v5 file is refused by v4 code (same one-way gate as
  v3→v4).

Because the migration runs inside `GraphMemory.load`, it is covered by B9's
atomic-save + quarantine machinery: a migration that raises leaves the v4 file
untouched and the daemon boots degraded, exactly like a corrupt-graph load.

### 3c · Census hygiene (`sensing/collectors/process.py`)

- **drop thread-name rows**: `psutil` — read `proc.info["name"]`; if it equals the
  process's main-thread name or matches `_THREAD_NAME_RE` (`MainThread`,
  `Thread-\d+`, `asyncio_\d+`, …), fall back to the executable stem from
  `proc.name()` a second way, or skip the row. Names only — no `cmdline` (rules §6).
- **ship a real `process_exclude_names` default** (B13 §7(8), now informed by the
  soak, not guessed): the daemon itself and its tooling —
  `neuropacad`, `python3`, `node`, `chrome-devtools-mcp`, plus `cosmic-comp` /
  `Xwayland` (compositor infra, not "an activity"). Still a config list the
  operator can empty.
- resolve the surviving `name` through `AppIdentity` before it leaves the
  collector? **No** — keep the collector dumb (it has no `AppMap`); resolution
  stays in L3 where `AppIdentity` lives, applied to the census rows in
  `_heavy_app_specs` etc.

### 3d · Optional, if the soak still shows drift — observed-pairing learning

`FocusSessionPattern` already fuzzy-matches the focused `app_id` to the top census
group. When that match is confident (single unambiguous census row, high name
similarity) **and** the two canonical keys differ, append the pair to a
`data/app_identity.learned.toml` (separate from the shipped default, like the
learned instincts pattern). Held in reserve — the alias table + normaliser should
cover a single machine; auto-pairing risks mislabelling and needs its own care.

---

## 4 · Files (expected)

```
NEW   src/neuropaca/diagnosis/app_identity.py      AppIdentity resolver + default table loader
EDIT  data/app_map.default.toml                    new [identity] section (or a sibling data/app_identity.default.toml)
EDIT  src/neuropaca/diagnosis/correlator.py        resolve before every app: node id / edge
EDIT  src/neuropaca/diagnosis/patterns.py          resolve in every app NodeSpec + census spec
EDIT  src/neuropaca/core/graph_memory.py           _SCHEMA_VERSION 5; _migrate v4->v5 (canonical merge + non-app drop)
EDIT  src/neuropaca/sensing/collectors/process.py  thread-name guard; real exclude default
EDIT  src/neuropaca/core/config.py                 app_identity_path; process_exclude_names default populated
EDIT  tests/test_app_identity.py                   NEW — resolver table + normaliser + passthrough + collisions
EDIT  tests/test_graph_memory.py                   v4->v5 migration: dup collapse, edge/attr fold, non-app drop, idempotent
EDIT  tests/test_diagnosis_activity_patterns.py    NodeSpecs carry canonical ids; census+focus land on one node
EDIT  tests/test_process_collector.py              thread-name rows dropped; exclude list applied
EDIT  phases.md · RESEARCH_DOSSIER.md              B17 entry, dossier v7 (§8.4 graph identity, §16 the dup finding)
NEW   B17_TEST_REPORT.md
```

No systemd unit change. No new runtime dependency.

## 5 · Verification

### 5a · Unit

1. `AppIdentity`: the shipped table resolves the known reverse-DNS ids; the
   normaliser handles `-browser`/`-desktop`/case/separators; an unknown name
   passes through normalised and is still a valid node id; two raw names that
   should collide *do* resolve equal.
2. `graph_memory` v4→v5: a hand-built v4 graph with `app:brave` +
   `app:brave-browser` (+ a `webapp:` child on one, resource attrs on the other)
   → one `app:brave` node after load, `access_count` summed, `webapp` edge
   rewired, `ram_mb` kept, running the migration twice is a no-op. `app:MainThread`
   with ac 0 and no focus edge → gone; an `app:` node with ac 5 whose name is in
   `_NON_APP_NAMES` → **kept** (real focus history wins).
3. patterns: focus + census snapshots for the same app in one ingest cycle →
   `_update_graph` writes exactly one `app:<key>` node carrying both the focus
   edge and the census attributes.
4. `ProcessCollector`: a mocked `psutil` row with `name="MainThread"` is dropped;
   `name="neuropacad"` with a populated exclude list is dropped; `name="zed"`
   survives.
5. Full suite + `ruff check .` + `mypy` green.

### 5b · Migration on the real soak graph (copy, not in place)

`cp data/graph.json /tmp` → load under v5 code → assert node count drops from 24
`app:`/`webapp:` to ~12, every `webapp:` still reaches its browser, no dangling
edges, the merged Brave node has `ram_mb ≈ 3811` **and** `access_count ≈ 114`
**and** all four webapp children.

### 5c · Daemon, on the target box

Restart on B17 (the live graph auto-migrates once). ~20 min of normal use →
`neuropaca overview`: each app the operator touched is **one** node that has both
a domain edge and, if it is memory-heavy, resource attributes. `app:MainThread`,
`app:python3`, `app:neuropacad` absent. No new duplicate appears for an app used
under two identities in the same session.

### 5d · Soak

The continuing soak's graph migrates on the next daemon restart; note the
one-time node-count drop in `data/soak/soak.log` so it is not read as data loss.

## 6 · Exit criteria

1. One real app ⇒ one `app:` node, regardless of whether it was seen via focus,
   census, or both — across a 20 min real session and across a daemon restart.
2. The v4→v5 migration collapses the current soak graph's duplicates with every
   edge and resource attribute preserved, and is idempotent.
3. `app:MainThread` and daemon-self/tooling nodes do not appear in a fresh graph;
   a genuinely-focused app is never dropped by the non-app filter.
4. `AppMap` domain routing is unchanged (identity and routing are independent).
5. Full suite + `ruff` + `mypy` green; graph load stays within B1's 2 s / 10k-node
   budget with the migration in the path.

## 7 · Risks / open questions

- **Canonical-form choice.** Going to the short slug (`brave`, not
  `brave-browser`) means the *default* `[identity]` table must be reasonably
  complete for a COSMIC/GNOME box or the first-run graph still splits until the
  operator edits it. Mitigation: ship a solid default; the normaliser catches the
  `-browser`/`-desktop` half automatically; document it like `webapp_map`.
- **Over-merging.** Two genuinely different apps that normalise to the same slug
  (e.g. two "helper" binaries). The alias table is exact-match and wins first, so
  the fix is an explicit alias; the normaliser stays conservative (no reverse-DNS
  flattening for exactly this reason).
- **Migration on a large graph.** The soak graph is ~50 nodes; a 10k-node graph
  with the O(n) canonical-key pass + a handful of merges is well inside budget,
  but 5a-5 measures it.
- **`_migrate` is new infrastructure.** First real migration step — worth keeping
  it tiny and total (never partial): compute the whole plan, apply, bump version,
  or raise and leave the v4 file untouched.
- **Hebbian weights all `0.0`** is a separate bug found in the same review — it is
  *not* in B17's scope, but B17's dossier note should point at it so it is not
  lost.
