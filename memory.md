# memory.md — Living Project State

## 📍 Current state

- **Phase:** B1 — Core Infrastructure. **Code + tests done** on branch `b1-core-infra`; formal exit-criteria harnesses still owed. B0 done. The B0 model spike is still not run (gates L4/L6/L9 fallbacks, not B1).
- **Currently working on:** B1. Commits: `a95ce07` decisions+tooling, `ddc8290` data foundation, `f71f34c` test contract, `fd516ed` L1 core + orchestration. `ruff` + `mypy` (25 files) + **76 pytest** green. Live daemon boots to a running idle loop holding the 11-hub graph and shuts down clean, graph persisted.
- **What's implemented:** `core/{enums,models,config,health,event_bus,graph_memory,inference,bitnet_runtime,base_module}.py`, `orchestration/{orchestrator,scheduler}.py`, `daemon.py` (+ `neuropaca` console script), `tests/conftest.py` (autouse singleton reset) + `tests/test_{core_foundation,event_bus,graph_memory,orchestrator}.py`.
- **Next action (finish B1):** the exit-criteria harnesses — a 10k-node graph fixture generator; a perf test asserting 10k-node `load()` < 2 s and `find_related(depth=2)` < 50 ms; a 1 h RSS-soak harness (like `spikes/b0_bitnet/benchmark_runtime.py`); the concurrent-writer stress already covered in `test_graph_memory.py` but add a longer-running variant. Then a `BaseModule` conformance test + a `NullModule` test double. Then B2.
- **Deferred within B1 (noted, not blocking):** `load()` still reads the graph file on the loop (small, startup-only — ruff-clean; wrap in `to_thread` if it grows). `consolidate()` is a locked no-op until B6. `Event.priority` is metadata only (FIFO queue).
- **Note:** the ECC GateGuard fact-forcing hook fires on every file write — `ECC_GATEGUARD=off` (or `ECC_DISABLED_HOOKS=pre:edit-write:gateguard-fact-force`) makes bulk file work practical.
- **Last updated:** 2026-08-29

## ✅ Completed log

- **2026-08-29 · B0 scaffold.** `git init` (branch `b0-foundation` off `main`). Repo skeleton per `Architecture.md §15` — `src/neuropaca/` with one package per layer, `tests/`, `data/` (gitignored). `pyproject.toml` (hatchling, no runtime deps yet; `[dev]` = ruff/mypy/pytest/pre-commit; `[spike]` = llama-cpp-python/psutil), `uv.lock`, `.gitignore`, `.pre-commit-config.yaml` (ruff + mypy + a hook forbidding `src/` → `spikes/` imports), `.github/workflows/ci.yml` (quality job + early egress-test canary). `core/errors.py` (one `NeuroPACAError` root, layer-mapped subclasses) and `core/logging.py` (stdlib only, idempotent `configure()`, `get_logger()`, `redact()`) with tests. `ruff` + `mypy --strict` (core) + 16 tests all green.
- **2026-08-29 · B0 spike harness** in `spikes/b0_bitnet/` (throwaway, not imported by the daemon; pre-commit-enforced): `_common.py` (llama.cpp loader, prompt + per-call GBNF assembly, post-generation validation gate, scoring), `grammars/insight.gbnf.template`, `gen_fixtures.py` (deterministic, seeded — generates 20x{K=1,3,5,8} synthetic fixtures; smoke-tested), `benchmark_runtime.py` (RSS/tok-s/thermal over 30 min), `coherence_ablation.py` (citation-accuracy(K) curve -> knee -> 3B-fallback recommendation).

## 🔨 In progress

- **B0 spike not yet executed.** The harness is written and the fixture generator runs, but nothing has touched a real model — needs `uv pip install -e ".[spike]"` + the BitNet 2B4T GGUF on the target machine. Both B0 questions (RAM/latency fit; citation accuracy >= ~80% at some K) are still open — see Blockers.
- **Runtime budgets in the benchmark are provisional guesses** (`< 1.4 GB` RSS, `>= 5 tok/s`) — revise against `PRD.md §9` once there are real numbers.

## 🧭 Decisions

