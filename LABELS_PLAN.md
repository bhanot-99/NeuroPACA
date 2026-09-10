# B18 · One labeling system — labels are rendered, never stored

Status: BUILT (steps 1-7; step 8 metrics via the high-level check, see
phases.md B18; step 9 alias adjudicator not built) · 2026-09-10 · follows B17
(app identity) and T7 (Hebbian)

## 0. In plain words

Right now every generated node (insight, idle thought, L8 probe) gets a
sentence glued onto it the moment it is created — e.g.
`"anomaly: idle implicates app:brave-browser"`. That sentence is frozen. When
B17 later renamed `brave-browser` to `brave`, the node kept the old sentence.
When the same fact happened again after a restart, nothing recognised the
sentence as "already known", so a new node was made. And the graph window,
unable to read the frozen sentences sensibly, shortened every probe to
"Summary" or "Learning", so distinct nodes look identical.

The fix is one idea applied everywhere:

> **A node stores *what it is about* (structured facts that point at other
> nodes by id). Its readable name is computed from those facts, on demand,
> by one function. Its identity — "have I seen this already?" — is computed
> from the same facts, by one other function.**

Names then heal themselves after renames (issue 1), repeats are recognised
across restarts because the check lives in the saved graph (issue 2), and every
view shows the part that makes a node different (issue 3).

## 1. What the live graph shows (data/graph.json, 2026-09-10 13:27)

90 nodes; 55 are generated (`insight:` 8, `idle:` 15, `ephemeral:` 32).
Re-expressing those 55 as structured facts with B17 canonical names gives
**25 distinct facts** — 55 % of the generated nodes are redundant.

| Issue | Evidence | Root cause (code) |
|---|---|---|
| 1 · stale names | `insight` about `app:brave-browser` **and** `app:brave`; `app:com.system76.CosmicTerm` **and** `app:cosmic-term` | `Insight.summary` bakes the raw id into text (`learning/insight.py:67`); `supervisor._grow_subcluster` does the same (`agents/supervisor.py:259-261`). B17's `canonicalise_app_nodes` rewires edges but cannot rewrite text. |
| 2 · duplicate insights | `"anomaly: idle implicates app:brave"` ×2, same for cosmic-term | The novelty gate compares **node-id sets** in an in-memory `deque` (`plasticity.py:69, 199-206`) — empty after every restart. The existing text merge (`graph_memory._find_duplicate_pairs_unsafe`) only runs inside a completed DMN idle cycle and only matches identical text, so it is late and misses renamed variants. |
| 3 · lookalike captions | every `ephemeral:summary:*` → "Summary", every `ephemeral:source-learning:*` → "Learning" | `pretty_label` (`scripts/neuropaca_graph_window.py:124-126`) can only read the id, because the label text is unparseable prose. |
| **4 · labels copy labels (new)** | `ephemeral:summary` label = `"pressure 1.59 on …: L4 anomaly: anomaly: idle implicates …"` (insight text nested inside); idle thought `"How does learning corroborated app:com.system76.CosmicTerm affect pressure 1.30 on …?"` | `pressure.py:238` copies `insight.summary` into the reason; DMN seeds exclude only `INSIGHT`/`IDLE_THOUGHT` types (`dmn.py:57`), so `ephemeral:` probes (type `CONCEPT`) become thought seeds and their text is quoted verbatim. |

Issue 4 is why stale names *spread*: one frozen sentence is copied into two or
three more frozen sentences.

## 2. Research — what others do and what we take

