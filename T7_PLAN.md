# T7 · Hebbian edge weights never leave zero — root cause + fix plan

**Branch:** `t7-hebbian-weights-zero` (off `main`, after B17)
**Found:** 2026-09-10, B17 graph review. Logged RESEARCH_DOSSIER §16.1.
**Status: IMPLEMENTED 2026-09-10 — A–E built, 673 + 10 tests green, ruff + mypy
clean. See `T7_TEST_REPORT.md`. Pending: soak confirmation (§7.10–7.11), then
merge.** The `wire_cooccurrence` / `decay_cooccurrence_edges` unit tests landed in
`tests/test_graph_memory.py` (not a separate `test_wire_cooccurrence.py`).

---

## 1 · Symptom

Every edge `weight` in `data/graph.json` is `0.0`. Confirmed again on the
post-B17 graph (2026-09-10 11:00):

```
nodes 67 · edges 74
weight histogram: {0.0: 74}          # all 74 edges
relations: {related_to: 61, part_of: 13}
nonzero edges: 0
```

The "fire together, wire together" co-occurrence reinforcement
(`GraphMemory.reinforce_cooccurrence`, RESEARCH_DOSSIER §F3 / §8) has not moved a
single weight in ~22 h + of soak. The graph is structure without strength: the
graph-window edge-opacity/width encoding is uniform, and `relevance_score`'s
non-weight terms are carrying the whole score.

