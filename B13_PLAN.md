# B13 — Resource-aware sensing + non-inert Idle/Distraction

**Status:** Draft plan, pre-approval. Not yet in `phases.md`.
**Author's context:** written 2026-09-08 after the "zero insights in 3 days" investigation on the B9 soak.
**Supersedes nothing. Depends on:** B2.5b (activity/app_map), B3 (patterns), B4 (L4 gate), B7 (L5 pressure).

---

## 1. Why this phase exists

The B9 soak has accrued ~3 days of runtime and produced:

```
Sensing     66 idle/active edges, 28 app switches
Drive       0 contributions, 0 low / 0 high crossings
Cognition   0 insights, 0 actions proposed
```

Root cause, traced end to end:

1. **L4 only consumes `SIGNAL_CORRELATED`.** Of the four L3 patterns, only `HighLoadPattern` and `FocusSessionPattern` attach nodes to their signal. `IdlePattern` and `DistractionPattern` emit `related_node_ids = ()`.
2. A nodeless signal is **dropped by L4 at the `no_nodes` gate** (before any inference) and **no-op'd by L5** (`for node_id in signal.related_node_ids` over an empty tuple). It fires and vanishes.
3. The two node-bearing patterns need conditions an unattended machine never creates: sustained **>90 % CPU for 5 min with concurrent file churn**, or a **20-min uninterrupted focus hold at ≥10 % mean CPU** in `engineering`/`research`. On an idle soak box neither happens; per the [B7 memory](.claude/…/b7-positive-control-and-soak-blindspot.md) `HighLoadPattern` only ever fired via an injected positive control.
4. Everything downstream of L3 is in-memory and resets on each daemon restart (~1/80 min in the soak), so multi-snapshot patterns can't mature.

Two independent fixes, bundled here because they are complementary and touch the same files:

- **B13-A — make Idle/Distraction non-inert.** They already fire; give them nodes so L4/L5 can act. Small, no schema change.
- **B13-B — resource-aware sensing.** CPU is the wrong primary signal for "what is this person doing" — it is bursty and mostly ~0. **RAM footprint is the stable indicator** of what is loaded and being worked with (browser tab-sets, IDE projects, VMs, containers, media tools). Add memory as a first-class sensed dimension, per-process, grouped by app, with **how long the app/activity has been running** as the third axis.

Neither fix makes the *unattended soak* produce insights — after excluding the daemon's own work there is nothing behaviourally rich left. They make the loop work during **real interactive dogfooding**, and the soak's job is narrowed to what it can actually validate (plumbing, restart-safety, decay, bounded growth, cost).

---

## 2. Scope, in dependency order

| Part | What | Schema/enum change? | Approval gate |
| --- | --- | --- | --- |
| **B13-A** | `IdlePattern` / `DistractionPattern` attach nodes | none | Architecture.md §5 line edit |
| **B13-B1** | Aggregate memory-pressure pattern (uses existing `mem_percent` + baseline) | none | Architecture.md §5 |
| **B13-B2** | `ProcessCollector` — per-process RAM/CPU/runtime census, app-grouped | new collector class | new public class, config fields, Architecture.md §4 |
| **B13-B3** | `Node` gains durable resource attributes (`ram_mb`, `cpu_percent`, `first_seen_at`, `last_seen_at`) | **schema v-bump** | D-19, human approval |
| **B13-B4** | RAM-aware patterns: generalise `HighLoadPattern` → resource pressure; RAM corroboration in `FocusSessionPattern`; new `HeavyAppStartedPattern` | **new `SignalType`** | D-19, human approval |
| **B13-C** | 7-day soak re-run under B9 harness with B13 config | none | — |

**B13-A ships first and alone** — it is the highest value-per-line change in the set and needs no approval beyond a doc edit.

---

## 3. Decisions to ratify (D-19)

These are the choices baked into the plan. Each needs an explicit yes before implementation.

### D-19(a) — Reuse `NodeType.APP`, do not add `NodeType.PROCESS`