| Source | Idea | Use here |
|---|---|---|
| Zep / Graphiti (Rasmussen et al., 2025, arXiv 2501.13956) | Episodic nodes keep raw input; entity nodes are deduplicated abstractions. Dedup = cheap candidate retrieval (embedding + full-text) → LLM adjudicates `is_duplicate` and emits a canonical name. Edge dedup is restricted to candidates between the **same entity pair**. | Our generated facts are already structured, so the "same pair" restriction becomes an exact key — no retrieval or LLM needed for facts. The retrieve→adjudicate pattern is kept for the only open-ended case: unknown app aliases (§6). |
| A-MEM (Xu et al., NeurIPS 2025, arXiv 2502.12110) | Each memory is a note with structured attributes (keywords, tags, context, links) that later memories update in place instead of duplicating. | "Update in place" → a repeat reinforces the existing node (count, last seen) instead of creating one. |
| Idempotency keys (event-driven systems practice) | A key derived deterministically from content makes a write safe to repeat. | The fact fingerprint *is* an idempotency key; the upsert is idempotent across restarts. |
| SemHash-LLM (2026, arXiv 2607.01601); embedding dedup with persistent index | Semantic hashing / embeddings for free-text near-duplicates, thresholds ~0.7-0.85. | **Rejected** for facts: our text is template output — exact structured keys are zero-cost and zero-error, embeddings would add a model and a threshold to tune. |
| Grammar-constrained decoding for small models (ACL Industry 2025; arXiv 2605.02363) | Grammar constraints make small models reliably parseable, and can stand in for few-shot examples. | If a model is used (§6), it answers a closed `same \| different \| unsure` grammar only — consistent with D-11 (no model free text). |

## 3. The design

### 3.1 One record per node: `LabelSpec`

Added to `Node` as a persisted field (graph **schema v5**). This is the one
schema change, and it is necessary: `GraphMemory` drops ad-hoc attributes (see
memory *graph-drops-adhoc-node-attributes*), so an id-prefix hack cannot carry
subject references.

```python
@dataclass(frozen=True, slots=True)
class LabelSpec:
    kind: LabelKind            # closed enum, see table
    refs: tuple[str, ...] = () # node ids this node is ABOUT (never text)
    facet: str = ""            # closed vocabulary per kind
    value: float | None = None # the one number worth showing (pressure, confidence)
    name: str = ""             # only for leaf kinds that name themselves (app slug, domain)
```

| kind | refs | facet | value | today's node |
|---|---|---|---|---|
| `root` | – | – | – | `YOU` |
| `domain` | – | – | – | `domain:*` (`name` = slug) |
| `app` / `webapp` | – | – | – | `app:*` (`name` = canonical slug) |
| `insight` | `(cited,)` | `category/signal` e.g. `anomaly/idle` | confidence | `insight:*` |
| `thought` | `(a, b)` | `affects` | – | `idle:*` |
| `probe` | `(trigger,)` | `summary/L4.anomaly`, `source/learning`, `source/diagnosis` | pressure | `ephemeral:*` |
| `file`, `session`, … | – | – | – | leaf kinds keep `name` |

Rule: **a spec may reference other nodes only through `refs`.** Text never
flows from one node's label into another's. That closes issue 4 by
construction.

### 3.2 Three pure functions, one module (`core/labels.py`)

```python
def fingerprint(spec: LabelSpec, canon: Callable[[str], str]) -> str
def render(spec: LabelSpec, lookup: Callable[[str], Node | None], mode: Literal["short", "full"]) -> str
def disambiguate(captions: Mapping[str, str], nodes: Mapping[str, Node]) -> dict[str, str]
```

- **`fingerprint`** = `kind | facet | refs-canonicalised` (refs sorted for
  symmetric kinds such as `thought`; `value` excluded, so pressure 1.43 and 1.59
  on Brave are one probe). `blake2b(…, digest_size=8)` hex. Deterministic, so it
  can be recomputed from the saved graph at load.
- **`render`** resolves each ref to the ref node's *current* rendered name —
  so a later rename changes every label that points at it, with no rewrite
  step. Templates are a table, not scattered f-strings:

  | kind | short (graph caption, ≤ 28 chars) | full (panel, terminal) |
  |---|---|---|
  | insight | `Brave · anomaly` | `Anomaly on Brave while idle · confidence 0.82 · seen 3×` |
  | probe summary | `Brave · pressure 1.6` | `Pressure 1.59 on Brave, from an anomaly insight` |
  | probe source | `Brave · via learning` | `Learning corroborated the pressure on Brave` |
  | thought | `Brave ↔ Cosmic Term` | `How does Brave affect Cosmic Term?` |

- **`disambiguate`** runs over the captions actually on screen: only where two
  are still equal, append the smallest field that tells them apart (value, then
  relative time `· 2h ago`). Once repeats are deduplicated (below), equal
  captions become rare. This is the same approach editors use for two open
  tabs that share a filename.