The unit + integration tests for the mechanism **pass**
(`tests/integration/test_hebbian_plasticity.py`: "+0.01 on existing edges only,
one lock, ~1.4 ms"). So the primitive is correct and the bug is in how — or
whether — production drives it.

---

## 2 · Investigation

### 2.1 · Call graph

`reinforce_cooccurrence` has exactly **one** call site in `src/`:

```
plasticity.py:223   BitNetPlasticity._store_insight()
    episode = [*insight.cited_node_ids, *signal.related_node_ids]
    await self._graph.reinforce_cooccurrence(episode, _HEBBIAN_DELTA)
```

`_store_insight` runs only at the very end of `BitNetPlasticity._handle`, i.e.
only when **all** of these pass (plasticity.py:132-197):

1. `signal.confidence >= 0.7`
2. `signal.related_node_ids` non-empty
3. BitNet runtime not busy
4. Jaccard-novelty gate (< 0.8 vs every buffered signal)
5. model loads / backend available
6. ≥ 1 cited candidate still in the graph
7. model does **not** abstain and the grammar-constrained output validates

In the ~22 h soak that path completed **5 times** (5 `insight:` nodes in the
graph). So reinforcement was *attempted* 5 times across the entire graph in a
day.

### 2.2 · What `reinforce_cooccurrence` actually does

`graph_memory.py:185-198` → `_reinforce_edge_unsafe` (757-768):

```python
for u, v in ((node_a, node_b), (node_b, node_a)):
    if not self._graph.has_edge(u, v):
        continue                       # <-- creates NOTHING
    for key in list(self._graph[u][v]):
        data["weight"] += delta
```

It bumps **only edges that already exist** between two episode members. Its
docstring is explicit: *"Creates nothing — 'if the edge exists'."*

### 2.3 · What edges actually exist, and between which nodes

Every edge-creating path in the diagnosis/learning layers:

| Path | Edge it writes |
|---|---|
| `correlator._classify_into_graph` (APP_SWITCH) | `app:<x> --PART_OF--> domain:<d>` |
| `correlator._classify_webapp_into_graph` | `webapp:<w> --PART_OF--> app:<browser>` , `webapp:<w> --PART_OF--> domain:<d>` |
| `FocusSessionPattern` NodeSpec (patterns.py:411) | `app:<x> --PART_OF--> domain:<d>` |
| `_store_insight` (plasticity.py:221) | `insight:<i> --RELATED_TO--> <cited>` |
| `GraphMemory.link_orphan_nodes` (B6) | `<orphan> --RELATED_TO--> YOU` |
| DMN idle "imagination" | `<concept> --RELATED_TO--> <concept>` (once, at creation) |

**Every edge runs node → hub, or insight → node, or orphan → YOU.** No path
ever creates an edge *between two peer activity nodes* (`app` ↔ `app`,
`app` ↔ `webapp`, `webapp` ↔ `webapp`).

### 2.4 · What the episodes passed to `reinforce_cooccurrence` contain

`episode = insight.cited_node_ids + signal.related_node_ids`, and
`signal.related_node_ids` is built in `correlator._update_graph` as *just the
pattern's NodeSpec node ids* — **the edge targets (domains) are not appended**
(correlator.py:257-267). The cited ids are a subset of the related ids (the
model picks from `_context_nodes(signal)`, which resolves `related_node_ids`).

So per emitting pattern the episode is:

| Pattern | Episode shape |
|---|---|
| `FocusSessionPattern` | **1 node** (`app:<x>` or `webapp:<w>`) — zero pairs |
| `HighLoadPattern` / `IdlePattern` | 0-1 `app:` nodes |
| `DistractionPattern` | N `app:`/`webapp:` nodes — **all siblings under a domain, no edge between them** |
| `MemoryPressurePattern` / `HeavyAppStartedPattern` | N `app:` nodes from `_heavy_app_specs` — **no edges at all, not even to a domain** |

Cross the two facts: **the node-pairs in an episode are never the node-pairs
that have an edge.** `reinforce_cooccurrence` iterates the O(k²) pairs, every
`has_edge` check returns `False`, `bumped == 0`, every time. The 5 insight
episodes bumped nothing because there was nothing between their nodes to bump.

### 2.5 · Second defect (latent): edge re-add clobbers weight

`_add_edge_unsafe` (graph_memory.py:702-720):

```python
self._graph.add_edge(source_id, target_id, key=edge.relation,
                     weight=edge.weight, created_at=edge.created_at)
```

Verified against networkx `MultiDiGraph`:

```
after bump:    {'weight': 0.35, 'created_at': 't0'}
add_edge(u, v, key='part_of', weight=0.0, created_at='t1')   # same key
after re-add:  {'weight': 0.0,  'created_at': 't1'}           # CLOBBERED
```

`correlator._update_graph` calls `add_edge` **unconditionally on every pattern
fire** (correlator.py:268-271). `FocusSessionPattern` re-emits
`app --PART_OF--> domain` each time it fires. `_classify_*` guards the
APP_SWITCH edges with in-memory `_known_apps` / `_known_webapps` sets — but
those **reset to empty on every daemon restart**, so the first switch to each
app after a restart re-adds (and would zero) its `PART_OF` edge.

`DistractionPattern` and `FocusSessionPattern`'s webapp branch already carry
comments explaining they omit `edges` *specifically to avoid this reset* — so
the hazard is known, but only partially mitigated, and only where someone
remembered.

This defect is currently **masked** by 2.4 (nothing accumulates, so nothing is
lost). Any fix that makes weights accumulate on `PART_OF` edges immediately
un-masks it.

### 2.6 · Third factor: reinforcement is gated behind the LLM

Even with 2.4 and 2.5 fixed, driving Hebbian learning *only* from
`_store_insight` means ~5 reinforcement events per day across the whole graph —
statistically dead. Co-occurrence is a high-rate signal (every focus switch);
binding it to the low-rate, model-gated, novelty-throttled insight path throws
almost all of it away.

---

## 3 · Root cause (one sentence)

**"Wire together" was never implemented.** `reinforce_cooccurrence` only
*strengthens edges that already exist*, but nothing in the system ever *creates*
an edge between two nodes because they co-occurred — the correlator wires
activity nodes to domain hubs, never to each other — so the reinforcement
primitive operates on node sets with no internal edges and is a no-op by
construction. Two aggravating defects sit behind it: unconditional `add_edge`
re-adds reset `weight` to `0.0` (latent, currently masked), and reinforcement is
driven only from the rare model-gated insight path.

### Why the tests never caught it

`tests/integration/test_hebbian_plasticity.py:72-74` and `:97-98` **manually
create `RELATED_TO` edges between the cited nodes** before calling the
reinforcer, then assert those edges gained `+0.01`. That pre-wiring is exactly
what production never does. The test proves the primitive; it assumes the caller
hands it a connected set, which no real caller does.

---

## 4 · Fix — design

Four parts. A and B are mechanism, C is the driver, D keeps the graph sparse.
E/F are config + docs.

### 4A · Clobber-proof edges (fixes 2.5)

`graph_memory.py` — `_add_edge_unsafe` becomes a true upsert:

- if `(source, target, relation)` already exists: **do not touch `weight`**;
  keep the earliest `created_at`. Return the existing edge.
- `weight` argument applies **only on creation**.
- new private `_bump_or_create_edge_unsafe(u, v, relation, *, delta, base)`:
  create with `weight=base` if absent, else `weight += delta`. Returns
  `("created" | "bumped")`.

This is a standalone correctness fix — an edge's accumulated strength must
survive a structural re-assertion of that edge. Ship it even if nothing else
lands.

### 4B · `GraphMemory.wire_cooccurrence()` — the missing "wire together" (fixes 2.4)

New public coroutine, one `_lock` cycle, same shape as `reinforce_cooccurrence`:

```python
async def wire_cooccurrence(
    self,
    node_ids: Sequence[str],
    *,
    delta: float,
    base: float,
    max_episode: int = 8,
    max_new_edges: int = 12,
) -> tuple[int, int]:          # (created, bumped)
```

- de-dupe, drop ids not in the graph, truncate to `max_episode` (keep the first
  N — callers pass most-relevant-first).
- for each unordered pair: if **any** edge exists between them (either
  direction, any relation) → bump every such edge by `delta`; else create one
  `app/webapp`-appropriate `RELATED_TO` edge with `weight=base`, up to
  `max_new_edges` new edges per call.
- only pairs where **both** ids are activity nodes (`app:` / `webapp:`) are
  eligible for *creation* — bumping an existing edge is unrestricted. Keeps
  `file:` / `concept:` noise out of the co-occurrence mesh.
- idempotent in the sense `reinforce_*` is: re-running bumps, never
  double-creates.

`reinforce_cooccurrence` stays as the pure bump-only primitive (other callers,
tests). `wire_cooccurrence` is `reinforce_cooccurrence` + create.

### 4C · Drive it continuously from `APP_SWITCH` (fixes 2.6)

The correlator already consumes `APP_SWITCH`, owns the canonical `app:` /
`webapp:` ids, and upserts both nodes before returning (correlator.py:146-150).
Add a bounded **co-activation window** there — no new module, ~20 lines:

```python
# SignalCorrelator.__init__
self._coactive: deque[tuple[str, float]] = deque(maxlen=config.coactivation_max_nodes)

# on_app_switch, after _classify_* (both nodes now exist):
focus_id = f"webapp:{webapp}" if webapp else self._canon_app_id(app_id)[0]
now = time.monotonic()
warm = [nid for nid, ts in self._coactive
        if nid != focus_id and now - ts <= config.coactivation_window_seconds]
if warm:
    await self._graph.wire_cooccurrence(
        [focus_id, *warm], delta=config.hebbian_delta, base=config.hebbian_base)
self._coactive.appendleft((focus_id, now))
```

Every app/tab switch now reinforces the new focus against everything focused in
the last `coactivation_window_seconds` (default 300 s). This is the real "fire
together, wire together" for a workspace graph: apps used in the same work
session accrue weight on the edge between them; apps never used together never
get an edge.

Also: in `_store_insight`, swap `reinforce_cooccurrence` → `wire_cooccurrence`
(model-confirmed co-occurrence is *stronger* evidence — pass
`delta = config.hebbian_delta * config.hebbian_insight_multiplier`). One-line
change; the insight path now contributes instead of no-op-ing.

### 4D · Decay + prune so weights stay meaningful (new, prevents saturation)

Without decay, `wire_cooccurrence` only ever adds — after months every
long-lived pair saturates and "which pairs matter" is flat again, just at a
higher number. Add "use it or lose it" to the B6 idle sweep:

`GraphMemory.decay_cooccurrence_edges(factor: float, floor: float) -> int`

- multiply `weight` of every `RELATED_TO` edge whose **weight > 0** by `factor`
  (default 0.9 per idle cycle).
- delete any `RELATED_TO` edge that (a) is now `< floor` (default 0.02) **and**
  (b) is not the node's only edge (never re-orphan a node — `link_orphan_nodes`
  runs right after and would just re-add a `→ YOU` edge).