A memory-heavy process resolves, via `app_map` / process name, to the **same `app:<id>` node** a focus event would create. Distinct concepts get an **id-prefix convention** (`app:<id>` for a grouped application; there is no per-PID node). This is the B8 precedent (`ephemeral:` / `idle:` / `insight:` prefixes carry semantics, not attributes) and it avoids an enum change. Rejected: `NodeType.PROCESS` — would fragment the graph (a focused app and a heavy app becoming two nodes) and cost a schema bump for no analytic gain.

### D-19(b) — RSS for grouping now, PSS flagged as the known inaccuracy

`psutil.Process.memory_info().rss` summed across an app's processes **double-counts shared libraries** (a 15-process browser looks larger than it is). Accepted for B13 because it is fast and loop-safe. `memory_full_info().uss` (accurate, private set) is 10–50× slower and sometimes privileged; **PSS** via `/proc/<pid>/smaps_rollup` is the honest compromise and is the documented follow-up if the soak shows the inflation matters. Recorded as a rejected-alternatives entry for the paper.

### D-19(c) — Minimal self-exclusion even in round 1

The operator's instruction was "don't bother excluding, see the results" — amended to: exclude **only** `os.getpid()` + `psutil.Process().children(recursive=True)`. Three lines, and it stops the daemon from observing its own ~1477 MiB BitNet RSS climb and (in a non-dry-run future) proposing to kill itself. Claude Code, `pytest`, `node`, and the soak harness are **not** excluded in round 1 — the first soak's graph will carry `app:python` / `app:node` noise, which is recoverable and informative (it tells us the census works). Round-2 exclusion list is a soak output, not a guess.

### D-19(d) — Idle attaches to the last-active app, not `YOU`, not a `SESSION` node

`IdlePattern` gains `"activity"` in its `collectors` and emits one `NodeSpec` for the last focused `app:<id>`. Semantically "you went idle after working in X" — a session boundary marker on **connected, traversable** structure (that app links to its domain and to idle-thoughts). Rejected: attaching to `YOU` (semantically muddy; floods the one hub `find_related` refuses to traverse); emitting a `SESSION` node (`NodeType.SESSION` exists and is unused — this is the right long-term model, but it needs a "what happened since last idle" accumulator in the correlator and is its own phase). If the activity collector is dead (headless), Idle stays nodeless — acceptable degradation.

### D-19(e) — New `SignalType.WORKING_SET_CHANGE`

For `HeavyAppStartedPattern`. Enum change → schema-version bump → approval. Rejected: overloading `HIGH_LOAD` (conflates a transient CPU episode with "a big app appeared" — different confidence math, different node attribution).

### D-19(f) — 200 MB grouped-total threshold, name-based grouping, empirically tuned

An app is censused when its **summed RSS across all same-name processes ≥ 200 MB**. Grouping key is the process `name` (`brave` × 15 → one `brave` row). Not the group *maximum*, not 100 MB (too low — Electron renderers idle at 200–500 MB each). The soak validates the number; expect to revise.

---

## 4. Part detail

### B13-A · Non-inert Idle / Distraction

**Files:** `src/neuropaca/diagnosis/patterns.py`, tests, `Architecture.md` line 404.

**`DistractionPattern`** already computes `distinct` (the list of distinct `app_id`s in the 2-min thrash window) and discards it. Change: populate `node_specs`:

```python
node_specs = tuple(
    NodeSpec(node_id=f"app:{app_id}", node_type=NodeType.APP, label=app_id)
    for app_id in distinct
)
```

No `edges` — the `app --part_of--> domain` edge is owned by the `APP_SWITCH` classification path. **Do not re-emit it**: `_add_edge_unsafe` keys on `(source, target, relation)` and a second `add_edge` with the same key **overwrites**, resetting `weight` to 0 and wiping any Hebbian reinforcement.

**`IdlePattern`** — add `"activity"` to `collectors`; in `_draft`, read `windows.get("activity", ())`; if the last snapshot carries an `app_id`, emit `NodeSpec(f"app:{app_id}", APP, label=app_id)`. Empty when no activity data.