`label` stays on `Node` as a **cache** of `render(spec, full)` so old readers,
`search_by_label`, and `context.py` keep working untouched. It is refreshed
on save and after `canonicalise_app_nodes`, never read as the source of truth.

### 3.3 One write path: `GraphMemory.upsert_fact(spec, node_type, id_prefix)`

Replaces the three `f"{prefix}{uuid}"` + `{"label": text}` call sites
(plasticity, DMN, supervisor). Under one `_lock` cycle:

1. `fp = fingerprint(spec)`; look it up in `_fp_index: dict[str, str]`
   (fingerprint → node id), which is **rebuilt from the graph at load**, so it
   survives restarts.
2. Hit → **reinforce**: `access_count += 1`, `last_accessed = now`, update
   `value`; return `(node_id, created=False)`.
3. Miss → create `f"{id_prefix}{fp[:12]}"` (the id is derived from the
   fingerprint, so even a lost index cannot produce a second node), add edges to
   `refs`, index it; return `(node_id, created=True)`.

The id prefixes (`insight:`, `idle:`, `ephemeral:`) stay — apoptosis, TTL
pruning and L9 surfacing all key on them (memory *graph-drops-adhoc-node-attributes*).

### 3.4 What each issue becomes

- **Issue 1** — labels are rendered through refs, and refs go through
  `AppIdentity.resolve`. `canonicalise_app_nodes` already rewires refs-as-edges.
  It now also rewrites `spec.refs` and merges any two facts whose fingerprints
  now collide.
- **Issue 2** — two gates, both graph-backed:
  - *before inference* (saves the model call): skip if a fact with the
    same `(signal_type, top cited candidate)` was reinforced within
    `insight_refractory_minutes` (default 360). Queried from `_fp_index`, so
    it's restart-proof.
  - *after inference*: `upsert_fact`. A repeat raises the insight's
    `seen` count rather than making a node, and `INSIGHT_GENERATED` is published
    only when `created=True` (L5 pressure and L9 surfacing stop double-counting).
  - The in-memory Jaccard `deque` is **deleted**. It was the only non-durable
    novelty state, and the fingerprint subsumes it.
- **Issue 3** — `pretty_label` shrinks to `render(spec, "short")` +
  `disambiguate`. The window's per-prefix special cases go away.
- **Issue 4** — `pressure.py` stores `ref=insight.node_id` and a short reason
  code, not `insight.summary`. The DMN excludes `ephemeral:` from seeds (seeds
  should be real things the user touches, not the system's own probes).

## 4. Migration of existing data (one-time, schema v4 → v5)

`_migrate_v4_to_v5` in `graph_memory` (runs on load, backup written first as
`graph.json.pre-b18-backup`):

1. Parse each legacy generated label with the four known templates (the same
   regexes validated against the live graph in §1: 55/55 parsed).
2. Canonicalise refs through `AppIdentity.resolve`.
3. Derived specs that quote another node's text (the two issue-4 thoughts)
   cannot be expressed as refs → **delete** them. They are idle thoughts past
   usefulness anyway and would be pruned at 48 h.
4. Fingerprint-merge with the existing `_merge_nodes_unsafe` (keeps the older
   `created_at`, sums counts).
5. Any label that matches no template → `LabelSpec(kind=…, name=label)`
   (rendered verbatim; nothing is lost).

Expected on today's graph: 55 → ~23 generated nodes, 90 → ~58 total.

## 5. Build order (each step green on bare `ruff check .` + mypy + pytest)

1. `core/labels.py` — `LabelSpec`, `LabelKind`, `fingerprint`, `render`,
   `disambiguate`. Pure; property tests (fingerprint stable under ref order for
   symmetric kinds, under rename via `canon`; `render` never emits a raw
   `app:` id; `disambiguate` output is injective on its input).
2. `Node.spec` + schema v5 read/write + v4→v5 migration; golden test on a copy
   of `data/graph.json.pre-b17-backup` and today's graph (55 → 25 facts before
   the issue-4 deletions).