- called from `DMN._reminiscence` between `consolidate()` and
  `link_orphan_nodes()`.

Net effect: an edge reinforced regularly climbs and stays; an edge from a
one-off co-activation fades below the floor within a few idle cycles and is
pruned. Weight becomes a recency-weighted affinity, which is what the graph
window's opacity encoding has always claimed to show.

### 4E · Config (`core/config.py`)

Promote the module constant and add the window/decay knobs, all operator-tunable:

| key | default | meaning |
|---|---|---|
| `hebbian_delta` | `0.01` | per-co-occurrence bump (was `plasticity._HEBBIAN_DELTA`) |
| `hebbian_base` | `0.05` | initial weight of a newly wired co-occurrence edge |
| `hebbian_insight_multiplier` | `3.0` | model-confirmed episodes bump harder |
| `coactivation_window_seconds` | `300` | two focuses within this are "co-active" |
| `coactivation_max_nodes` | `16` | co-activation deque cap (bounds O(k²)) |
| `hebbian_decay_factor` | `0.9` | idle-cycle multiplier on co-occurrence weights |
| `hebbian_floor` | `0.02` | prune a decayed co-occurrence edge below this |

### 4F · No backfill

`data/graph.json` is gitignored and weights recover within ~2-3 days of soak
once 4C is live. A one-time replay of `data/actions.jsonl` was considered and
rejected as effort with a short shelf life (§5.6). The orchestrator does **not**
get a T7 load-time pass.