**Downstream, unchanged, now live:**
- L4 can form `distraction: rapid switching implicates app:slack` — a real insight (it was dropped at `no_nodes` before).
- L5 accumulates pressure on the thrash-set apps. Repeated distraction on the same apps is exactly the accumulation the gradient is designed for.
- Hebbian: the thrash-set apps co-occur in the episode; an insight over them reinforces the edges between them.

**Known ceiling (documented, not fixed here):** on a 3-app machine, Distraction always cites the same apps, so L4's Jaccard-novelty gate (`> 0.8`) drops every distraction signal after the first. B13-B widens the app vocabulary, which is what lets either change produce *sustained* insight flow. A test asserts this ceiling so the behaviour is intentional, not a surprise.

**Architecture.md §5 line 404** currently reads *"`IdlePattern` / `DistractionPattern` write none."* — must change. Per `rules.md §9` this is a doc edit requiring sign-off (small).

---

### B13-B1 · Aggregate memory-pressure pattern

**Files:** `src/neuropaca/diagnosis/patterns.py`, `src/neuropaca/core/config.py`, tests.

The `system` snapshot already carries `mem_percent` and `mem_available_mb`, and `SignalCorrelator` already maintains a `MetricBaseline` (rolling mean + population stddev) for every numeric metric — so `baselines.zscore("system", "mem_percent", value)` works today with no new sensing.

New `MemoryPressurePattern` (`_RunLengthPattern` subclass, `collectors = ("system",)`):
- `_hit`: `mem_percent` z-score `> _MEM_PRESSURE_Z` (default 2.0) **or** `mem_available_mb < _MEM_PRESSURE_FLOOR_MB` (default configurable, e.g. 1024)
- sustained `_MEM_PRESSURE_SUSTAIN_SECONDS` (default 180) — shorter than HighLoad's 300 because a memory ceiling is a slower, more meaningful event than a CPU spike
- `_cleared`: z-score `< 1.0` and available above floor
- confidence from z-score margin, same shape as `HighLoadPattern`
- **node attribution:** the memory-heavy `app:<id>` nodes from the concurrent `process` snapshot (B13-B2). If B13-B2 is not yet merged, attaches nothing and is effectively inert until then — that ordering is fine.

Config fields: `mem_pressure_z: float = 2.0`, `mem_pressure_floor_mb: float = 1024.0`, `mem_pressure_sustain_seconds: float = 180.0`.

---

### B13-B2 · `ProcessCollector`

**Files:** new `src/neuropaca/sensing/collectors/process.py`, `src/neuropaca/sensing/collector_module.py` wiring, `src/neuropaca/core/config.py`, `build_modules()`, tests.