3. `GraphMemory.upsert_fact` + `_fp_index` (rebuilt in `load`, maintained on
   delete/merge/relabel). Concurrency test: 50 concurrent identical upserts →
   1 node, `access_count == 50`.
4. Switch the three writers: plasticity (and delete the Jaccard buffer), DMN
   (`thought` spec + `ephemeral:` seed exclusion), supervisor (`probe` specs).
   Regression test for issue 2: generate insight → restart GraphMemory from
   disk → same signal → no new node, `seen == 2`, model not called.
5. `pressure.py` reason carries a ref, not text.
6. `canonicalise_app_nodes` rewrites `spec.refs` and re-merges collisions.
7. Graph window + panel + `context.py` + L9 surfacing read `render(...)`.
   Panel shows `full` + seen count + first/last seen.
8. `scripts/soak_probe.py --labels`: report `generated_nodes / distinct
   fingerprints` (target 1.0) and `captions_colliding_after_disambiguate`
   (target 0). Paper metrics.

## 6. Where an AI model helps — and where it must not

**Not in labels.** A label must be reproducible from the saved graph and
traceable to evidence (D-11: no model free text; BitNet cannot write grounded
sentences, problems.md 1.13). Letting Qwen write captions would bring back
issue 1: a model-written sentence is frozen text again.

**Yes, for one open-ended question: "are these two app ids the same app?"**
Rule tables can't enumerate every alias. The live graph already has
`app:python` / `app:python3`, and scripts appearing as apps
(`app:verify-old-py`, `app:neuropaca-graph-window-py`). Optional step 9,
behind `label_alias_adjudication = false` by default:

- *Candidates (no model)*: new `app:` ids whose normalised slug has token-set
  Jaccard ≥ 0.5 or a shared prefix with an existing app, and that
  co-occurred with it in the same process census. This is Graphiti's cheap
  retrieval step, without embeddings.
- *Adjudicate (Qwen2.5-3B, the interactive model that sits idle)*: one
  grammar-constrained call, output `same | different | unsure`, run only
  inside a DMN idle cycle, budget 1 call per cycle, under the single
  `_inference_lock` (rules.md §4). Context: both slugs, exe paths, window
  app_ids, and co-occurrence counts — no free text.
- *Effect*: `same` writes a **suggested** alias to
  `data/app_identity.suggested.toml`; it takes effect only after the user
  runs `neuropaca identity accept`. No silent merges.
- *Paper ablation*: alias precision/recall vs. a hand-labelled set of every
  app id seen in the soak — rules-only vs. rules + adjudicator.

If the ablation shows no gain over rules alone, step 9 is dropped and
recorded as a rejected alternative.

## 7. Rejected alternatives (paper record)

| Alternative | Why rejected |
|---|---|
| Persist the Jaccard buffer to disk | Patches one symptom (restart), and adds a second source of truth next to the graph. It still compares node sets, not facts, so renamed variants still slip through. |
| Rewrite old label text after B17 renames | Treats the symptom. Every future rename needs another rewrite pass, and nested copies (issue 4) are unreachable. |
| Embedding / MinHash / SemHash near-duplicate detection | Our facts are structured, so exact keys are free and exact. Embeddings would add a model, a threshold and false merges. |
| LLM-written node names | Breaks D-11 grounding and reproducibility, and brings back frozen text. |
| Encode facets in the id and parse ids for display | What `pretty_label` does today. It can't carry refs, so it can't render names or survive renames. |
| Random uuid ids + index-only dedup | A lost or corrupt index silently produces duplicates. Fingerprint-derived ids make duplicates impossible by construction. |

## 8. Risks

- **Schema v5 is a one-way door** for older builds. Mitigation: backup file +
  `_MIN_READABLE_SCHEMA_VERSION` stays 1 (v4 files still load and migrate).
- **Reinforce-instead-of-create changes L5 dynamics** (fewer
  `INSIGHT_GENERATED` events means less pressure). This is intended, since
  duplicates were inflating pressure, but re-check the B7 positive control
  after step 4.
- **Refractory window hides a real recurrence**. The recurrence still
  reinforces (count, last seen); only the model call and the new node are
  skipped.
