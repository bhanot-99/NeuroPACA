# pruning.md — Personal Model Pruning (DEFERRED)

**Status:** Deferred to the **end** of the roadmap. Not part of build steps B0–B9. Nothing in the core system depends on it.

This is NeuroPACA's original and most ambitious idea — and its biggest research risk. It is fenced off here so the rest of the project can be built, shipped, and dogfooded without it. We return to it only after the core loop (sensing → graph → grounded answers → idle cognition → action gradients) is proven useful.

---

## 1. The idea — the fourth job of `relevance_score`

Every graph node carries one `relevance_score` (see `PRD.md §3`). In the core system it does three jobs: graph **retention** (`prune_low_score`), memory **replay** priority (the DMN), and **retrieval ranking** (context building for `$`).

The deferred fourth job: **the same score decides which attention heads of the local model get cut.**

- `score ≥ 7` → protect that domain's heads
- `score ≤ 3` → flag them for removal
- `4–7` → keep, compressible

The result would be a sparse subnetwork shaped around the two or three domains you actually work in — a "personal" model, smaller than the base.

---

## 2. Why it's deferred (not deleted)

1. **BitNet b1.58 2B4T already fits a laptop** (~1.1 GB resident). Pruning was a fix for "the model is too big." That problem no longer exists — so pruning is an optimisation, not a requirement.
2. **It's the riskiest part of the whole project.** See §5. It may simply not work at 2B.
3. **Doing it last is the right order.** By the end there will be weeks of real usage data to prune against, and the core system will have proven its worth first.
4. **The tradeoff:** we won't know whether the central thesis ("behaviour can shape a model") holds until the end. **Mitigation:** nothing else depends on it, so a negative result costs nothing structural — and a documented negative result is itself a publishable deliverable (`problems.md` Part 2, claim E).

---

## 3. The design so far

### 3.1 BitNet b1.58 2B4T

Weights ∈ {−1, 0, +1}, 1.58 bits/param. Matrix multiplications become additions/subtractions — no floating point at inference. ~0.4 GB of weights, ~1.1 GB resident. Run in-process via **llama.cpp** (which exposes model internals), not Ollama.

### 3.2 The bridge — `domain_to_heads`

Graph nodes are semantic concepts; model components are attention heads — different spaces. A static lookup table maps `domain → [(layer, head_id), …]`, built **once at setup** by probing the model with representative inputs per domain. After that, generating a prune mask is a DB read + table lookup — no inference at prune time.

```
for node in graph.all_nodes():
    heads = domain_to_heads[node.domain]
    if   node.relevance_score <= 3: mask.prune(heads)
    elif node.relevance_score >= 7: mask.protect(heads)
```

### 3.3 The tech-fix — llama.cpp, not Ollama

The original design routed pruning through **Ollama** at inference time. That is architecturally impossible: Ollama loads the model as a frozen binary — there is no "skip head 5" parameter, and modifying weights in memory + re-serialising + reloading takes seconds, not milliseconds.

The fix: build the head mask from graph scores and apply it **once at model load** (`load_gguf_with_mask`). The sparse subnetwork then runs natively at inference — no reload, no latency penalty. The mask is regenerated only when scores change significantly (idle time). This is why `BitNetRuntime` owns `model` + `tokenizer` in-process (`Architecture.md §11`).

### 3.4 Framing — usage-driven structured pruning, not "Lottery Ticket"

The Lottery Ticket Hypothesis (Frankle & Carbin, 2019) is a specific claim about finding sparse subnetworks *before training* that then *train* to full accuracy. NeuroPACA does one-shot post-hoc structured head removal guided by an outside signal. Call it **"usage-driven structured pruning of a personal language model."** Cite LTH as inspiration only ("most of a network is unused for any one task") — never as the method.

---

## 4. Open questions — must resolve before building

| # | Question |
| --- | --- |
| Q1 | Are attention heads in a 2B model topic-separable enough that cutting a domain's heads removes that capability without hurting the rest? (`problems.md` 1.2) |
| Q2 | How much can you cut before quality drops? Measure the curve at 5/10/20/30 %, find the knee. (`problems.md` 1.3) |
| Q3 | `domain_to_heads` probing method — with what prompts? The graph has behavioural nodes (`pytest`), not sentences. |
| Q4 | Is a short recovery pass needed after pruning? "No retraining ever" may not hold. |
| Q5 | Does BitNet quantization-aware machinery (STE) make any recovery pass materially harder than a normal fine-tune? |

---

## 5. Risks

Carried over from `problems.md` — these apply **only** to this deferred work:

- **1.2** — the graph-node → model-part bridge is the weakest piece; heads aren't topic-clean at 2B.
- **1.3** — a 2B BitNet model has almost no room to cut without a recovery pass.
- **1.4** — "Lottery Ticket" is an overclaim (addressed by §3.4 framing).

If Q1/Q2 come back negative: the paper reports it as a result ("behavioural scores did not predict prunable regions at 2B; here is the data"), and the `relevance_score` keeps its three core jobs.

---

## 6. Build steps (when we get here — after B9)

1. **Cheap spike first.** Released 2B4T + a hand-built `domain_to_heads` table for 3 domains. Zero some heads, measure quality vs sparsity on a held-out personal prompt set. Live in `experiments/pruning/`.
2. If the spike is promising: `domain_to_heads` probing pipeline (once, at setup).
3. `relevance_score` → prune-mask generator.
4. `load_gguf_with_mask` — mask applied once at model load; `BitNetRuntime.load_model()` gains a mask parameter.
5. DMN regenerates the mask when scores shift significantly.
6. A/B quality harness: sparse vs base vs random-head-cut vs magnitude-pruned, on personal prompts.
7. One-command rollback; mandatory backup of the previous model.
8. Promote from `experiments/pruning/` to `src/neuropaca/sparsity/` — **never imported by the daemon**, offline only.

**Exit:** the sparse model is measurably smaller with no measurable quality loss on personal prompts, and rollback restores the previous model exactly. Feature is off by default.

---

## 7. How it plugs back in

Nothing needs to change in B0–B9 to keep this door open:

- `relevance_score` and `domain` already exist on every node.
- `domain_to_heads` is built once at setup, independent of the daemon.
- The prune mask is regenerated during idle time by the DMN.
- Only `BitNetRuntime.load_model()` gains an optional `mask` argument.
- `sparsity/` never appears in the running daemon's import graph.

---

*Related: [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [phases.md](phases.md) · [problems.md](problems.md) · [memory.md](memory.md)*