A polled `BaseCollector` (`is_blocking = True` — `process_iter` + memory reads cost tens of ms; always `asyncio.to_thread`'d by `XMetricCollector`). Poll interval configurable, default 60 s aligned with `system`.

**`collect()` algorithm:**

1. `own = {os.getpid()} | {p.pid for p in psutil.Process().children(recursive=True)}` (D-19(c)).
2. Iterate `psutil.process_iter(["name", "memory_info", "cpu_percent", "create_time"])`. For each, skip if `pid in own` or `NoSuchProcess` / `AccessDenied`.
3. Group by `name`. Per group accumulate: summed `rss`, summed `cpu_percent`, **earliest `create_time`** (→ `oldest_start`), process count.
4. Keep groups where **summed RSS ≥ `process_min_rss_mb`** (default 200).
5. Emit `MetricSnapshot(collector_name="process", data={"processes": [ {name, rss_mb, cpu_percent, proc_count, running_seconds} … ] })`, sorted by `rss_mb` desc (D-19: RAM is the primary sort key).

`running_seconds = now - oldest_start` — the "how long has this app been running" axis.

**Privacy (`rules.md §6`):** `name` only. **Never** `cmdline`, `exe`, `environ`, `open_files`, `connections`. The existing `_top_processes_by_cpu` helper already sets this precedent ("Names only — never cmdline/args"). A test greps the collector for those psutil attributes and fails if any appear.

**Config:** `process_collector_enabled: bool = True`, `process_min_rss_mb: float = 200.0`, `poll_intervals` gains a `"process"` default.

**`SignalCorrelator` integration:** the correlator subscribes `METRIC_COLLECTED` already; it gets `process` snapshots for free into its per-collector deque. Patterns that want the census read `windows["process"]`.

---

### B13-B3 · Durable resource attributes on `Node`

**Files:** `src/neuropaca/core/models.py` (`Node`), `src/neuropaca/core/graph_memory.py` (`_add_node_unsafe`, `_node_to_attrs`, `_node_record`, `_deserialise`, `_UPSERT_PROTECTED`, `_SCHEMA_VERSION`), `Architecture.md §3`, tests.

**The problem this solves:** `Node` persists a *fixed* field set. Any `ram_mb` / `running_seconds` hung on a node via `upsert_node(attributes=…)` is **silently discarded** — dropped at creation by `_add_node_unsafe` (which only reads known keys) and again on every `save()` (`_node_record` serialises only the dataclass fields). This is the [ad-hoc-attribute drop](.claude/…/graph-drops-adhoc-node-attributes.md) that bit B8. Per B8's own postmortem: *"selecting on an attribute would mean that after one daemon restart every ephemeral node looked permanent."*

**Change:** add to `Node`:

```python
ram_mb: float = 0.0            # last observed grouped RSS for this app, MB
cpu_percent: float = 0.0       # last observed grouped CPU
first_seen_at: datetime | None = None   # first census sighting
last_seen_at: datetime | None = None    # most recent census sighting
```

- `_add_node_unsafe` reads them from `attributes`.
- `_node_to_attrs` / `_node_record` / `_deserialise` round-trip them.
- `_UPSERT_PROTECTED` does **not** include `ram_mb` / `cpu_percent` / `last_seen_at` (they update every census) but **does** include `first_seen_at` (write-once, like `created_at`).
- `_SCHEMA_VERSION` bumps (currently the schema tracks `surfaced_at` as v2; this is v3). `_MIN_READABLE_SCHEMA_VERSION` unchanged — a v2 graph loads, these fields default.
- `recalculate_importance` is untouched for now; whether `ram_mb` feeds `relevance_score` is a **deliberate non-goal of B13** (see §7 rejected alternatives — changing the score formula changes the graph's meaning and deserves its own discussion).

**Who writes them:** `SignalCorrelator`, when a pattern's `NodeSpec` for an `app:<id>` is upserted and the concurrent `process` snapshot has a matching row. The `NodeSpec` dataclass gains an optional `attributes: dict[str, float] = field(default_factory=dict)` field passed through to `upsert_node`.

---

### B13-B4 · RAM-aware patterns

**Files:** `src/neuropaca/diagnosis/patterns.py`, `src/neuropaca/core/enums.py` (new `SignalType`), `Architecture.md §5`, tests.

**1. Generalise `HighLoadPattern` → resource pressure.** Keep the class name (Architecture.md contract) but `_hit` becomes `cpu > threshold` **OR** the `MemoryPressurePattern` predicate. A memory-triggered firing attaches the **heavy `app:` nodes** from the `process` window (not `file:` nodes — memory pressure is not file churn). Confidence math forks on which arm fired. *(If keeping one class doing two things reads badly in review, split into `HighLoadPattern` (unchanged) + `MemoryPressurePattern` (B13-B1) and drop this item — B13-B1 already covers the memory arm. Reviewer's call.)*

**2. `FocusSessionPattern` RAM corroboration.** After the existing gates pass, check the concurrent `process` window: if the focused `app_id` is the **largest non-browser RSS group**, add a corroboration bonus to confidence (`+0.15`, clamped). A 20-min focus session currently emits `confidence = 0.6`, **below L4's 0.7 gate** — this is a named reason for zero insights. Corroboration pushes a genuine deep-work session over the line. No new nodes, no new signal type.

**3. New `HeavyAppStartedPattern`** (`signal_type = SignalType.WORKING_SET_CHANGE`, `collectors = ("process",)`):
- fires once when an app group crosses `process_min_rss_mb` that was **not present in the previous `process` snapshot** (edge-triggered on appearance; re-arms when the app drops below threshold or disappears)
- `confidence = 0.5` (low — "something started", not "something's wrong")
- attaches that one `app:<id>` node with `ram_mb` / `first_seen_at` / `running_seconds` attributes
- during real use fires on "launched a VM", "opened Blender", "Docker came up" — every firing widens the graph's input surface

---

### B13-C · Soak re-validation

Re-run the B9 7-day soak harness (`neuropaca-soak.service`, session ledger, boot popup) with a `neuropaca.b13.toml` config: `process_collector_enabled = true`, `process_min_rss_mb = 200`, the new pattern config at defaults, `action_dry_run = true`.

The boot popup's counter block gains: `Census   N app groups tracked, top <name>@<rss>` and `Cognition  M insights (K from idle/distraction)`.

---

## 5. Testing plan (heavy)

`rules.md §8`: every module ships tests in the same change; no test loads a real model; no test sleeps; no test touches real `psutil` outside an integration marker.

### Unit — patterns

| Test | Asserts |
| --- | --- |
| `test_distraction_attaches_the_distinct_apps` | 6 switches across `{brave, slack, term}` → `signal.related_node_ids == ("app:brave","app:slack","app:term")`, order stable |
| `test_distraction_does_not_re_emit_domain_edges` | after firing, the pre-existing `app:brave --part_of--> domain:habits` edge still has its accumulated `weight` (not reset to 0) |
| `test_idle_attaches_last_active_app` | activity window ending on `CosmicTerm` → idle signal cites `app:com.system76.CosmicTerm` |
| `test_idle_with_no_activity_data_stays_nodeless` | `collectors` has `"activity"` but window empty → `related_node_ids == ()`, no crash |
| `test_memory_pressure_fires_on_sustained_z` | synthetic `mem_percent` series 3σ above baseline for 4 samples → fires once, silent on the negative (flat series) |
| `test_memory_pressure_rearms` | fires, clears below 1σ, fires again on a second episode |
| `test_focus_session_ram_corroboration_crosses_l4_gate` | 20-min focus + editor is top RSS → confidence ≥ 0.7 (was 0.6) |
| `test_heavy_app_started_edge_triggers_on_appearance` | app absent → app at 600 MB → fires once; stays silent while it persists; re-arms after it disappears |
| `test_heavy_app_started_ignores_sub_threshold` | app at 150 MB → no signal |
| `test_a_synthetic_6th_pattern_registers_with_no_correlator_change` | B3 exit criterion still holds after the additions |

### Unit — `ProcessCollector`

| Test | Asserts |
| --- | --- |
| `test_groups_same_name_processes_and_sums_rss` | fake `process_iter` with 15 `brave` @ ~120 MB each → one `brave` row, `rss_mb ≈ 1800`, `proc_count == 15` |
| `test_threshold_is_on_the_group_total` | one 250 MB process kept; five 40 MB same-name processes (200 MB total) kept; five 30 MB (150 MB) dropped |
| `test_self_and_children_are_never_censused` | inject own pid + a child pid → absent from output |
| `test_running_seconds_uses_earliest_create_time` | group with starts at T-100 / T-40 → `running_seconds ≈ 100` |
| `test_sorted_by_rss_descending` | RAM is the primary sort key |
| `test_never_reads_cmdline_or_environ` | source scan: no `cmdline` / `exe` / `environ` / `connections` / `open_files` token in `process.py` |
| `test_access_denied_on_one_process_does_not_abort_the_census` | one process raises `AccessDenied` → the rest are still collected |
| `test_collect_is_pure_and_returns_a_typed_snapshot` | `MetricSnapshot(collector_name="process")`, `data["processes"]` a list of dicts |

### Unit — schema round-trip (the B8 precedent)

| Test | Asserts |
| --- | --- |
| `test_resource_attributes_survive_a_save_and_reload` | upsert `app:x` with `ram_mb=812.0`, `first_seen_at=…`; `save()`; fresh `GraphMemory.load()`; fields intact |
| `test_first_seen_at_is_write_once` | second upsert with a different `first_seen_at` does not overwrite |
| `test_ram_mb_updates_on_reobservation` | second upsert with `ram_mb=910.0` wins (not protected) |
| `test_a_v2_graph_loads_under_v3` | v2 fixture (no resource fields) loads, fields default, no error |
| `test_schema_version_is_written_as_v3` | `save()` output carries `schema_version: 3` |

### Integration (`FakeInferenceBackend`, `FakeClock`)

| Test | Asserts |
| --- | --- |
| `test_a_distraction_signal_now_produces_an_insight` | full L3→L4 wiring: distraction fires → L4 gate passes (nodes present, confidence ok) → `INSIGHT_GENERATED` published, `insight.category == "distraction"`, `traces_to_evidence()` true |
| `test_distraction_pressure_accumulates_and_crosses_low_threshold` | L3→L5: three distraction episodes on `app:slack` inside a half-life → `PRESSURE_THRESHOLD_REACHED{tier:"low"}` on `app:slack` |
| `test_idle_pressure_lands_on_the_last_active_app` | L3→L5: idle signal → pressure on `app:com.system76.CosmicTerm`, reason string mentions idle |
| `test_novelty_gate_still_shuts_down_repeat_distraction_on_a_small_graph` | 3-app graph, 5 identical distraction episodes → exactly 1 insight (documents the ceiling as intended) |
| `test_memory_pressure_signal_attaches_the_heavy_apps` | `process` window has `brave`@2 GB + `code`@1.5 GB; `mem_percent` spikes → signal cites `app:brave`, `app:code` |
| `test_heavy_app_started_to_insight` | app appears at 800 MB → `WORKING_SET_CHANGE` signal → L4 insight citing that app |

### Isolation / performance (`rules.md §8` — "the event loop never stalls during inference")

| Test | Asserts |
| --- | --- |
| `test_process_census_never_stalls_the_loop` | `ProcessCollector.collect` stubbed to a 500 ms blocking sleep; `XMetricCollector` runs it via `to_thread`; max measured loop lag < 10 ms while it runs (pattern of `test_l4_executor_isolation.py`) |
| `test_census_cost_bound` (integration marker) | real `psutil` on the dev box, 60 s of polling: p95 `collect()` wall time recorded, asserted < 1000 ms |
| `test_snapshot_stays_bounded_under_process_churn` (stress) | fake `process_iter` yielding 2000 short-lived distinct names across a poll series → `data["processes"]` length bounded by the ≥200 MB filter, correlator deque bounded, RSS flat |

### Privacy

| Test | Asserts |
| --- | --- |
| `test_process_snapshot_carries_no_identifying_strings` | census output fields are exactly `{name, rss_mb, cpu_percent, proc_count, running_seconds}` — no paths, no args |
| `test_graph_json_after_a_census_soak_has_no_cmdline` (integration) | run the census against real psutil for 5 min, dump the graph, grep for `/home/`, `--`, `.py ` in node data → none |

### Soak (B13-C) — pass/fail counters, not a green bar

Over a bounded window (48 h minimum, 7 d target):
- `ProcessCollector` self-disable count == 0
- census produced ≥ 1 `app:` node with non-zero `ram_mb` and `running_seconds`
- daemon RSS slope within the B9 envelope (the census must not itself leak — it builds a fresh list every poll; `gc.freeze()` after `load()` still holds)
- ≥ 1 `distraction` or `idle`-attributed insight **if** real interactive use occurred during the window (checked against the activity-edge count — if the box was genuinely untouched, this is waived, same as the [B7 soak waiver](.claude/…/b7-positive-control-and-soak-blindspot.md))

---

## 6. Exit criteria

| ✅/⏳ | Criterion | Evidence |
| --- | --- | --- |
| ⏳ | `DistractionPattern` and `IdlePattern` each attach ≥ 1 real node on a recorded fixture and stay nodeless-safe on the negative | `test_distraction_attaches_the_distinct_apps`, `test_idle_attaches_last_active_app`, `test_idle_with_no_activity_data_stays_nodeless` |
| ⏳ | An idle/distraction signal round-trips L3→L4 to a stored, evidence-tracing insight | `test_a_distraction_signal_now_produces_an_insight` |
| ⏳ | An idle/distraction signal round-trips L3→L5 to a `PRESSURE_THRESHOLD_REACHED` | `test_distraction_pressure_accumulates_and_crosses_low_threshold` |
| ⏳ | No pattern re-emits a structural edge (no Hebbian weight is ever reset by a pattern firing) | `test_distraction_does_not_re_emit_domain_edges` |
| ⏳ | `ProcessCollector` groups by name, thresholds on the group total, sorts by RAM, and never censuses self/children | `test_groups_same_name_processes_and_sums_rss`, `test_threshold_is_on_the_group_total`, `test_self_and_children_are_never_censused` |
| ⏳ | The census reads process **names only** — no cmdline/exe/environ/connections, verified by source scan and by a real-psutil graph grep | `test_never_reads_cmdline_or_environ`, `test_graph_json_after_a_census_soak_has_no_cmdline` |
| ⏳ | Resource attributes (`ram_mb`, `cpu_percent`, `first_seen_at`, `last_seen_at`) survive save/reload; `first_seen_at` is write-once; a v2 graph loads under v3 | `test_resource_attributes_survive_a_save_and_reload`, `test_first_seen_at_is_write_once`, `test_a_v2_graph_loads_under_v3` |
| ⏳ | `MemoryPressurePattern` fires on a sustained z-score / low-available episode, stays silent on the negative, and re-arms | `test_memory_pressure_fires_on_sustained_z`, `test_memory_pressure_rearms` |
| ⏳ | `FocusSessionPattern` RAM corroboration lifts a real 20-min session past L4's 0.7 confidence gate | `test_focus_session_ram_corroboration_crosses_l4_gate` |
| ⏳ | `HeavyAppStartedPattern` edge-triggers once on app appearance and re-arms | `test_heavy_app_started_edge_triggers_on_appearance` |
| ⏳ | The census poll never stalls the loop > 10 ms; p95 `collect()` < 1 s on the target box | `test_process_census_never_stalls_the_loop`, `test_census_cost_bound` |
| ⏳ | Snapshot and correlator deque stay bounded under a 2000-process churn storm; census does not leak over the soak | `test_snapshot_stays_bounded_under_process_churn`, B13-C RSS slope |
| ⏳ | The novelty-gate ceiling on a small graph is asserted as intentional | `test_novelty_gate_still_shuts_down_repeat_distraction_on_a_small_graph` |
| ⏳ | A synthetic new pattern still registers with zero `SignalCorrelator` changes (B3 invariant preserved) | `test_a_synthetic_6th_pattern_registers_with_no_correlator_change` |
| ⏳ | 48 h+ soak: census self-disable count 0, ≥ 1 populated `app:` node, RSS slope within B9 envelope; insight criterion waived only if the box was provably untouched | B13-C ledger + popup |
| ⏳ | `Architecture.md` §4 (sensing), §5 (patterns, incl. the line 404 correction), §3 (schema v3) updated and approved | doc diff |

---

## 7. Risks and rejected alternatives

### If… / Then…

| If… | Then… |
| --- | --- |
| the soak shows RSS-summed group totals wildly overstate real memory (browser at "8 GB") | switch grouping RSS → PSS via `/proc/<pid>/smaps_rollup`, accept the ~5–15× slower census, re-poll at 120 s |
| `app:python` / `app:node` / `app:claude` dominate the census and pollute the graph | add the round-2 name/cgroup exclusion list (informed by the soak, not guessed); consider cgroup-scope grouping (`app-<app_id>.scope`) which also fixes multi-Electron-app name collisions |
| the novelty gate still collapses insight flow even with a wider app vocabulary | scale the Jaccard threshold with node count (`0.8` at ≤ 20 nodes → `0.6` at ≥ 100), as its own small change |
| memory pressure fires constantly because the 16 GB box runs near-full during dev | the pattern is z-score-relative to the box's own baseline, not absolute — but if it still flaps, raise `mem_pressure_z` to 2.5 and lengthen the sustain window |
| `ProcessCollector` hits the inotify/`AccessDenied` wall on some processes | already handled — per-process `try/except`, the census is best-effort; a whole-collector failure self-disables like any other (`XMetricCollector` max-failures) |
| reviewers reject one class (`HighLoadPattern`) doing CPU-or-memory | drop B13-B4 item 1; `MemoryPressurePattern` (B13-B1) already stands alone |

### Rejected alternatives (paper deliverable — [research-paper goal](.claude/…/neuropaca-research-paper-goal.md))

1. **`NodeType.PROCESS`** — fragments the graph, costs a schema bump, no analytic gain over the `app:` id-prefix convention. (D-19(a))
2. **PSS/USS from day one** — 10–50× slower, sometimes privileged; RSS is good enough to validate the *shape*, and the soak will say if precision matters. (D-19(b))
3. **Special-casing L5/L4 to accept nodeless signals** (a global `__system__` pressure bucket) — fights the node-keyed architecture throughout L5; attaching nodes is in the grain. (§4 B13-A)
4. **Idle → `YOU`** — semantically muddy and floods the one hub `find_related` refuses to traverse. (D-19(d))
5. **Idle → `SESSION` node now** — the right long-term model, but needs a correlator-side session accumulator; deferred to a later phase. (D-19(d))
6. **Feeding `ram_mb` into `relevance_score`** — changes what the graph *means* (importance would track memory footprint, not behavioural salience); out of scope for B13, needs its own design discussion. (§4 B13-B3)
7. **100 MB census threshold** — below the idle footprint of a single Electron renderer; produces 40+ rows of renderer shards. (D-19(f))
8. **Full self+tooling exclusion in round 1** — the operator wants to see the unfiltered census first; only the minimal PID+children guard is kept. (D-19(c))

---

## 8. PR breakdown

| PR | Contents | Approval needed before merge |
| --- | --- | --- |
| **B13-A** | Idle/Distraction node attachment + tests + Architecture.md §5 line 404 | doc edit sign-off |
| **B13-B2** | `ProcessCollector` + config + wiring + unit/perf/privacy tests (census produces snapshots, nothing consumes them yet) | new public class, config fields, Architecture.md §4 |
| **B13-B3** | `Node` schema v3 + graph_memory round-trip + `NodeSpec.attributes` + tests | **D-19, schema-version bump, human approval** |
| **B13-B1 + B13-B4** | `MemoryPressurePattern`, `FocusSessionPattern` corroboration, `HeavyAppStartedPattern`, new `SignalType`, correlator writes resource attrs on upsert, integration tests | **D-19, new enum member, human approval** |
| **B13-C** | `neuropaca.b13.toml`, soak popup counter additions, run the soak | — |

B13-A is independently shippable and should go first. B13-B2 can land and bake (collecting data, feeding nothing) while B13-B3/B4 are in review.

---

## 9. Non-goals

- No change to `relevance_score` / `recalculate_importance`.
- No `SESSION` node, no session accumulator (later phase).
- No `USER_RETURN` pattern (adjacent opportunity, noted, not scoped).
- No cgroup-based grouping in round 1 (name-based; cgroup is the round-2 answer to noise).
- No action *class* for "app causing distraction" — the value in B13 is L4 insights + graph structure + pressure being *possible*; a distraction-mitigation action is a separate proposal and gated regardless.
- The unattended soak is **not** expected to produce insights; B13 makes the loop work under real use and narrows the soak to plumbing validation.
