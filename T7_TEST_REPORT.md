# T7 · Test report — Hebbian co-occurrence, "wire together" built

**Branch:** `t7-hebbian-weights-zero` · **Plan:** `T7_PLAN.md` · **Date:** 2026-09-10

`ruff check .` clean · `mypy` (src) clean · **673 passed** (`-m "not integration"`,
+17 vs `main`) · **10 passed** (`-m integration`, +1 vs `main`) · 3 skipped
(pre-existing, unrelated).

---

## What changed

| Area | Before | After |
|---|---|---|
| `_add_edge_unsafe` | networkx `add_edge` on an existing `(u,v,key)` merged kwargs → `weight` reset to `0.0`, `created_at` bumped | re-asserting an existing edge is a no-op that returns it unchanged; only a new edge is written |
| Hebbian primitive | `reinforce_cooccurrence` — bump existing edges only | + `wire_cooccurrence` — create the pair edge (`RELATED_TO`, `base` weight) when absent and both ends are `app:`/`webapp:`, else bump by `delta` |
| Driver | only `_store_insight` (model-gated, ~5/day) | + co-activation window in `on_app_switch` — every focus switch wires the new focus to everything focused within `coactivation_window_seconds`; `_store_insight` now uses `wire_cooccurrence` at `delta × hebbian_insight_multiplier` |
| Decay | none — weights could only grow | `decay_cooccurrence_edges(factor, floor)` in the B6 idle sweep; sub-floor edges pruned, never re-orphaning a node |
| Config | `_HEBBIAN_DELTA = 0.01` module constant | 7 validated `Config` knobs |

---

## Verification (maps to `T7_PLAN.md` §7)

| # | Check | Test | Result |
|---|---|---|---|
| 1 | `_add_edge_unsafe` upsert — re-add keeps weight 0.35 + original `created_at` | `test_graph_memory.py::test_add_edge_never_resets_an_existing_edges_weight` | ✅ |
| 2 | `wire_cooccurrence` create — 3 unconnected `app:` → 3 `RELATED_TO` @ `base`, `(3,0)`; re-run `(0,3)` @ `base+delta` | `test_wire_cooccurrence_creates_then_strengthens_activity_pairs`; integration `test_wire_cooccurrence_creates_the_mesh_the_correlator_never_builds` | ✅ |
| 3 | Caps — `max_episode` truncates to first N; `max_new_edges` bounds creations | `test_wire_cooccurrence_caps_episode_size`, `..._caps_new_edges_per_call` | ✅ |
| 4 | Activity-only creation — `[app, file, concept]` creates nothing; an existing edge to a non-activity node is still bumped | `test_wire_cooccurrence_only_creates_between_activity_nodes`; integration tail | ✅ |
| 5 | Co-activation window — two switches 60 s apart wire the pair @ `base`; revisit within window → `base+delta`; switch 600 s later (window 300 s) → no edge; deque ≤ `coactivation_max_nodes`; no self-edge on repeat focus; webapp focus wires the `webapp:` node | `test_cooccurrence_window.py` (6 tests) | ✅ |
| 6 | Insight path — `_store_insight` bumps pre-existing episode edges at `delta × multiplier` (0.03), still one lock, still < 50 ms loop lag on the 10k fixture | `test_learning.py::test_hebbian_bump_on_co_occurring_edge`; integration `test_store_insight_with_50_citations_stays_off_the_loop` | ✅ (`wall` + `max loop lag` printed, well under `_LAG_LIMIT_MS`) |
| 7 | Decay — `RELATED_TO` weights × `factor`; sub-`floor` edge pruned unless it is an endpoint's last edge; weight-0 structural `RELATED_TO` (insight→cited, orphan→YOU) untouched | `test_decay_cooccurrence_edges_fades_and_prunes`, `..._never_reorphans_a_node`, `..._leaves_structural_related_to_alone` | ✅ |
| 8 | `skips_absent_ids` — an episode id not in the graph is ignored | `test_wire_cooccurrence_skips_absent_ids` | ✅ |
| 9 | Full suite + `ruff` + `mypy` (src) green; no regression in `test_diagnosis.py`, `test_webapp_pipeline.py`, `test_dmn*`, `test_orchestrator*` | full run | ✅ |

**Deferred to soak (plan §7.10–7.11):** live daemon run showing a spread of
non-zero `RELATED_TO` weights between co-used apps with `PART_OF` hub edges
untouched; a `weight_nonzero_fraction` / `max_cooccurrence_weight` probe added to
`scripts/soak_probe.py`. T7 closes when a ≥ 24 h soak shows a stable non-zero
distribution that decay keeps bounded.

---

## Behaviour changes a reviewer should know

- **`reinforce_cooccurrence` is now unused in `src/`** — kept as the public
  bump-only primitive (integration test + external callers). `wire_cooccurrence`
  is the superset the daemon uses.
- **`_store_insight` bump is 3× larger** (`hebbian_insight_multiplier`) — a
  model-confirmed association is stronger evidence than a bare co-activation.
  `test_learning.py` updated to assert the multiplier-aware delta.
- **The integration test no longer pre-wires its fixture** for the create path —
  `test_wire_cooccurrence_creates_the_mesh_the_correlator_never_builds` exercises
  the exact production gap (activity nodes with no peer edges).
- **`DMN._reminiscence` summary line** gained a `faded N` field.
- **First non-zero edge weights ever** → `relevance_score` will shift for nodes in
  a dense co-occurrence mesh (`connectivity` term reads `degree`; new
  `RELATED_TO` edges raise it). Bounded by `max_new_edges` + decay; re-baseline in
  the soak.