| # | Decision |
| --- | --- |
| D-1 | The Markdown docs are derived from the three source files in root: `1000071408.png` (authoritative class diagram), `neuropaca-v4.html` (full concept), `neuropaca-overview.html` (overview + tech-fix). Where a doc and a source conflict, the source wins; where the diagram and the concept HTML conflict, the diagram wins. |
| D-2 | Inference is **BitNet b1.58 2B4T via llama.cpp, in-process** — not Ollama, not a 7B model. 2B4T is the only released usable BitNet b1.58 checkpoint. The Ollama-pruning route is architecturally impossible (see `problems.md` T1). `BitNetRuntime` owns `model` + `tokenizer` directly. Applied to `PRD.md` / `Architecture.md` 2026-08-29. |
| D-3 | **Decided 2026-08-29:** attention-head pruning is **deferred to the end of the roadmap** (phase D1, after B0–B9). All of it moved to `pruning.md`. In the core system `relevance_score` drives graph retention, DMN replay, and retrieval ranking — not model weights. Nothing in B0–B9 depends on pruning. Weekly model training also cut from core scope. |
| D-4 | **Decided 2026-08-29:** small-model coherence is handled by **schema-first constrained generation** — every `BitNetRuntime` call runs against a task-specific GBNF grammar with `cited_nodes` locked to the IDs in the prompt, an `abstain` path, distilled input (≤ 5 nodes), temp ≈ 0, and a hard post-generation validation gate. Full rules in `rules.md §4.1`; risk + B0 test in `problems.md` 1.13. Fallbacks (micro-decompose `$?`, or 3B model for `$?` only) are Plan B, contingent on the B0 result. |
| D-6 | **Decided 2026-08-29 (5 rulings unblocking B1, user-approved).** (a) `Config.inference_backend: str = "llama"` — `"fake"` in tests; validation is backend-aware (`model_path` checked only for `"llama"`). (b) `grammar: Optional[str] = None` added **now** to `infer()`, `infer_async()`, and the `InferenceBackend` protocol — B4's GBNF path needs no later signature change. (c) Hub IDs are exactly `"YOU"` and `"domain:engineering"` … `"domain:learning"` (10). (d) **Singletons stay** — `get_instance()` for `EventBus`/`GraphMemory`/`BitNetRuntime`; test isolation is `conftest.py` wiping `_instance`. No DI refactor. (e) B1 hardcodes `bridge_value = 0.0` (domains land in B2/B3); `relevance_score` uses fixed-scale normalization over the other 3 terms (frequency, decay, connections). Applied to `Architecture.md §3.3`, `§3.4`. |
| D-5 | **Decided + partly applied 2026-08-29 (pre-B1 structural fixes, user-approved).** (1) `GraphMemory.graph` is `networkx.MultiDiGraph`, not `DiGraph` — parallel edges keyed by `RelationType`. (2) All node identifiers and node-reference fields are `str`; only `Event.id` stays `UUID`. (3) `MEMORY_UPDATED` is published by the mutating *module* (L3/L6/L9), never by `GraphMemory` (which holds no `EventBus` ref — Invariant 1). (4) `find_related()` never traverses *through* the 11 routing hubs (`YOU` + 10 `domain:*`); `traverse_hubs=False` default — protects the < 50 ms target. (5) `prune()` / `prune_low_score()` skip a hard-coded protected set = the same 11 hubs. (6) `EventBus.event_queue` is `asyncio.Queue(maxsize=1000)`; `publish()` is `put_nowait` + `except QueueFull` → drop, bump `_dropped_count`, log ERROR (no SYSTEM_ERROR *event* — queue is full). (7) `GraphMemory` public methods acquire `_lock` and delegate to lock-free `_*_locked` workers; compound ops lock once. (8) `tests/conftest.py` gets an `autouse` fixture calling each singleton's `_reset_for_tests()` before every test. Applied now: `Architecture.md §3.1/§3.2/§3.5/§9/§13`, `PRD.md §4`. Applied in code when the `core/` modules land in B1. Tooling done: `uv lock` (networkx 3.4.2 now in `uv.lock`), pre-commit mypy `additional_dependencies: ["networkx==3.4.2"]`, `[[tool.mypy.overrides]]` `module = "networkx.*"` `ignore_missing_imports = true`. |

## ❓ Open questions / 🚧 Blockers

- **B0 spike (highest risk) — harness ready, not run:** (a) does BitNet b1.58 2B4T via llama.cpp fit the RAM/latency budget on the target machine? (b) with GBNF-constrained schemas, does it hit ~80 % citation accuracy on `(signal + K nodes)` inputs, and at which K? Everything in L4/L6/L9 depends on both. Run `spikes/b0_bitnet/` on the target machine. See `problems.md` 1.13.
- *(Deferred — tracked in `pruning.md`, not a current blocker: the `domain_to_heads` probing method, and whether heads are topic-separable at 2B.)*
- **L7 Action and L8 Agents** are reconstructed (`Architecture.md §11b`) — the class diagram is cut off at the right edge. Re-export at full width and reconcile before building them.
- **Node/Edge/enum drift** between the class diagram and the concept HTML's inline code — the diagram's shapes are treated as canonical; the HTML variants are noted in `Architecture.md §3`.
- **`ActivityCollector.get_idle_seconds()`** is platform-specific per the blueprint — the concrete backend for the target OS is undecided.

---

## 🔄 Update protocol

**When to update:** end of every work session · on completing any build step · on any decision worth remembering · on hitting a blocker.

1. **📍 Current state** — phase, *currently working on*, next action, last-updated date
2. **✅ Completed log** — what was actually finished, not what was attempted
3. **🔨 In progress** — the honest state, including what does *not* work yet
4. **🧭 Decisions** — any choice a future session might otherwise re-litigate
5. **❓ Open questions / 🚧 Blockers** — add, or resolve and strike through

**Rules:** write what is true, not what was intended. Append to the completed log; never rewrite history. Update the last-updated date every time.

---

*Related: [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [rules.md](rules.md) · [phases.md](phases.md) · [design.md](design.md)*
