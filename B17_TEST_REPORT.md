# B17 · Test report — one app, one node

Branch `b17-app-identity-canonicalization`. Plan: [`B17_PLAN.md`](B17_PLAN.md).

---

## 1 · Unit suite

`.venv/bin/python -m pytest -q` → **643 passed, 3 skipped** (env-gated, unchanged).
`ruff check .` clean (bare, as CI runs it). `mypy` clean — *73 source files*.

New / changed:

| test file | what it pins |
|---|---|
| `test_app_identity.py` (37) | alias collapses Wayland id + process name to one slug; normaliser (`Brave-Browser`→`brave`, `foot`→`foot`); unknown passes through; `is_non_app` (thread labels, shells); `pretty`; missing-file degrades to normalise-only |
| `test_graph_memory.py` (+4) | `canonicalise_app_nodes`: folds the two schemes (ac summed, `webapp` edge rewired, `ram_mb` kept); renames a lone focus node; drops every `is_non_app` node + its edges; never touches hubs; idempotent `(0, 0)` |
| `test_graph_window.py` (8) | `pretty_label` table; the 11 hubs on a deterministic `HUB_RING` ring, first at 12 o'clock; `pin_hubs` fixes + pins every master node and overrides drift; normal nodes still float |
| `test_graph_cleanup.py` (6) | fold + edge rewire + resource keep; drops edgeless non-hub, spares hubs; idempotent; `--dry-run` writes nothing; `--apply` writes valid JSON |
| `test_b13_process_collector.py` (+1) | `psutil` rows named `MainThread` / `Thread-7 (worker)` are dropped; `zed` kept |
| `test_webapp_pipeline` / `test_b2_5_recorded_fixtures` / `test_b13_pipeline` | assertions updated to canonical ids (`app:brave`, `app:zed`, `app:cosmic-term`) |

## 2 · `canonicalise_app_nodes` on a copy of the real soak graph

`cp data/graph.json /tmp && scripts/graph_cleanup.py --graph /tmp/…` (same fold
logic the orchestrator runs):

```
drop   app:MainThread  (not an application)
rename app:com.system76.CosmicTerm    -> app:cosmic-term
merge  app:brave-browser              -> app:brave
rename app:com.system76.CosmicMonitor -> app:cosmic-monitor
merge  app:com.system76.CosmicFiles   -> app:cosmic-files
merge  app:com.anthropic.Claude       -> app:claude
merge  app:claude-desktop             -> app:claude
merge  app:md.obsidian.Obsidian       -> app:obsidian
rename app:neuropaca_graph_window.py  -> app:neuropaca-graph-window-py
rename app:sublime_text               -> app:sublime-text

nodes 63 -> 57   edges 61 -> 58
```

- 24 → 19 `app:`/`webapp:` nodes; **0 dangling edges** after.
- The merged **Brave** node carries `ram_mb ≈ 3811` (from the census node) **and**
  its focus `access_count` **and** all four `webapp:` children.
- Claude's three nodes → one.
- Second pass: `nodes 57 -> 57`, *"nothing to clean"* — idempotent.

## 3 · Live daemon on the target box

1. `systemctl --user stop neuropacad` → `cp data/graph.json data/graph.json.pre-b17-backup`
   → `scripts/graph_cleanup.py --apply` (63 → 57 nodes) → `systemctl --user start neuropacad`.
2. Daemon boots healthy: `graph_nodes 57 · graph_edges 58` — **exactly** the
   cleaned file, and **no** `B17 app-identity pass: merged …` log line: the
   orchestrator's canonical pass found `(0, 0)`, confirming `graph_cleanup.py`
   and `GraphMemory.canonicalise_app_nodes` agree.
3. `data/graph.json` after further running: **15 `app:` nodes, all canonical** —
   no `com.*`, no underscores, no uppercase; no duplicate reappeared.

Graph window (`scripts/neuropaca_graph_window.py`) — verified visually:
- the 10 domain hubs sit evenly on a fixed ring around "You", labelled in plain
  English ("Mental Models", "Engineering", …); they do not drift or reshuffle on
  reload and cannot be dragged.
- clicking any node opens the right-hand panel with its pretty name, raw id,
  type, relevance, focus count, RAM/CPU where present, and a click-through
  "Connected to" list; clicking empty space or `Esc` closes it.

## 4 · Exit criteria (`B17_PLAN.md §6`)

| # | criterion | status |
|---|---|---|
| 1 | `AppIdentity` resolver — alias, normaliser, passthrough, `is_non_app`, `pretty` | ✅ 37 tests |
| 2 | `canonicalise_app_nodes` on a hand-built graph — fold / rename / drop / hub-safe / idempotent | ✅ 4 tests |
| 3 | on the real soak graph — 24 → 19, resource+focus attrs preserved, 0 dangling, idempotent | ✅ §2 |
| 4 | one focus + one census snapshot for the same app → one node | ✅ `test_webapp_pipeline`, `test_b13_pipeline` |
| 5 | `ProcessCollector` drops thread-label rows | ✅ 1 test |
| 6 | graph window — `pretty_label`, deterministic hub ring, pinned | ✅ 8 tests + live |
| 7 | `graph_cleanup.py` dry-run / apply | ✅ 6 tests + live |
| 8 | full suite + `ruff` + `mypy` green; load in budget | ✅ (643 pass; the canonical pass is O(n), a no-op after the first boot) |
| 9 | live: one node per touched app, no `MainThread`/tooling, no new dup; graph window structure | ✅ §3 |

## 5 · Deferred / notes

- **Hebbian edge weights are all `0.0`** in the graph — reinforcement is not
  accumulating. Found in the same review; **out of B17 scope**. Its own fix.
- `app:neuropacad` / `app:python3` / `app:chrome-devtools-mcp` / `app:cosmic-comp`
  survive the clean (they have census edges, are not `is_non_app`). They are now
  in `process_exclude_names` so the census adds nothing further; their edges will
  decay. A future pass could fold `process_exclude_names` into the cleanup drop.
- `scripts/_provenance.py` re-run at merge (needs `PROV_SECRET`).
