# memory.md — Living Project State

## 📍 Current state

- **Phase:** B0 — Foundation. **In progress.** Skeleton + tooling + spike harness done; spike not yet run.
- **Currently working on:** B0. Branch `b0-foundation`.
- **Next action:** run the BitNet de-risking spike on the target machine (`spikes/b0_bitnet/README.md`) — the runtime benchmark and the coherence ablation. Record the numbers here and in `PRD.md §9`, set production `K` at the knee. Then B1.
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