---

## 5 · Alternatives considered and rejected

| # | Alternative | Why rejected |
|---|---|---|
| 5.1 | **Fix only the clobber (2.5), leave the rest.** | Doesn't touch the root cause. Nothing creates peer edges, so `has_edge` still `False`, weights still `0.0`. Necessary, not sufficient. |
| 5.2 | **Append domain hub ids to `signal.related_node_ids`** so the existing `app --PART_OF--> domain` edges get bumped. | Reinforces the wrong thing. Every app in a domain would trend upward together → `weight ≈ f(access_count)`, no discriminative value. Hub edges are not "which pairs matter". |
| 5.3 | **Keep driving Hebbian only from `_store_insight`, just fix the wiring.** | ~5 events/day across the whole graph (2.6). Even correct, it measures nothing. The novelty gate (Jaccard > 0.8) specifically *suppresses* recurring co-occurrence — the opposite of what Hebbian learning wants. |
| 5.4 | **New `HebbianReinforcer` `BaseModule` subscribed to `APP_SWITCH`.** | Correct but heavier than warranted. The correlator already consumes `APP_SWITCH`, already owns canonical `app:`/`webapp:` ids and the upsert ordering. A deque there is ~20 lines vs a new module with lifecycle + health + wiring + its own test file. Revisit only if co-occurrence sourcing grows past focus events (e.g. also files, also agents). |
| 5.5 | **New `CO_OCCURS_WITH` relation** instead of reusing `RELATED_TO`. | Cleaner semantics and the graph window could style it distinctly. But: new `RelationType` enum value, touches serialisation round-trip tests, the edge-encoding in two graph scripts, and `relevance_score`'s relation handling. For v1, `RELATED_TO` + `weight` as the carrier matches the dossier's existing description ("a bright line is a Hebbian-reinforced pair"). Logged as a future refinement. |
| 5.6 | **Backfill weights by replaying `data/actions.jsonl` / `raw_metrics.csv`.** | The graph file is gitignored and disposable; 4C repopulates within days. A replay script is real code + tests for a one-session benefit. |
| 5.7 | **Reinforce on `SIGNAL_CORRELATED` in the correlator** (wire every signal's `related_node_ids`) instead of a time window on `APP_SWITCH`. | Kept as a cheap secondary (it's a subset of 4C's value) but not the primary: `FocusSessionPattern` episodes are single-node, `HighLoad`/`Idle` are 0-1 node, so most signals still contribute nothing. The `APP_SWITCH` window captures co-occurrence the patterns never surface. |

---

## 6 · Files

```
EDIT  src/neuropaca/core/graph_memory.py
        _add_edge_unsafe            -> upsert, never reset weight/created_at
        _bump_or_create_edge_unsafe (new)
        wire_cooccurrence           (new, public)
        decay_cooccurrence_edges    (new, public)
EDIT  src/neuropaca/core/config.py           7 new keys (§4E)
EDIT  src/neuropaca/diagnosis/correlator.py  co-activation deque in on_app_switch
EDIT  src/neuropaca/learning/plasticity.py   reinforce_cooccurrence -> wire_cooccurrence;
                                             _HEBBIAN_DELTA -> config
EDIT  src/neuropaca/idle/dmn.py              call decay_cooccurrence_edges in _reminiscence
EDIT  tests/integration/test_hebbian_plasticity.py
        stop pre-wiring the cited nodes; assert wire_cooccurrence CREATES then bumps
NEW   tests/test_wire_cooccurrence.py        create/bump/cap/idempotent/activity-only
NEW   tests/test_cooccurrence_window.py      correlator: two switches in window -> edge;
                                             outside window -> none; deque cap
EDIT  tests/test_graph_memory.py             _add_edge_unsafe no longer clobbers;
                                             decay_cooccurrence_edges: decays, prunes,
                                             never re-orphans
EDIT  tests/test_diagnosis_*.py              on_app_switch wiring wired in fixtures
EDIT  tests/test_dmn*.py / test_idle*.py     _reminiscence calls decay
EDIT  phases.md · RESEARCH_DOSSIER.md (§8, §16.1 T7 -> resolved, §5 record)
NEW   T7_TEST_REPORT.md
```

No schema change (weight already serialises). No new dependency. No systemd
change.

## 7 · Verification

1. **`_add_edge_unsafe` upsert:** add edge, bump weight to 0.35, re-add same
   `(u,v,relation)` with `weight=0.0` → weight still 0.35, `created_at`
   unchanged.
2. **`wire_cooccurrence` create:** episode of 3 `app:` nodes with no edges →
   3 `RELATED_TO` edges at `weight=base`, `(created, bumped) == (3, 0)`.
   Re-run → `(0, 3)`, weights at `base+delta`.
3. **`wire_cooccurrence` caps:** episode of 20 → truncated to `max_episode`;
   never more than `max_new_edges` created per call.
4. **`wire_cooccurrence` activity-only:** `[app:x, concept:y]` → no edge
   created; an existing `app:x--concept:y` edge is still bumped.
5. **co-activation window (correlator):** switch A, switch B 60 s later →
   `app:A--RELATED_TO--app:B` exists, `weight == base`. Switch to A again
   400 s later → no bump (B fell out of the 300 s window). Deque never exceeds
   `coactivation_max_nodes`.
6. **insight path:** `_store_insight` with a real connected episode →
   `wire_cooccurrence` bumps at `delta * insight_multiplier`; still one lock
   cycle; still < 50 ms loop lag on the 10k fixture (existing integration test).
7. **decay:** graph with `RELATED_TO` weights `{0.5, 0.03, 0.10}` →
   `decay_cooccurrence_edges(0.9, 0.02)` → `{0.45, 0.027, 0.09}`, nothing
   pruned; run again → the 0.027 edge crosses `0.02`? no (0.0243) ... third run
   → pruned, **unless** it is its node's only edge.
8. **never re-orphan:** a node whose single edge decays below floor keeps it.
9. Full suite + `ruff check .` + `mypy` green. Graph load + a
   `wire_cooccurrence(8 nodes)` inside B1's 2 s / 10k budget.
10. **Live:** daemon on this branch → 30-45 min of normal multi-app use →
    `python -c "...weight histogram"` on `data/graph.json` shows a **spread**
    of non-zero `RELATED_TO` weights between the apps actually used together,
    `PART_OF` hub edges untouched by decay. Leave one idle cycle → transient
    single-co-activation edges gone, session pairs retained. Graph window: edge
    opacity now varies.
11. **Soak:** fold into the running soak; add a `weight_nonzero_fraction` +
    `max_cooccurrence_weight` probe to `scripts/soak_probe.py` so B9/B15-class
    dashboards track it. T7 closes when a ≥ 24 h soak shows a stable non-zero
    weight distribution that decay keeps bounded.

## 8 · Risks / open

- **Edge-count growth.** `wire_cooccurrence` adds edges. Mitigated by:
  activity-only creation, `max_new_edges` per call, `coactivation_max_nodes`
  deque cap, and 4D decay+prune. The soak probe (7.11) is the guard — if edge
  count trends up unbounded, drop `coactivation_window_seconds` or raise
  `hebbian_floor`.
- **Default constants are guesses.** `base=0.05`, `delta=0.01`, `decay=0.9`,
  `window=300 s` are first estimates. All in `Config`; tune from the first
  soak's weight distribution. Document like `webapp_map`.
- **Restart still re-adds `PART_OF` edges** (empty `_known_apps`). 4A makes that
  harmless for `weight`, but `created_at` on the *first* creation is what we
  keep — a genuine re-add after a delete would keep the stale timestamp. Edge
  case, acceptable; note it.
- **`CO_OCCURS_WITH` relation** (5.5) left for a future phase — if the graph
  window wants to draw co-occurrence distinctly from semantic `RELATED_TO`.
- **`relevance_score` interaction.** Non-zero weights will shift scores for the
  first time. Check `_recalculate_chunk_unsafe` / `_bridge_value_unsafe` don't
  over-weight a now-dense co-occurrence mesh; re-baseline the score
  distribution in the test report.
